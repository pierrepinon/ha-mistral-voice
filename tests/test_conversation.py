"""Tests de l'entité agent de conversation Mistral (ChatLog + flux).

Architecture testée (Home Assistant 2026.10) : l'agent est une
`ConversationEntity` dont `_async_handle_message` reçoit un `ChatLog`
persistant par conversation — la **mémoire multi-tours** — et diffuse
les deltas de la réponse en flux (`chat_log.async_add_delta_content_stream`),
ce qui alimente le TTS du pipeline au fil de l'eau.

Mocks : client chat en flux (`async_chat_complete_stream`, itérateurs
d'événements factices) et API LLM Assist (`llm.LLM_API_ASSIST.
async_get_api_instance` remplaçée par une instance factice). Clé API
factice uniquement ; la conversion de schéma, la boucle tool calling,
le cap de `max_tokens`, le pre-prompt, le plafond d'historique, le
streaming des deltas et l'erreur polie sont vérifiés. Intégration
légère : enregistrement de l'agent au setup / retrait au unload.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from homeassistant.components import conversation as ha_conversation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import chat_session, entity_registry as er, intent, llm
import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mistral_voice import conversation as conv
from custom_components.mistral_voice.api import MistralApiError
from custom_components.mistral_voice.const import (
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    DOMAIN,
    ERROR_LOOP_LIMIT_TEXT,
    ERROR_RESPONSE_TEXT,
    MAX_HISTORY_MESSAGES,
)
from custom_components.mistral_voice.conversation import MistralVoiceAgentEntity

KEY_PLACEHOLDER = "placeholder-key-from-test"

SPEAK_LANGUAGE = "fr"

# Deux tours de chat streaming : un tool_call puis la réponse finale.
EXPECTED_CHAT_CALLS_AFTER_TOOL = 2


# ---------------------------------------------------------------- fixtures


class FakeTool(llm.Tool):
    """Outil LLM de test (nom, description, schéma probatio)."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: probatio.Schema | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or probatio.Schema({})

    async def async_call(
        self, hass, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        return llm.ToolResult(data={"turn_on": tool_input.tool_args})


class FakeApiInstance:
    """APIInstance LLM Assist de test."""

    def __init__(self, tools: list[llm.Tool]) -> None:
        self.tools = tools
        self.custom_serializer = None
        self.api_prompt = ""
        self.tool_calls: list[llm.ToolInput] = []
        self.tool_results: dict[str, dict] = {}

    async def async_call_tool(self, tool_input: llm.ToolInput) -> llm.ToolResult:
        self.tool_calls.append(tool_input)
        return llm.ToolResult(data=self.tool_results.get(tool_input.tool_name, {}))


@pytest.fixture
def api_instance() -> FakeApiInstance:
    """Instance LLM Assist factice avec deux outils."""
    return FakeApiInstance(
        tools=[
            FakeTool(
                name="HassTurnOn",
                description="Allume les entités cibles",
                parameters=probatio.Schema(
                    {"name": list[str], "area": probatio.Optional(str)}
                ),
            ),
            FakeTool(name="HassGetState", description="Récupère un état"),
        ]
    )


@pytest.fixture
def fake_llm_api(monkeypatch, api_instance) -> FakeApiInstance:
    """Brancher l'instance factice sur `llm.LLM_API_ASSIST`."""

    async def fake_get_api(hass, api_id, llm_context):
        assert api_id == llm.LLM_API_ASSIST
        return api_instance

    monkeypatch.setattr(llm, "async_get_api", fake_get_api)
    return api_instance


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    """Entrée de configuration avec clé API factice."""
    mock = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": KEY_PLACEHOLDER},
    )
    mock.add_to_hass(hass)
    return mock


@pytest.fixture
def agent(hass, entry) -> MistralVoiceAgentEntity:
    """Entité agent de conversation avec l'entrée de test."""
    entity = MistralVoiceAgentEntity(entry)
    entity.hass = hass
    entity.entity_id = f"conversation.{DOMAIN}"
    return entity


