"""Agent de conversation Mistral pour le Pipeline Assist.

Fonctionnement pour chaque énoncé (`async_process`) :

1. récupération de l'API LLM Assist de Home Assistant via
   `llm.async_get_api(hass, llm.LLM_API_ASSIST, user_input.as_llm_context(DOMAIN))`
   — le mécanisme d'outils de HA n'est PAS réimplémenté ;
2. conversion des outils (`llm.Tool`, schéma `probatio.Schema`) au format
   function-calling de Mistral (`{"type": "function", "function": {…}}`) ;
3. appel `POST /v1/chat/completions` via le client du ticket #4
   (`api.async_chat_complete`), `tool_choice="auto"`, `max_tokens` capé
   (`const.MAX_TOKENS`) ;
4. boucle de tool calling : chaque `tool_call` est exécuté côté HA via
   `api_instance.async_call_tool(llm.ToolInput(...))`, le résultat repart
   en message `{"role": "tool", "tool_call_id": …, "content": …}` puis la
   conversation reboucle (limite `const.MAX_TOOL_ITERATIONS`) ;
5. le texte final (dernier message assistant sans tool_call) alimente une
   `IntentResponse` française, dans une `ConversationResult`.

En cas d'erreur (API injoignable, outil en échec), une réponse polie en
français est renvoyée : aucune exception, aucun détail de requête et
surtout jamais la clé API ne sortent de ce module.

Les définitions d'outils converties et le pre-prompt actif sont mis en
cache (réutilisation du même objet dict / même valeur lru) : les requêtes
successives sont byte-stables. Le parcours est instrumenté en log debug
(modèle, itération, finish_reason, tool_calls, tailles de contenu) pour
permettre le diagnostic sans jamais y loguer de secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
import logging
from types import MappingProxyType
from typing import Any, Literal, override

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
import probatio

from .api import MistralApiError, async_chat_complete
from .config_flow import resolve_system_prompt
from .const import (
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    DOMAIN,
    ERROR_LOOP_LIMIT_TEXT,
    ERROR_RESPONSE_TEXT,
    MAX_TOKENS,
    MAX_TOOL_ITERATIONS,
    TOOL_CHOICE_AUTO,
)

_LOGGER = logging.getLogger(__name__)

# Repli quand un schéma d'outils ne se convertit pas en JSON Schema :
# un schéma objet ouvert, sûr pour le function-calling Mistral.
_FALLBACK_JSON_SCHEMA: dict[str, Any] = MappingProxyType(
    {"type": "object", "properties": {}, "additionalProperties": False}
)


@lru_cache(maxsize=8)
def _resolve_system_prompt_cached(system_prompt_option: str | None) -> str:
    """Pre-prompt actif mémorisé (byte-stable entre les appels).

    La surcharge utilisateur (`option system_prompt` non vide) prime ;
    sinon le pre-prompt par défaut de `const.py` est utilisé (même
    comportement que `resolve_system_prompt` du config flow). La mémoire
    est indexée par la valeur de l'option : même option -> même objet str.
    """
    return resolve_system_prompt(
        MappingProxyType({CONF_SYSTEM_PROMPT: str(system_prompt_option or "")})
    )


def _tool_definition(tool: llm.Tool) -> dict[str, Any]:
    """Convertir un `llm.Tool` au format function-calling Mistral.

    Le schéma des paramètres (`probatio.Schema`) est rendu en JSON Schema
    via `probatio.to_openapi` — conversion infaillible : en cas d'échec,
    le repli est un schéma objet ouvert plutôt qu'une exception.
    """
    try:
        parameters = dict(probatio.to_openapi(tool.parameters))
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


def _tool_digest(tool_def: Mapping[str, Any]) -> str:
    """Empreinte byte-stable d'une définition d'outil (tri des clés)."""
    return json.dumps(tool_def, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(slots=True)
class _CachedToolDef:
    """Définition d'outil convertie, avec l'empreinte de sa source.

    Réutilisée entre les appels tant que l'empreinte (description +
    schéma des paramètres) ne change pas : la même instance de dict est
    renvoyée, donc les requêtes successives sont byte-stables.
    """

    digest: str
    definition: dict[str, Any]


class MistralVoiceAgent(conversation.AbstractConversationAgent):
    """Agent de conversation Mistral (enregistré programmatiquement).

    L'agent n'est pas une entité `conversation` : il est enregistré au
    setup de l'entrée via `conversation.async_set_agent` puis
    désenregistré au unload, comme le pipeline Assist l'attend pour un
    agent de service (voir google_generative_ai_conversation / openai).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialiser l'agent pour une entrée de configuration."""
        self.hass = hass
        self.entry = entry
        # Cache des définitions d'outils converties, indexé par nom d'outil.
        self._tool_defs: dict[str, _CachedToolDef] = {}

    @property
    @override
    def supported_languages(self) -> Literal["*"]:
        """Langues supportées : toutes (le modèle de chat s'adapte)."""
        return MATCH_ALL

    def convert_tool(self, tool: llm.Tool) -> dict[str, Any]:
        """Conversion d'outil avec cache byte-stable (par nom d'outil)."""
        fresh = _tool_definition(tool)
        fresh_digest = _tool_digest(fresh)
        cached = self._tool_defs.get(tool.name)
        if cached is None or cached.digest != fresh_digest:
            cached = _CachedToolDef(digest=fresh_digest, definition=fresh)
            self._tool_defs[tool.name] = cached
        return cached.definition

    @override
    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Traiter un énoncé : chat/completions (+ boucle tool calling)."""
        options = self.entry.options
        model = str(options.get(CONF_MODEL) or DEFAULT_MODEL)
        system_prompt = _resolve_system_prompt_cached(
            str(options.get(CONF_SYSTEM_PROMPT) or "") or None
        )

        try:
            api_instance = await llm.async_get_api(
                self.hass,
                llm.LLM_API_ASSIST,
                user_input.as_llm_context(DOMAIN),
            )
        except Exception:
            _LOGGER.warning(
                "Conversation %s : API LLM Assist indisponible.", user_input.agent_id
            )
            return _error_result(user_input)

        converted_tools = [self.convert_tool(tool) for tool in api_instance.tools]
        tool_choice = TOOL_CHOICE_AUTO if converted_tools else None
        _LOGGER.debug(
            "Conversation %s : modèle %s, %d outils exposés, énoncé de %d caractères.",
            user_input.agent_id,
            model,
            len(converted_tools),
            len(user_input.text),
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input.text},
        ]

        try:
            for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
                response = await async_chat_complete(
                    self.hass,
                    self.entry,
                    model=model,
                    messages=messages,
                    tools=converted_tools or None,
                    tool_choice=tool_choice,
                    max_tokens=MAX_TOKENS,
                )
                message = _first_message(response)
                tool_calls = _message_tool_calls(message)
                _LOGGER.debug(
                    "Conversation %s : itération %d/%d, finish_reason=%s, "
                    "%d tool_calls, contenu de %d caractères.",
                    user_input.agent_id,
                    iteration,
                    MAX_TOOL_ITERATIONS,
                    _finish_reason(response),
                    len(tool_calls),
                    len(_message_content(message)),
                )

                if not tool_calls:
                    return _result_with_speech(user_input, _message_content(message))

                # L'assistant demande des outils : rejouer son message avec
                # ses tool_calls, puis ajouter les résultats en messages `tool`.
                messages.append(
                    {
                        "role": "assistant",
                        "content": _message_content(message),
                        "tool_calls": tool_calls,
                    }
                )
                for tool_call in tool_calls:
                    content = await self._async_execute_tool(api_instance, tool_call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": _field(tool_call, "id") or "",
                            "content": content,
                        }
                    )
        except MistralApiError:
            # Journalisation volontairement générique : ni la clé API, ni
            # le détail de l'exception (qui peut contenir la requête).
            _LOGGER.warning(
                "Conversation %s : échec de l'appel chat/completions Mistral.",
                user_input.agent_id,
            )
            return _error_result(user_input)
        except Exception:
            _LOGGER.warning(
                "Conversation %s : échec inattendu du traitement de l'énoncé.",
                user_input.agent_id,
            )
            return _error_result(user_input)

        # Limite d'itérations atteinte sans réponse finale : réponse de repli.
        _LOGGER.warning(
            "Conversation %s : limite de %d itérations de tool calling atteinte.",
            user_input.agent_id,
            MAX_TOOL_ITERATIONS,
        )
        return _result_with_speech(
            user_input, ERROR_LOOP_LIMIT_TEXT, error_response=True
        )

    async def _async_execute_tool(self, api_instance: Any, tool_call: Any) -> str:
        """Exécuter un tool_call côté HA et sérialiser le résultat en JSON.

        Le mécanisme HA (`async_call_tool`) est utilisé tel quel ; en cas
        d'échec, un contenu d'erreur générique part au modèle (pas de
        détail d'exception).
        """
        function = _field(tool_call, "function") or {}
        name = _field(function, "name") or ""
        raw_arguments = _field(function, "arguments") or ""

        try:
            tool_args = _parse_tool_args(raw_arguments)
        except ValueError:  # JSONDecodeError est une sous-classe de ValueError
            _LOGGER.warning("Outil %s : arguments non décodables.", name)
            return json.dumps({"error": "invalid_arguments"})

        try:
            result = await api_instance.async_call_tool(
                llm.ToolInput(tool_name=name, tool_args=tool_args)
            )
        except Exception:
            _LOGGER.warning("Outil %s : échec d'exécution côté Home Assistant.", name)
            return json.dumps({"error": "tool_execution_failed"})

        data = _field(result, "data")
        data = data if data is not None else {}
        content = json.dumps(data, default=str)
        _LOGGER.debug(
            "Outil %s : exécuté, résultat de %d caractères.", name, len(content)
        )
        return content


def _parse_tool_args(raw_arguments: Any) -> dict[str, Any]:
    """Décoder les arguments d'un tool_call (dict, ou chaîne JSON)."""
    if isinstance(raw_arguments, Mapping):
        return dict(raw_arguments)
    if isinstance(raw_arguments, str) and raw_arguments.strip():
        decoded = json.loads(raw_arguments)
        if isinstance(decoded, Mapping):
            return dict(decoded)
    return {}


def _field(obj: Any, key: str) -> Any:
    """Lire un champ d'un modèle SDK (attribut) ou d'un dict, sans supposer."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _first_message(response: Any) -> Any:
    """Message du premier choix de la réponse chat/completions."""
    choices = _field(response, "choices") or []
    if not choices:
        return None
    return _field(choices[0], "message")


def _finish_reason(response: Any) -> str:
    """`finish_reason` du premier choix de la réponse (vide si absent).

    Informatif pour le diagnostic (réponse tronquée par `max_tokens` :
    `length` avec un contenu vide, typique des modèles de raisonnement).
    """
    choices = _field(response, "choices") or []
    if not choices:
        return ""
    reason = _field(choices[0], "finish_reason")
    return reason if isinstance(reason, str) else ""


def _message_content(message: Any) -> str:
    """Contenu texte d'un message (str vide si absent)."""
    content = _field(message, "content")
    return content if isinstance(content, str) else ""


def _message_tool_calls(message: Any) -> list[dict[str, Any]]:
    """Tool_calls normalisés d'un message (liste vide si aucun)."""
    raw = _field(message, "tool_calls") or []
    normalised: list[dict[str, Any]] = []
    for tool_call in raw:
        function = _field(tool_call, "function") or {}
        normalised.append(
            {
                "id": _field(tool_call, "id") or "",
                "type": "function",
                "function": {
                    "name": _field(function, "name") or "",
                    "arguments": _field(function, "arguments") or "",
                },
            }
        )
    return normalised


def _result_with_speech(
    user_input: conversation.ConversationInput,
    text: str,
    error_response: bool = False,
) -> conversation.ConversationResult:
    """Construire le résultat de conversation (réponse parlée en français).

    Un texte vide est remplacé par la réponse d'erreur polie. En cas
    d'erreur, la réponse est typée `IntentResponseType.ERROR`.
    """
    final_text = text or ERROR_RESPONSE_TEXT
    response = intent.IntentResponse(user_input.language or DEFAULT_LANGUAGE)
    response.async_set_speech(final_text)
    if error_response:
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, final_text)
    return conversation.ConversationResult(
        response=response, conversation_id=user_input.conversation_id
    )


def _error_result(
    user_input: conversation.ConversationInput,
) -> conversation.ConversationResult:
    """Réponse d'erreur polie en français (jamais de détail d'exception)."""
    return _result_with_speech(user_input, "", error_response=True)
