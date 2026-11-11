"""Agent de conversation Mistral : entité conversation dotée de mémoire.

L'agent étend `conversation.ConversationEntity` (pattern
openai_conversation / google_generative_ai_conversation sur Home
Assistant 2026.10) : le pipeline Assist ouvre un `ChatLog` persistant
par conversation (`homeassistant.components.conversation`), ce qui
donne la **mémoire multi-tours** — chaque énoncé est traité avec
l'historique de la conversation, plafonné à
`MAX_HISTORY_MESSAGES` messages coupés sur une frontière de tour
utilisateur (jamais un appel d'outil séparé de sa réponse).

Pour chaque énoncé (`_async_handle_message`) :

1. `chat_log.async_provide_llm_data(...)` attache l'API LLM Assist de HA
   (`llm.LLM_API_ASSIST` : outils issus des entités exposées à Assist)
   et compose le pre-prompt système (surcharge utilisateur ou valeur
   par défaut) ;
2. les tools (`llm.Tool`, schéma `probatio.Schema`) sont convertis au
   format function-calling Mistral, l'historique du `ChatLog` en
   messages Mistral ( système / user / assistant / tool ) ;
3. la réponse arrive en **flux** : `client.chat.stream_async`
   (POST /v1/chat/completions SSE) via `api.async_chat_complete_stream`,
   les deltas de contenu sont poussés dans
   `chat_log.async_add_delta_content_stream` — c'est ce flux qui
   alimente le TTS du pipeline au fil de l'eau (streaming bout en bout
   quand le TTS supporte l'entrée en flux, i.e. notre plateforme `tts`) ;
4. boucle de tool calling : chaque tool_call est exécuté par le ChatLog
   via l'API LLM, le résultat repart en message `tool`, la conversation
   reboucle (limite `MAX_TOOL_ITERATIONS`) jusqu'au texte final.

En cas d'erreur (API injoignable, outil en échec), une réponse polie en
français est renvoyée : aucune exception, aucun détail de requête et
surtout jamais la clé API ne sortent de ce module.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import logging
from typing import Any, Literal, override

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent, llm
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import probatio

from .api import MistralApiError, MistralVoiceData, async_chat_complete_stream
from .config_flow import resolve_system_prompt
from .const import (
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    DEFAULT_MODEL,
    DOMAIN,
    ERROR_LOOP_LIMIT_TEXT,
    ERROR_RESPONSE_TEXT,
    MANUFACTURER,
    MAX_HISTORY_MESSAGES,
    MAX_TOKENS,
    MAX_TOOL_ITERATIONS,
    TOOL_CHOICE_AUTO,
)

MistralVoiceConfigEntry = ConfigEntry[MistralVoiceData]

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Repli quand un schéma d'outils ne se convertit pas en JSON Schema :
# un schéma objet ouvert, sûr pour le function-calling Mistral.
_FALLBACK_JSON_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _resolve_system_prompt(options: dict[str, Any]) -> str:
    """Pre-prompt actif : surcharge utilisateur ou valeur par défaut."""
    prompt = str(options.get(CONF_SYSTEM_PROMPT) or "").strip()
    return prompt or resolve_system_prompt({})


def _tool_definition(tool: llm.Tool, custom_serializer: Any | None) -> dict[str, Any]:
    """Convertir un `llm.Tool` au format function-calling Mistral.

    Le schéma des paramètres (`probatio.Schema`) est rendu en JSON
    Schema via `probatio.to_openapi` — conversion infaillible : en cas
    d'échec, le repli est un schéma objet ouvert plutôt qu'une exception.
    """
    try:
        parameters = dict(
            probatio.to_openapi(tool.parameters, custom_serializer=custom_serializer)
        )
    except Exception:
        _LOGGER.warning(
            "Outil %s : schéma de paramètres non convertible, repli générique.",
            tool.name,
        )
        parameters = dict(_FALLBACK_JSON_SCHEMA)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    }


def _delta_text(content: Any) -> str:
    """Contenu texte d'un delta (chaîne, ou liste de parties typées).

    Certains modèles hébergés (ex. `zai-glm-5-3-flash`, raisonnement)
    renvoient `content` sous forme d'une liste de parties : les parties
    texte sont concaténées, les autres ignorées.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            part_type = getattr(part, "type", None)
            if isinstance(part, dict):
                part_type = part.get("type")
            if part_type != "text":
                continue
            text = getattr(part, "text", None)
            if isinstance(part, dict):
                text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _message_content(content: conversation.Content) -> str:
    """Contenu parlable d'un message du ChatLog (chaîne vide si aucun)."""
    return _delta_text(getattr(content, "content", None))