def make_conversation_input(
    text: str = "allume la lampe du salon",
    conversation_id: str | None = "conv-123",
    language: str = SPEAK_LANGUAGE,
) -> ha_conversation.ConversationInput:
    """ConversationInput de test (pipeline Assist)."""
    return ha_conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id=conversation_id,
        device_id=None,
        satellite_id=None,
        language=language,
        agent_id=f"conversation.{DOMAIN}",
    )


def make_delta(
    content: Any = None,
    tool_calls: list | None = None,
    role: str | None = None,
) -> SimpleNamespace:
    """Delta de chunk streaming Mistral (forme du SDK, champ optionnel)."""
    return SimpleNamespace(content=content, tool_calls=tool_calls, role=role)


def make_tool_call_delta(
    call_id: str, name: str, args: dict | str, index: int = 0
) -> SimpleNamespace:
    """ToolCall streaming de test (arguments assemblés par le transformeur)."""
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(
        id=call_id,
        index=index,
        function=SimpleNamespace(name=name, arguments=raw),
    )


def make_stream(deltas: list[SimpleNamespace]) -> Any:
    """Flux SSE factice : réponses en file, appelées async chat stream."""

    async def gen():
        for delta in deltas:
            yield SimpleNamespace(
                data=SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
            )

    return gen()


class ChatStreamMock:
    """Mock de `api.async_chat_complete_stream` : tours en file + appels gardés.

    Chaque tour est une liste de deltas ; la dernière liste est répétée
    quand la file est vide (test de la limite d'itérations).
    """

    def __init__(self, rounds: list[list[SimpleNamespace]]) -> None:
        self.rounds = list(rounds)
        self.calls: list[dict] = []

    async def __call__(self, hass, entry, **kwargs):
        # Copie profonde : la liste `messages` est étendue par l'agent
        # entre les tours de la boucle (le mock ne doit pas capturer
        # d'alias mutable sur les appels précédents).
        self.calls.append(
            {
                **kwargs,
                "messages": [dict(message) for message in kwargs["messages"]],
                "tools": [dict(tool) for tool in (kwargs.get("tools") or [])],
            }
        )
        index = min(len(self.calls) - 1, len(self.rounds) - 1)
        return make_stream(self.rounds[index])


async def process(
    hass,
    agent: MistralVoiceAgentEntity,
    user_input: ha_conversation.ConversationInput,
):
    """Traiter un énoncé comme l'entité : session + ChatLog + message."""
    with (
        chat_session.async_get_chat_session(
            hass, user_input.conversation_id
        ) as session,
        ha_conversation.async_get_chat_log(hass, session, user_input) as chat_log,
    ):
        return await agent._async_handle_message(user_input, chat_log)


# ----------------------------------------------- conversion function-calling


