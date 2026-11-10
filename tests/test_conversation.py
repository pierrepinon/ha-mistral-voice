"""Tests de l'agent de conversation Mistral (ticket #6).

Mocks : client chat (`api.async_chat_complete`) et API LLM Assist de HA
(`llm.async_get_api` / `api_instance.async_call_tool`). Clé API factice
uniquement ; la conversion de schéma, la boucle tool calling, le cap de
max_tokens, le pre-prompt, le cache byte-stable et l'erreur polie sont
vérifiés. Intégration légère : enregistrement / désenregistrement de
l'agent auprès du Pipeline Assist (async_get_agent / unset).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from homeassistant.components import conversation as ha_conversation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import intent, llm
import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mistral_voice import conversation as conv
from custom_components.mistral_voice.api import MistralApiError
from custom_components.mistral_voice.const import (
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    DOMAIN,
    ERROR_LOOP_LIMIT_TEXT,
    ERROR_RESPONSE_TEXT,
    MAX_TOKENS,
)
from custom_components.mistral_voice.conversation import MistralVoiceAgent

KEY_PLACEHOLDER = "placeholder-key-from-test"

SPEAK_LANGUAGE = "fr"

# Deux échanges chat/completions : un tool_call puis la réponse finale.
EXPECTED_CHAT_CALLS_AFTER_TOOL = 2

# Cap max_token demandé par le ticket (~150 ; constante MAX_TOKENS).
EXPECTED_MAX_TOKENS = 150


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


class FakeAPIInstance:
    """APIInstance LLM Assist de test."""

    def __init__(self, tools: list[llm.Tool]) -> None:
        self.tools = tools
        self.tool_calls: list[llm.ToolInput] = []
        self.tool_results: dict[str, dict] = {}

    async def async_call_tool(self, tool_input: llm.ToolInput) -> llm.ToolResult:
        self.tool_calls.append(tool_input)
        return llm.ToolResult(data=self.tool_results.get(tool_input.tool_name, {}))


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


def make_chat_response(
    content: str | list | None = None, tool_calls: list | None = None
) -> SimpleNamespace:
    """Réponse chat/completions en object-SDK (SimpleNamespace suffisant).

    `content` accepte aussi une liste de parties typées (modèles
    raisonnement hébergés, ex. `zai-glm-5-3-flash`).
    """
    message = SimpleNamespace(
        content="" if content is None else content, tool_calls=tool_calls
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")]
    )


def make_tool_call(call_id: str, name: str, args: dict | str) -> SimpleNamespace:
    """ToolCall de test (arguments dict ou chaîne JSON, comme l'API)."""
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=raw),
    )


class ChatMock:
    """Mock de `api.async_chat_complete` : réponses en file + appels gardés.

    Une fois la file vide, la dernière réponse est répétée (permet de
    tester la limite d'itérations avec un modèle qui déborde d'appels).
    """

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, hass, entry, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


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
def agent(hass, entry) -> MistralVoiceAgent:
    """Agent de conversation avec l'entrée de test."""
    return MistralVoiceAgent(hass, entry)


