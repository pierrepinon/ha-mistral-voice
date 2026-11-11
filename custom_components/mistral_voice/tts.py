"""Fournisseur TTS : réponse du Pipeline Assist -> POST /v1/audio/speech Mistral.

Le pipeline Assist fournit le texte à parler (sortie de l'agent de
conversation, éventuellement diffusé en flux via ``message_gen``) ;
l'entité le synthétise vers ``POST /v1/audio/speech`` (ADR 0001) :

- **sans flux** (``async_get_tts_audio``, service ``tts.speak``) :
  ``client.audio.speech.complete(...)`` renvoie une réponse audio
  complète en base64 (``audio_data``), décodée en une fois et rendue
  avec son extension ;
- **en flux** (``async_stream_tts_audio``, pipeline Assist /
  satellite vocal) : ``client.audio.speech.complete_async(...,
  stream=True)`` et les événements ``speech.audio.delta`` arrivent au
  fil de l'eau, décodés et diffusés au pipeline — le satellite vocal
  commence à jouer avant la fin de la génération (latence réduite au
  premier audio, silence de fin raccourci).

L'entité annonce le format attendu : l'option Home Assistant
``preferred_format`` est déclarée supportée, donc le pipeline demande le
format du consommateur (WAV 16 kHz/16 bits/mono pour le satellite
Voice PE) et l'API produit directement ce format — jamais de conversion
ffmpeg hasardeuse sur du PCM brut. Valeur demandée inconnue : repli
MP3 (le défaut déclaré).

La langue supportée suit l'option ``language`` de l'entrée (comme le
fournisseur STT) : un rechargement de l'entrée (déclenché à la
modification des options) recrée l'entité et met la valeur à jour. La
voix est optionnelle : l'option ``voice`` contient l'identifiant d'une
voix Mistral (préréglée ou clonée), vide = voix de repli
``DEFAULT_VOICE`` — Voxtral TTS 3 refuse les requêtes sans voix (400
« Either ref_audio or voice must be provided »).

En cas d'échec (API injoignable, modération du contenu…), l'erreur est
journalisée en mode générique — jamais de clé API ni de détail de
requête — puis remontée au pipeline via ``HomeAssistantError``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, ClassVar, override

from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    TtsAudioType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import cached_property
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import (
    MistralApiError,
    MistralVoiceData,
    async_speech_complete,
    async_speech_stream,
)
from .const import (
    CONF_LANGUAGE,
    CONF_VOICE,
    DEFAULT_LANGUAGE,
    DEFAULT_VOICE,
    DOMAIN,
    MANUFACTURER,
    TTS_MODEL,
)

MistralVoiceConfigEntry = ConfigEntry[MistralVoiceData]

_LOGGER = logging.getLogger(__name__)

# Formats Mistral exposables au pipeline via l'option Home Assistant
# `preferred_format` ; toute autre valeur demandée retombe sur MP3.
_TTS_FORMATS = ("wav", "mp3", "flac", "opus")


def _preferred_format(options: dict[str, Any]) -> str:
    """Format de réponse à demander à l'API (option du pipeline, MP3 défaut)."""
    requested = options.get(ATTR_PREFERRED_FORMAT)
    return requested if requested in _TTS_FORMATS else "mp3"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MistralVoiceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Ajouter le fournisseur TTS de cette entrée."""
    async_add_entities([MistralVoiceTTSEntity(hass, config_entry)])


class MistralVoiceTTSEntity(TextToSpeechEntity):
    """Synthèse vocale de la réponse Assist via l'API Mistral."""

    _attr_has_entity_name = True
    _attr_translation_key = "mistral_tts"
    # Le pipeline choisit le format via l'option `preferred_format`
    # (WAV 16 kHz/16 bits/mono pour le satellite Voice PE) : l'entité
    # demande ce format à l'API au lieu de laisser ffmpeg deviner.
    _attr_supported_options: ClassVar[list[str]] = [ATTR_PREFERRED_FORMAT]
    _attr_default_options: ClassVar[dict[str, Any]] = {ATTR_PREFERRED_FORMAT: "mp3"}

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MistralVoiceConfigEntry,
    ) -> None:
        """Initialiser le fournisseur avec l'entrée de configuration."""
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        # Même appareil (service) que les autres entités : sans `model`
        # ni `name` pour ne pas renommer l'appareil déjà déclaré.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
        )

    @cached_property
    @override
    def supported_languages(self) -> list[str]:
        """Langues supportées : la langue choisie dans les options.

        Comme le fournisseur STT, l'entité ne sert que le pipeline Assist
        configuré sur la même langue ; un rechargement de l'entrée recrée
        l'entité et remet la valeur à jour.
        """
        language = self.entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        return [language]

    @cached_property
    @override
    def default_language(self) -> str:
        """Langue par défaut : la langue choisie dans les options."""
        language = self.entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        return str(language)

    @override
    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Synthétiser le texte via POST /v1/audio/speech Mistral (sans flux).

        Un appel HTTPS par annonce (pas de session réutilisée) ; l'audio
        base64 renvoyé par l'API est décodé et rendu avec son extension.
        Les erreurs remontent au pipeline en ``HomeAssistantError`` sans
        jamais exposer la clé ni la requête.
        """
        voice_id = str(self.entry.options.get(CONF_VOICE) or DEFAULT_VOICE)
        response_format = _preferred_format(options)
        _LOGGER.debug(
            "Synthèse vocale : annonce de %d caractères (langue %s).",
            len(message),
            language,
        )
        try:
            response = await async_speech_complete(
                self.hass,
                self.entry,
                model=TTS_MODEL,
                text=message,
                response_format=response_format,
                voice_id=voice_id or None,
            )
        except MistralApiError as err:
            # Journalisation volontairement générique : ni la clé API, ni
            # le texte parlé, ni le détail de l'exception ne sont logués.
            _LOGGER.warning("Synthèse vocale : échec de l'appel audio/speech Mistral.")
            raise HomeAssistantError("La synthèse vocale Mistral a échoué.") from err

        audio_data = getattr(response, "audio_data", None)
        if not audio_data:
            _LOGGER.warning(
                "Synthèse vocale : la réponse de l'API Mistral ne contient aucun audio."
            )
            raise HomeAssistantError("La synthèse vocale Mistral a échoué.")

        try:
            return response_format, base64.b64decode(audio_data)
        except ValueError as err:
            # `audio_data` mal formé (base64 invalide) : erreur générique,
            # jamais le contenu de la réponse dans les logs.
            _LOGGER.warning(
                "Synthèse vocale : l'audio renvoyé par l'API Mistral est indécodable."
            )
            raise HomeAssistantError("La synthèse vocale Mistral a échoué.") from err

    @override
    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Synthétiser le texte en flux (pipeline Assist / satellite vocal).

        Le texte est consommé intégralement depuis ``request.message_gen``
        (le pipeline y diffuse la réponse de l'agent), puis l'audio
        arrive en événements ``speech.audio.delta``, décodés au fil de
        l'eau et diffusés au pipeline chunk par chunk.
        """
        message = "".join([chunk async for chunk in request.message_gen])
        voice_id = str(self.entry.options.get(CONF_VOICE) or DEFAULT_VOICE)
        response_format = _preferred_format(request.options)
        _LOGGER.debug(
            "Synthèse vocale en flux : annonce de %d caractères (langue %s, "
            "format %s).",
            len(message),
            request.language,
            response_format,
        )
        try:
            stream = await async_speech_stream(
                self.hass,
                self.entry,
                model=TTS_MODEL,
                text=message,
                response_format=response_format,
                voice_id=voice_id or None,
            )
        except MistralApiError as err:
            _LOGGER.warning("Synthèse vocale : échec de l'appel audio/speech Mistral.")
            raise HomeAssistantError("La synthèse vocale Mistral a échoué.") from err

        return TTSAudioResponse(response_format, self._async_audio_stream(stream))

    async def _async_audio_stream(self, stream: Any) -> Any:
        """Diffuser les chunks audio (base64 décodés) au fil de l'eau.

        Une erreur en cours de flux remonte la même erreur générique
        (jamais de détail de requête), quelle que soit l'étape.
        """
        try:
            async for event in stream:
                data = getattr(event, "data", None)
                audio_data = getattr(data, "audio_data", None)
                if not audio_data:
                    continue
                try:
                    yield base64.b64decode(audio_data)
                except ValueError as err:
                    raise HomeAssistantError(
                        "La synthèse vocale Mistral a échoué."
                    ) from err
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.warning(
                "Synthèse vocale : échec de la lecture du flux audio Mistral."
            )
            raise HomeAssistantError("La synthèse vocale Mistral a échoué.") from err