def _convert_content_to_messages(
    chat_content: list[conversation.Content],
) -> list[dict[str, Any]]:
    """Convertir un historique ChatLog en messages Mistral.

    Le pre-prompt système de `chat_log` est converti en message
    `system` ; les contenus assistant avec tool_calls en messages
    `assistant` rejouant leurs appels ; les résultats d'outils en
    messages `tool`. Les contenus assistant sans texte et sans appel
    sont ignorés.
    """
    messages: list[dict[str, Any]] = []
    for content in chat_content:
        if isinstance(content, conversation.ToolResultContent):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": content.tool_call_id,
                    "content": json.dumps(content.result.data, default=str),
                }
            )
            continue
        if isinstance(content, conversation.AssistantContent):
            message: dict[str, Any] = {
                "role": "assistant",
                "content": _message_content(content),
            }
            if content.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.tool_name,
                            "arguments": json.dumps(tool_call.tool_args),
                        },
                    }
                    for tool_call in content.tool_calls
                ]
            if message.get("tool_calls") or message["content"]:
                messages.append(message)
            continue
        role = getattr(content, "role", None)
        if isinstance(role, str) and role in ("system", "user"):
            messages.append({"role": role, "content": _message_content(content)})
    return messages


def _cap_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plafonner l'historique envoyé au modèle (le pre-prompt reste).

    La coupure se fait toujours sur une frontière de tour utilisateur :
    un message `tool` ou un message assistant avec tool_calls ne peut
    jamais se retrouver détaché de sa suite (l'API rejeterait la
    séquence).
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    system, rest = messages[0], messages[1:]
    for index, message in enumerate(rest):
        if message["role"] == "user" and len(rest) - index <= MAX_HISTORY_MESSAGES - 1:
            return [system, *rest[index:]]
    # Aucune frontière exploitable (peu probable) : garder l'ordre strict
    # en tronquant simplement à la limite moins le pre-prompt.
    return [system, *rest[-(MAX_HISTORY_MESSAGES - 1) :]]