async def test_tool_schema_conversion(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Les outils llm.Tool sont convertis au schéma function-calling Mistral."""
    chat = ChatStreamMock([[make_delta(content="C'est fait.")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    result = await process(hass, agent, make_conversation_input())

    assert len(chat.calls) == 1
    tools = chat.calls[0]["tools"]
    assert [t["type"] for t in tools] == ["function", "function"]
    turn_on = tools[0]["function"]
    assert turn_on["name"] == "HassTurnOn"
    assert turn_on["description"] == "Allume les entités cibles"
    parameters = turn_on["parameters"]
    # JSON Schema rendu depuis probatio.Schema.
    assert parameters["type"] == "object"
    assert parameters["properties"]["name"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert "area" in parameters["properties"]
    get_state = tools[1]["function"]
    assert get_state["name"] == "HassGetState"
    # Schéma vide -> schéma objet valide.
    assert get_state["parameters"]["type"] == "object"
    assert result.response.speech["plain"]["speech"] == "C'est fait."


# ------------------------------------------------- boucle de tool calling


async def test_tool_call_loop(hass, entry, agent, fake_llm_api, monkeypatch) -> None:
    """1 itération : tool_call -> async_call_tool -> message tool -> final."""
    fake_llm_api.tool_results["HassTurnOn"] = {"success": True}
    chat = ChatStreamMock(
        [
            [
                make_delta(
                    tool_calls=[
                        make_tool_call_delta(
                            "call_1", "HassTurnOn", {"name": "Lampe Salon"}
                        )
                    ]
                )
            ],
            [make_delta(content="J'ai allumé la lampe du salon.")],
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    result = await process(hass, agent, make_conversation_input())

    assert len(fake_llm_api.tool_calls) == 1
    assert fake_llm_api.tool_calls[0].tool_name == "HassTurnOn"
    assert fake_llm_api.tool_calls[0].tool_args == {"name": "Lampe Salon"}

    # Deuxième tour : system + user + assistant(tool_calls) + tool.
    assert len(chat.calls) == EXPECTED_CHAT_CALLS_AFTER_TOOL
    second_messages = chat.calls[1]["messages"]
    assert [m["role"] for m in second_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    replayed_assistant = second_messages[2]
    assert replayed_assistant["tool_calls"]
    tool_message = second_messages[3]
    assert tool_message["tool_call_id"] == "call_1"
    assert json.loads(tool_message["content"]) == {"success": True}

    assert result.response.speech["plain"]["speech"] == "J'ai allumé la lampe du salon."


async def test_tool_arguments_assembled_across_deltas(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Les arguments d'un tool call arrivant en morceaux sont assemblés."""
    chat = ChatStreamMock(
        [
            [
                make_delta(
                    tool_calls=[
                        SimpleNamespace(
                            id="",
                            index=0,
                            function=SimpleNamespace(
                                name="HassTurnOn", arguments='{"na'
                            ),
                        )
                    ]
                ),
                make_delta(
                    tool_calls=[
                        SimpleNamespace(
                            id="call_x",
                            index=0,
                            function=SimpleNamespace(
                                name="", arguments='me": "Lampe"}'
                            ),
                        )
                    ]
                ),
            ],
            [make_delta(content="ok")],
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    result = await process(hass, agent, make_conversation_input())

    assert len(fake_llm_api.tool_calls) == 1
    assert fake_llm_api.tool_calls[0].tool_args == {"name": "Lampe"}
    assert fake_llm_api.tool_calls[0].id == "call_x"
    assert result.response.speech["plain"]["speech"] == "ok"


async def test_tool_call_loop_hits_iteration_limit(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Au-delà de MAX_TOOL_ITERATIONS, réponse de repli (pas de boucle infinie)."""
    chat = ChatStreamMock(
        [
            [
                make_delta(
                    tool_calls=[
                        make_tool_call_delta(
                            "call_loop", "HassTurnOn", {"name": "Lampe"}
                        )
                    ]
                )
            ]
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    result = await process(hass, agent, make_conversation_input())

    assert len(chat.calls) == conv.MAX_TOOL_ITERATIONS
    assert result.response.speech["plain"]["speech"] == ERROR_LOOP_LIMIT_TEXT


# ------------------------------------------------ mémoire multi-tours


async def test_conversation_memory_across_turns(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Le deuxième tour reçoit l'historique du premier (ChatLog persistant)."""
    chat = ChatStreamMock(
        [
            [make_delta(content="Le salon est à 20 degrés.")],
            [make_delta(content="Et la cuisine ?")],
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    # Même session (même conversation) sur deux énoncés successifs.
    with chat_session.async_get_chat_session(hass, "conv-42") as session:
        first_input = make_conversation_input(
            text="Quelle température fait-il au salon ?", conversation_id="conv-42"
        )
        with ha_conversation.async_get_chat_log(hass, session, first_input) as chat_log:
            await agent._async_handle_message(first_input, chat_log)

        second_input = make_conversation_input(
            text="Et à côté ?", conversation_id="conv-42"
        )
        with ha_conversation.async_get_chat_log(
            hass, session, second_input
        ) as chat_log:
            await agent._async_handle_message(second_input, chat_log)

    second_messages = chat.calls[1]["messages"]
    roles = [message["role"] for message in second_messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert second_messages[1]["content"] == "Quelle température fait-il au salon ?"
    assert second_messages[2]["content"] == "Le salon est à 20 degrés."
    assert second_messages[3]["content"] == "Et à côté ?"


def test_history_cap_cut_on_user_boundary() -> None:
    """Le plafond d'historique coupe sur une frontière de tour utilisateur."""
    messages = [{"role": "system", "content": "prompt"}]
    for index in range(20):
        messages.append({"role": "user", "content": f"q{index}"})
        messages.append({"role": "assistant", "content": f"r{index}"})

    capped = conv._cap_history(messages)
    assert capped[0]["role"] == "system"
    assert len(capped) <= MAX_HISTORY_MESSAGES
    # La liste commence toujours par un tour utilisateur complet.
    assert capped[1]["role"] == "user"
    # Les tours ne sont jamais dissociés : pas de question orpheline.
    roles = [message["role"] for message in capped]
    assert roles.count("user") == roles.count("assistant")


def test_history_kept_below_cap() -> None:
    """Un historique court part intégralement (pre-prompt inclus)."""
    messages = [
        {"role": "system", "content": "p"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "r"},
    ]
    assert conv._cap_history(messages) == messages


# --------------------------------------------------- streaming des deltas


async def test_content_deltas_streamed_to_listener(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Les deltas de contenu alimentent le delta_listener (pipeline/TTS)."""
    chat = ChatStreamMock(
        [
            [
                make_delta(role="assistant"),
                make_delta(content="Bon"),
                make_delta(content="jour le "),
                make_delta(content="monde."),
            ]
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    deltas: list[dict] = []
    with chat_session.async_get_chat_session(hass, "conv-delta") as session:
        user_input = make_conversation_input(conversation_id="conv-delta")
        listener = lambda log, delta: deltas.append(delta)  # noqa: E731
        with ha_conversation.async_get_chat_log(
            hass, session, user_input, chat_log_delta_listener=listener
        ) as chat_log:
            result = await agent._async_handle_message(user_input, chat_log)

    contents = [d.get("content") for d in deltas if d.get("content")]
    assert contents == ["Bon", "jour le ", "monde."]
    # Deux opening roles : un par tour de boucle (ici un seul tour).
    assert sum(1 for d in deltas if d.get("role") == "assistant") >= 1
    assert result.response.speech["plain"]["speech"] == "Bonjour le monde."


# ------------------------------------------------ paramètres de l'appel chat


async def test_chat_call_parameters(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """max_tokens capé (plafond de sécurité), tool_choice auto,
    model = option (défaut).
    """
    chat = ChatStreamMock([[make_delta(content="Bonjour.")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    await process(hass, agent, make_conversation_input())

    call = chat.calls[0]
    assert call["max_tokens"] == DEFAULT_MAX_TOKENS
    assert call["tool_choice"] == "auto"
    assert call["model"] == DEFAULT_MODEL


async def test_options_override_model(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """L'option `model` prime sur la constante par défaut."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_MODEL: "mon-model-custom"}
    )
    chat = ChatStreamMock([[make_delta(content="ok")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    await process(hass, agent, make_conversation_input())

    assert chat.calls[0]["model"] == "mon-model-custom"


async def test_options_override_max_tokens(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """L'option `max_tokens` prime sur la valeur par défaut (plafond)."""
    custom_max_tokens = 2048
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_MAX_TOKENS: custom_max_tokens, CONF_MODEL: "mon-model-custom"},
    )
    chat = ChatStreamMock([[make_delta(content="ok")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    await process(hass, agent, make_conversation_input())

    assert chat.calls[0]["max_tokens"] == custom_max_tokens


# -------------------------------------------------------------- pre-prompt


async def test_default_system_prompt(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Le pre-prompt par défaut (texte exact de const.py) est en tête du système."""
    chat = ChatStreamMock([[make_delta(content="Hola")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    await process(hass, agent, make_conversation_input())

    system_message = chat.calls[0]["messages"][0]
    assert system_message["role"] == "system"
    # Le ChatLog compose : pre-prompt + prompt API LLM + date/heure.
    assert system_message["content"].startswith(DEFAULT_SYSTEM_PROMPT)


async def test_system_prompt_option_override(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """L'option `system_prompt` non vide surcharge le pre-prompt par défaut."""
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_SYSTEM_PROMPT: "Toujours répondre en breton.",
            CONF_MODEL: DEFAULT_MODEL,
        },
    )
    chat = ChatStreamMock([[make_delta(content="Kenavo.")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    await process(hass, agent, make_conversation_input())

    system_message = chat.calls[0]["messages"][0]
    assert system_message["content"].startswith("Toujours répondre en breton.")


# --------------------------------------------------- erreurs API polies


async def test_api_error_returns_polite_french_response(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Erreur API : réponse d'erreur polie, jamais de crash ni de clé."""

    async def failing_chat(hass, entry, **kwargs):
        raise MistralApiError("POST /v1/chat/completions a échoué : …")

    monkeypatch.setattr(conv, "async_chat_complete_stream", failing_chat)

    result = await process(hass, agent, make_conversation_input())

    speech = result.response.speech["plain"]["speech"]
    assert speech == ERROR_RESPONSE_TEXT
    assert result.response.response_type is intent.IntentResponseType.ERROR
    # Aucune fuite : la clé factice est absente de toute la réponse parlée.
    assert KEY_PLACEHOLDER not in json.dumps(result.response.speech)


async def test_llm_api_unavailable_returns_polite_french_response(
    hass, entry, agent, monkeypatch
) -> None:
    """API LLM Assist indisponible : réponse d'erreur polie sans crash."""

    async def failing_get_api(hass, api_id, llm_context):
        raise RuntimeError("API Assist absente")

    monkeypatch.setattr(llm, "async_get_api", failing_get_api)

    result = await process(hass, agent, make_conversation_input())

    assert result.response.speech["plain"]["speech"] == ERROR_RESPONSE_TEXT
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_empty_response_returns_polite_error(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Réponse sans texte parlable (max_tokens, raisonnement) -> réponse polie."""
    chat = ChatStreamMock([[make_delta(content="")]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    result = await process(hass, agent, make_conversation_input())

    assert result.response.speech["plain"]["speech"] == ERROR_RESPONSE_TEXT
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_content_parts_only_text_spoken(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Modèles raisonnement hébergés : deltas `content` en liste de parties.

    Seules les parties `text` sont conservées (concaténées dans l'ordre) ;
    les parties raisonnement ne sont jamais parlées.
    """
    parts = [
        SimpleNamespace(type="reasoning", text="L'utilisateur demande l'heure…"),
        SimpleNamespace(type="text", text="Il est "),
        SimpleNamespace(type="text", text="23 h 17."),
    ]
    chat = ChatStreamMock([[make_delta(content=parts)]])
    monkeypatch.setattr(conv, "async_chat_complete_stream", chat)

    result = await process(hass, agent, make_conversation_input())

    assert result.response.speech["plain"]["speech"] == "Il est 23 h 17."
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE


# --------------------------------------- enregistrement au setup / unload


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_agent_entity_registered_on_setup_and_unset_on_unload(hass) -> None:
    """L'entité conversation s'enregistre au setup, se retire au unload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": KEY_PLACEHOLDER},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # L'agent s'est enregistré auprès du Pipeline Assist sous l'id d'entrée
    # (compatibilité avec les pipelines existants).
    agent = ha_conversation.async_get_agent(hass, entry.entry_id)
    assert isinstance(agent, MistralVoiceAgentEntity)
    assert agent.supported_languages == "*"

    # L'entité est dans le registre du domaine conversation.
    registry = er.async_get(hass)
    conv_entities = [
        reg_entry
        for reg_entry in registry.entities.values()
        if reg_entry.platform == DOMAIN and reg_entry.domain == "conversation"
    ]
    assert len(conv_entities) == 1

    # L'agent expose le streaming.
    entry_info = ha_conversation.async_get_agent_info(hass, agent.entity_id)
    assert entry_info is not None
    assert entry_info.supports_streaming is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ha_conversation.async_get_agent(hass, entry.entry_id) is None
