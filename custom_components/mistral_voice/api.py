"""Client API Mistral partagé par toute l'intégration.

Contrat API utilisé (mistralai[realtime]==3.7.0, HTTPS/WSS sortant
uniquement vers api.mistral.ai — ADR 0001) :

- `client.models.list()` → GET /v1/models (alimente le sélecteur
  dynamique de modèles, jamais de liste statique en dur) ;
- `client.chat.complete(...)` → POST /v1/chat/completions
  (l'agent de conversation, tickets suivants) ;
- `client.chat.stream_async(...)` → POST /v1/chat/completions en flux
  SSE (l'agent de conversation en flux diffuse les deltas au pipeline) ;
- `client.audio.speech.complete(...)` → POST /v1/audio/speech
  (la synthèse vocale sans flux, réponse en audio base64 `audio_data`) ;
- `client.audio.speech.complete_async(..., stream=True)` → POST
  /v1/audio/speech en flux SSE (synthèse vocale en flux : événements
  `speech.audio.delta`).

Les méthodes `*_async` du SDK itèrent nativement en asyncio
(`EventStreamAsync`) : les chemins synchrones restent hors de la boucle
event (`async_add_executor_job`), les chemins en flux consomment un
itérateur asynchrone.

La clé API ne vit que dans `entry.data[CONF_API_KEY]` : elle est passée
au constructeur du SDK et n'est jamais loguée, relue, ni incluse dans un
message d'erreur. Pour d'éventuels appels HTTP directs utiles, la session
aiohttp partagée de Home Assistant est réutilisée
(`async_get_clientsession(hass)`) : aucune `ClientSession` dédiée
n'est créée.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from mistralai.client import (
    Mistral,  # namespace 3.7.0 : classe dans mistralai.client.sdk
)

from .const import CONF_API_KEY, DOMAIN

# Suffixe d'alias « stable » privilégié par le tri du sélecteur de modèles.
MODEL_LATEST_SUFFIX = "-latest"


class MistralApiError(Exception):
    """L'API Mistral est injoignable ou a renvoyé une erreur.

    Les messages d'exception du SDK peuvent contenir des détails de
    requête : on n'expose qu'une erreur générique (jamais la clé).
    """


@dataclass(slots=True)
class MistralVoiceData:
    """État partagé d'une entrée de configuration Mistral Voice.

    Stocké dans `hass.data[DOMAIN][entry.entry_id]`. La construction du
    client est paresseuse : le SDK ne fait aucune validation réseau (ni
    requête) au chargement de l'entrée, seulement au premier appel.
    """

    entry: ConfigEntry
    client: Mistral | None = field(default=None, repr=False)

    def get_client(self) -> Mistral:
        """Construire (une seule fois) le client SDK de cette entrée.

        La clé est lue depuis `entry.data` au dernier moment : elle
        n'apparaît jamais dans les logs (le client est masqué dans
        l'attribut via `repr=False`).
        """
        if self.client is None:
            self.client = Mistral(api_key=self.entry.data[CONF_API_KEY])
        return self.client


def get_data(hass: HomeAssistant, entry: ConfigEntry) -> MistralVoiceData:
    """Récupérer (ou créer paresseusement) l'état partagé de l'entrée."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    data = domain_data.get(entry.entry_id)
    if data is None:
        data = MistralVoiceData(entry)
        domain_data[entry.entry_id] = data
    return data


def async_get_shared_http_session(hass: HomeAssistant) -> Any:
    """Session aiohttp partagée de HA pour d'éventuels appels HTTP directs.

    Réservée aux endpoints non couverts par le SDK (ADR 0001) : toujours
    la session partagée de Home Assistant, jamais une session dédiée.
    """
    return async_get_clientsession(hass)