@pytest.fixture
def fake_llm_api(monkeypatch) -> FakeAPIInstance:
    """Mock de `llm.async_get_api` : instance avec outils Assist."""
    api_instance = FakeAPIInstance(
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

    async def fake_async_get_api(hass, api_id, llm_context):
        return api_instance

    monkeypatch.setattr(llm, "async_get_api", fake_async_get_api)
    return api_instance


# ----------------------------------------------- conversion function-calling


async def test_tool_schema_conversion(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Les outils llm.Tool sont convertis au schéma function-calling Mistral."""
    chat = ChatMock([make_chat_response(content="C'est fait.")])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

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
    chat = ChatMock(
        [
            make_chat_response(
                tool_calls=[
                    make_tool_call("call_1", "HassTurnOn", {"name": "Lampe Salon"})
                ]
            ),
            make_chat_response(content="J'ai allumé la lampe du salon."),
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

    assert len(fake_llm_api.tool_calls) == 1
    assert fake_llm_api.tool_calls[0].tool_name == "HassTurnOn"
    assert fake_llm_api.tool_calls[0].tool_args == {"name": "Lampe Salon"}

    # Deuxième appel : la file contient system + user + assistant + tool.
    assert len(chat.calls) == EXPECTED_CHAT_CALLS_AFTER_TOOL
    second_messages = chat.calls[1]["messages"]
    assert [m["role"] for m in second_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert second_messages[2]["tool_calls"] != []
    tool_message = second_messages[3]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    assert json.loads(tool_message["content"]) == {"success": True}

    assert result.response.speech["plain"]["speech"] == "J'ai allumé la lampe du salon."
    assert result.conversation_id == "conv-123"


async def test_tool_call_loop_hits_iteration_limit(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Au-delà de MAX_TOOL_ITERATIONS, réponse de repli (pas de boucle infinie)."""
    tool_call = make_tool_call("call_loop", "HassTurnOn", {"name": "Lampe"})
    chat = ChatMock([make_chat_response(tool_calls=[tool_call])])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

    assert len(chat.calls) == conv.MAX_TOOL_ITERATIONS
    assert result.response.speech["plain"]["speech"] == ERROR_LOOP_LIMIT_TEXT


# ------------------------------------------------ paramètres de l'appel chat


async def test_chat_call_parameters(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """max_tokens capé (~150), tool_choice auto, model = option (défaut)."""
    chat = ChatMock([make_chat_response(content="Bonjour.")])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    await agent.async_process(make_conversation_input())

    call = chat.calls[0]
    assert call["max_tokens"] == MAX_TOKENS
    assert call["max_tokens"] == EXPECTED_MAX_TOKENS
    assert call["tool_choice"] == "auto"
    assert call["model"] == DEFAULT_MODEL


async def test_options_override_model(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """L'option `model` prime sur la constante par défaut."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_MODEL: "mon-model-custom"}
    )
    chat = ChatMock([make_chat_response(content="ok")])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    await agent.async_process(make_conversation_input())

    assert chat.calls[0]["model"] == "mon-model-custom"


# -------------------------------------------------------------- pre-prompt


async def test_default_system_prompt(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Le pre-prompt par défaut (texte exact de const.py) est actif."""
    chat = ChatMock([make_chat_response(content="Hola")])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    await agent.async_process(make_conversation_input())

    assert chat.calls[0]["messages"][0] == {
        "role": "system",
        "content": DEFAULT_SYSTEM_PROMPT,
    }
    # Texte exact (objet str identique : clé du cache byte-stable).
    first = chat.calls[0]["messages"][0]["content"]
    second_call = ChatMock([])
    monkeypatch.setattr(conv, "async_chat_complete", second_call)
    await agent.async_process(make_conversation_input())
    assert second_call.calls[0]["messages"][0]["content"] is first


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
    chat = ChatMock([make_chat_response(content="Kenavo.")])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    await agent.async_process(make_conversation_input())

    assert chat.calls[0]["messages"][0]["content"] == "Toujours répondre en breton."


# --------------------------------------------------- cache byte-stable


async def test_tools_and_prompt_cached_byte_stable(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Mêmes objets renvoyés entre deux appels : requêtes byte-stables."""
    chat_first = ChatMock([make_chat_response(content="un")])
    monkeypatch.setattr(conv, "async_chat_complete", chat_first)
    input_ = make_conversation_input()
    result_one = await agent.async_process(input_)
    tools_first = chat_first.calls[0]["tools"]
    prompt_first = chat_first.calls[0]["messages"][0]["content"]

    fake_llm_api.tools = [
        FakeTool(
            name="HassTurnOn",
            description="Allume les entités cibles",
            parameters=probatio.Schema(
                {"name": list[str], "area": probatio.Optional(str)}
            ),
        ),
        FakeTool(name="HassGetState", description="Récupère un état"),
    ]
    chat_second = ChatMock([make_chat_response(content="deux")])
    monkeypatch.setattr(conv, "async_chat_complete", chat_second)
    result_two = await agent.async_process(input_)
    tools_second = chat_second.calls[0]["tools"]
    prompt_second = chat_second.calls[0]["messages"][0]["content"]

    # Mêmes instances (identité) entre les deux appels.
    for first, second in zip(tools_first, tools_second, strict=True):
        assert first is second
    assert tools_first == tools_second
    assert prompt_second is prompt_first  # même objet str (lru_cache)
    assert json.dumps(tools_first, sort_keys=True) == json.dumps(
        tools_second, sort_keys=True
    )
    assert result_one.response.speech["plain"]["speech"] == "un"
    assert result_two.response.speech["plain"]["speech"] == "deux"


async def test_cache_refreshes_when_tool_changes(hass, entry, agent) -> None:
    """Un outil dont le schéma change est reconverti (cache invalidé)."""
    tool_a = FakeTool(
        name="HassTurnOn",
        description="desc",
        parameters=probatio.Schema({"name": str}),
    )
    first = agent.convert_tool(tool_a)
    tool_b = FakeTool(
        name="HassTurnOn",
        description="desc",
        parameters=probatio.Schema({"name": str, "area": str}),
    )
    second = agent.convert_tool(tool_b)
    assert first is not second
    assert "area" in second["function"]["parameters"]["properties"]
    # Outil inchangé -> même objet.
    assert agent.convert_tool(tool_b) is second


# --------------------------------------------------- erreurs API polies


async def test_api_error_returns_polite_french_response(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Erreur API : réponse d'erreur polie, jamais de crash ni de clé."""

    async def failing_chat(hass, entry, **kwargs):
        raise MistralApiError("POST /v1/chat/completions a échoué : …")

    monkeypatch.setattr(conv, "async_chat_complete", failing_chat)

    result = await agent.async_process(make_conversation_input(KEY_PLACEHOLDER))

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

    result = await agent.async_process(make_conversation_input())

    assert result.response.speech["plain"]["speech"] == ERROR_RESPONSE_TEXT
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_tool_execution_failure_gives_error_content_to_model(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Un outil en échec produit un message tool d'erreur, pas d'exception."""

    async def broken_call_tool(tool_input):
        raise RuntimeError("entité introuvable")

    fake_llm_api.async_call_tool = broken_call_tool  # type: ignore[method-assign]
    chat = ChatMock(
        [
            make_chat_response(
                tool_calls=[make_tool_call("call_9", "HassTurnOn", {"name": "X"})]
            ),
            make_chat_response(content="Je n'ai pas réussi à faire l'action."),
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

    tool_message = chat.calls[1]["messages"][3]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"]) == {"error": "tool_execution_failed"}
    assert result.response.speech["plain"]["speech"] == (
        "Je n'ai pas réussi à faire l'action."
    )


async def test_reasoning_model_content_parts_spoken(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Modèles raisonnement hébergés : `content` en liste de parties typées.

    Seules les parties `text` sont conservées (concaténées dans l'ordre) ;
    les parties raisonnement ne sont jamais parlées.
    """
    content = [
        {"type": "reasoning", "text": "L'utilisateur demande l'heure…"},
        {"type": "text", "text": "Il est "},
        {"type": "text", "text": "23 h 17."},
    ]
    chat = ChatMock([make_chat_response(content=content)])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

    assert result.response.speech["plain"]["speech"] == "Il est 23 h 17."
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE


async def test_reasoning_model_content_parts_replayed_as_text(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Dans la boucle tool calling, l'assistant est rejoué en texte normalisé.

    La liste de parties ne peut pas repartir telle quelle vers Mistral :
    le message assistant rejoué porte le texte concaténé des parties text.
    """
    content = [
        {"type": "text", "text": "Je regarde la lampe."},
        {"type": "reasoning", "text": "…"},
    ]
    chat = ChatMock(
        [
            make_chat_response(
                content=content,
                tool_calls=[make_tool_call("call_p", "HassTurnOn", {"name": "Lampe"})],
            ),
            make_chat_response(content="C'est allumé."),
        ]
    )
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

    replayed = chat.calls[1]["messages"][2]
    assert replayed["role"] == "assistant"
    assert replayed["content"] == "Je regarde la lampe."
    assert result.response.speech["plain"]["speech"] == "C'est allumé."


async def test_content_without_text_parts_returns_polite_error(
    hass, entry, agent, fake_llm_api, monkeypatch
) -> None:
    """Une liste de parties sans aucun `text` reste une réponse vide → polie."""
    content = [{"type": "reasoning", "text": "je réfléchis…"}]
    chat = ChatMock([make_chat_response(content=content)])
    monkeypatch.setattr(conv, "async_chat_complete", chat)

    result = await agent.async_process(make_conversation_input())

    # Texte de repli poli ; le pipeline voit un ACTION_DONE (seule la
    # branche d'exception de async_process type la réponse en ERROR).
    assert result.response.speech["plain"]["speech"] == ERROR_RESPONSE_TEXT
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE


# --------------------------------------- enregistrement au setup / unload


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_agent_registered_on_setup_and_unset_on_unload(hass) -> None:
    """L'agent est enregistré programmatiquement au setup, retiré au unload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": KEY_PLACEHOLDER},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    agent = ha_conversation.async_get_agent(hass, entry.entry_id)
    assert agent is not None
    assert isinstance(agent, MistralVoiceAgent)
    assert agent.supported_languages == "*"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ha_conversation.async_get_agent(hass, entry.entry_id) is None