def _result_with_speech(
    user_input: conversation.ConversationInput,
    text: str,
    error_response: bool = False,
) -> conversation.ConversationResult:
    """Construire un résultat de conversation (réponse parlée en français)."""
    final_text = text or ERROR_RESPONSE_TEXT
    response = intent.IntentResponse(language=user_input.language or "fr")
    response.async_set_speech(final_text)
    if error_response:
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, final_text)
    return conversation.ConversationResult(
        response=response, conversation_id=user_input.conversation_id
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MistralVoiceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Ajouter l'entité agent de conversation de cette entrée."""
    async_add_entities([MistralVoiceAgentEntity(config_entry)])


class MistralVoiceAgentEntity(conversation.ConversationEntity):
    """Agent de conversation Mistral (entité avec mémoire et streaming)."""

    _attr_has_entity_name = True
    _attr_translation_key = "mistral_agent"
    # Le pipeline peut diffuser les deltas de l'agent vers le TTS.
    _attr_supports_streaming = True
    # L'agent contrôle la maison via les outils Assist.
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, entry: MistralVoiceConfigEntry) -> None:
        """Initialiser l'entité avec l'entrée de configuration."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    @override
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Langues supportées : toutes (le modèle de chat s'adapte)."""
        return MATCH_ALL

    @override
    async def async_added_to_hass(self) -> None:
        """Enregistrement auprès du Pipeline Assist (id d'entrée, compat)."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Désenregistrement de l'agent auprès du Pipeline Assist."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    @override
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Traiter un énoncé : historique + chat/completions en flux."""
        options = self.entry.options
        system_prompt = _resolve_system_prompt(options)
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                llm.LLM_API_ASSIST,
                system_prompt,
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()
        except Exception:
            _LOGGER.warning(
                "Conversation %s : API LLM Assist indisponible.",
                chat_log.conversation_id,
            )
            return _result_with_speech(user_input, "", error_response=True)

        try:
            await self._async_handle_chat_log(chat_log)
        except MistralApiError:
            # Journalisation volontairement générique : ni la clé API, ni
            # le détail de l'exception (qui peut contenir la requête).
            _LOGGER.warning(
                "Conversation %s : échec de l'appel chat/completions Mistral.",
                chat_log.conversation_id,
            )
            return _result_with_speech(user_input, "", error_response=True)
        except HomeAssistantError:
            _LOGGER.warning(
                "Conversation %s : échec côté Home Assistant pendant "
                "l'exécution d'un outil.",
                chat_log.conversation_id,
            )
            return _result_with_speech(user_input, "", error_response=True)

        if chat_log.unresponded_tool_results:
            # Limite d'itérations atteinte sans réponse finale.
            _LOGGER.warning(
                "Conversation %s : limite de %d itérations de tool calling atteinte.",
                chat_log.conversation_id,
                MAX_TOOL_ITERATIONS,
            )
            return _result_with_speech(
                user_input, ERROR_LOOP_LIMIT_TEXT, error_response=True
            )

        try:
            result = conversation.async_get_result_from_chat_log(user_input, chat_log)
        except HomeAssistantError:
            # Pas de réponse parlable exploitable (contenu absent) :
            # réponse polie plutôt qu'une erreur remontée au pipeline.
            return _result_with_speech(user_input, "", error_response=True)
        # Le speech du résultat est un dict {"plain": {"speech": …}} ;
        # une réponse vide (trônquée par max_tokens, contenu absent) est
        # remplacée par la réponse polie.
        plain = (result.response.speech or {}).get("plain") or {}
        spoken = plain.get("speech") or ""
        if not spoken.strip():
            return _result_with_speech(user_input, "", error_response=True)
        return result

    async def _async_handle_chat_log(self, chat_log: conversation.ChatLog) -> None:
        """Générer la réponse en flux, avec la boucle de tool calling.

        Chaque itération diffuse les deltas de la réponse au ChatLog
        (ce qui exécute les tool calls via l'API LLM côté HA) ; l'historique
        Mistral est prolongé avec les contenus produits. La boucle
        s'arrête quand la dernière réponse ne demande plus d'outil.
        """
        model = str(self.entry.options.get(CONF_MODEL) or DEFAULT_MODEL)
        messages = _cap_history(_convert_content_to_messages(chat_log.content))

        converted_tools: list[dict[str, Any]] = []
        if chat_log.llm_api:
            converted_tools = [
                _tool_definition(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            ]
        tool_choice = TOOL_CHOICE_AUTO if converted_tools else None

        for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
            stream = await async_chat_complete_stream(
                self.hass,
                self.entry,
                model=model,
                messages=messages,
                tools=converted_tools or None,
                tool_choice=tool_choice,
                max_tokens=MAX_TOKENS,
            )
            contents: list[conversation.Content] = []
            async for content in chat_log.async_add_delta_content_stream(
                self.entity_id,
                _transform_stream(stream),
            ):
                contents.append(content)

            _LOGGER.debug(
                "Conversation %s : itération %d/%d, %d contenus produits (tools=%s).",
                chat_log.conversation_id,
                iteration,
                MAX_TOOL_ITERATIONS,
                len(contents),
                bool(contents and getattr(contents[0], "tool_calls", None)),
            )
            messages.extend(_convert_content_to_messages(contents))

            if not chat_log.unresponded_tool_results:
                break


async def _transform_stream(
    stream: Any,
) -> AsyncGenerator[dict[str, Any]]:
    """Transformer le flux SSE Mistral en deltas du protocole ChatLog.

    Yield successivement des `AssistantContentDeltaDict` : ouverture du
    message assistant (`role`), deltas de contenu (texte parlé, diffusé
    au pipeline au fil de l'eau), puis — en fin de tour — les tool calls
    assemblés (arguments concaténés par index, comme le protocole
    streaming de l'API). Les deltas ne portent jamais les appels
    d'outils avant que leurs arguments soient complets : le ChatLog
    démarre l'exécution dès leur apparition.
    """
    tool_calls: dict[int, dict[str, str]] = {}
    yield {"role": "assistant"}

    async for event in stream:
        data = getattr(event, "data", None)
        choices = getattr(data, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue
        if text := _delta_text(getattr(delta, "content", None)):
            yield {"content": text}
        for tool_call in getattr(delta, "tool_calls", None) or []:
            index = getattr(tool_call, "index", 0) or 0
            entry = tool_calls.setdefault(index, {"id": "", "name": ""})
            function = getattr(tool_call, "function", None)
            if call_id := getattr(tool_call, "id", None):
                entry["id"] = str(call_id)
            if function is not None:
                if name := getattr(function, "name", None):
                    entry["name"] = str(name)
                if arguments := getattr(function, "arguments", None):
                    entry.setdefault("arguments", "")
                    entry["arguments"] += str(arguments)

    if tool_calls:
        inputs: list[llm.ToolInput] = []
        for entry in tool_calls.values():
            try:
                tool_args = json.loads(entry.get("arguments") or "{}")
                if not isinstance(tool_args, dict):
                    raise ValueError
            except ValueError:
                _LOGGER.warning(
                    "Outil %s : arguments non décodables, arguments vides.",
                    entry["name"],
                )
                tool_args = {}
            inputs.append(
                llm.ToolInput(
                    tool_name=entry["name"] or "",
                    tool_args=tool_args,
                    id=entry["id"] or None,
                )
            )
        yield {"tool_calls": inputs}