async def async_list_models(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    """GET /v1/models via `client.models.list()` (exécuté hors boucle event)."""
    client = get_data(hass, entry).get_client()
    try:
        return await hass.async_add_executor_job(client.models.list)
    except Exception as err:
        raise MistralApiError(
            "GET /v1/models a échoué : API injoignable ou réponse invalide."
        ) from err


def filter_chat_models(model_list: Any) -> list[str]:
    """Filtrer une réponse de GET /v1/models aux modèles de chat.

    - garde les cartes dont les capacités annoncent `completion_chat` ;
    - privilégie les alias « -latest » (triés en premier, puis ordre
      alphabétique) ;
    - déduplique et ignore les entrées sans identifiant exploitable.

    `model_list` est l'objet renvoyé par `client.models.list()`
    (conteneur avec `.data`, cartes avec `.id` et `.capabilities`).
    """
    cards = getattr(model_list, "data", None) or []
    ids: set[str] = set()
    for card in cards:
        capabilities = getattr(card, "capabilities", None)
        if capabilities is None or not getattr(capabilities, "completion_chat", False):
            continue
        model_id = getattr(card, "id", None)
        if isinstance(model_id, str) and model_id:
            ids.add(model_id)
    return sorted(
        ids,
        key=lambda model_id: (
            not model_id.endswith(MODEL_LATEST_SUFFIX),
            model_id,
        ),
    )


async def async_list_chat_model_ids(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[str]:
    """Liste triée des modèles de chat : GET /v1/models, alias -latest d'abord."""
    model_list = await async_list_models(hass, entry)
    return filter_chat_models(model_list)


async def async_chat_complete(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    model: str,
    messages: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    tools: Iterable[Any] | None = None,
    tool_choice: str | None = None,
    max_tokens: int | None = None,
) -> Any:
    """POST /v1/chat/completions via `client.chat.complete` (hors boucle event).

    `tools`, `tool_choice` et `max_tokens` sont transmis au SDK tels quels
    lorsqu'ils sont renseignés (agent de conversation, tool calling).
    """
    client = get_data(hass, entry).get_client()
    call_kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        call_kwargs["tools"] = tools
    if tool_choice:
        call_kwargs["tool_choice"] = tool_choice
    if max_tokens is not None:
        call_kwargs["max_tokens"] = max_tokens
    try:
        return await hass.async_add_executor_job(
            partial(client.chat.complete, **call_kwargs)
        )
    except Exception as err:
        raise MistralApiError(
            "POST /v1/chat/completions a échoué : API injoignable ou réponse invalide."
        ) from err


async def async_speech_complete(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    model: str,
    text: str,
    response_format: str,
    voice_id: str | None = None,
) -> Any:
    """POST /v1/audio/speech via `client.audio.speech.complete` (hors boucle event).

    La réponse du SDK porte l'audio encodé en base64 (`audio_data`).
    `voice_id` n'est transmis que s'il est renseigné : vide, l'API
    utilise sa voix par défaut.
    """
    client = get_data(hass, entry).get_client()
    call_kwargs: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": response_format,
    }
    if voice_id:
        call_kwargs["voice_id"] = voice_id
    try:
        return await hass.async_add_executor_job(
            partial(client.audio.speech.complete, **call_kwargs)
        )
    except Exception as err:
        raise MistralApiError(
            "POST /v1/audio/speech a échoué : API injoignable ou réponse invalide."
        ) from err


async def async_chat_complete_stream(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    model: str,
    messages: Iterable[Mapping[str, Any]],
    tools: Iterable[Any] | None = None,
    tool_choice: str | None = None,
    max_tokens: int | None = None,
) -> Any:
    """POST /v1/chat/completions en flux SSE via `client.chat.stream_async`.

    Renvoie un `EventStreamAsync` d'événements `CompletionEvent`
    (itérable en asyncio) ; les erreurs d'ouverture sont enrobées en
    `MistralApiError`, celles de lecture le sont par l'appelant.
    """
    client = get_data(hass, entry).get_client()
    call_kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        call_kwargs["tools"] = tools
    if tool_choice:
        call_kwargs["tool_choice"] = tool_choice
    if max_tokens is not None:
        call_kwargs["max_tokens"] = max_tokens
    try:
        return await client.chat.stream_async(**call_kwargs)
    except Exception as err:
        raise MistralApiError(
            "POST /v1/chat/completions a échoué : API injoignable ou réponse invalide."
        ) from err


async def async_speech_stream(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    model: str,
    text: str,
    response_format: str,
    voice_id: str | None = None,
) -> Any:
    """POST /v1/audio/speech en flux via `client.audio.speech.complete_async`.

    Renvoie un `EventStreamAsync` d'événements `SpeechStreamEvents`
    (delta `speech.audio.delta` puis `speech.audio.done`). `voice_id`
    n'est transmis que s'il est renseigné : vide, l'API utilise sa voix
    par défaut.
    """
    client = get_data(hass, entry).get_client()
    call_kwargs: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": response_format,
        "stream": True,
    }
    if voice_id:
        call_kwargs["voice_id"] = voice_id
    try:
        return await client.audio.speech.complete_async(**call_kwargs)
    except Exception as err:
        raise MistralApiError(
            "POST /v1/audio/speech a échoué : API injoignable ou réponse invalide."
        ) from err
