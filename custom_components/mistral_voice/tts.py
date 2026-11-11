"""Fournisseur TTS : réponse du Pipeline Assist -> POST /v1/audio/speech Mistral.

Le pipeline Assist fournit le texte à parler (sortie de l'agent de
conversation) ; l'entité le synthétise en un seul appel HTTPS sortant vers
``POST /v1/audio/speech`` (ADR 0001) via
``client.audio.speech.complete(model=voxtral-mini-tts-3-0, input=…)``.
L'API renvoie l'audio encodé en base64 (``audio_data``) au format MP3 :
décodé en bytes et rendu au pipeline comme tout fichier TTS, avec
l'extension ``mp3`` — le satellite vocal joue ensuite l'annonce.

La langue supportée suit l'option ``language`` de l'entrée (comme le
fournisseur STT) : un rechargement de l'entrée (déclenché à la
modification des options) recrée l'entité et met la valeur à jour. La
voix est optionnelle : l'option ``voice`` contient l'identifiant d'une
voix Mistral (préréglée ou clonée), vide = voix par défaut de l'API.

En cas d'échec (API injoignable, modération du contenu…), l'erreur est
journalisée en mode générique — jamais de clé API ni de détail de
requête — puis remontée au pipeline via ``HomeAssistantError``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, override

from homeassistant.components.tts import TextToSpeechEntity, TtsAudioType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import cached_property
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import MistralApiError, MistralVoiceData, async_speech_complete
from .const import (
    CONF_LANGUAGE,
    CONF_VOICE,
    DEFAULT_LANGUAGE,
    DEFAULT_VOICE,
    DOMAIN,
    MANUFACTURER,
    TTS_AUDIO_FORMAT,
    TTS_MODEL,
)

MistralVoiceConfigEntry = ConfigEntry[MistralVoiceData]

_LOGGER = logging.getLogger(__name__)


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

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MistralVoiceConfigEntry,
    ) -> None:
        """Initialiser le fournisseur avec l'entrée de configuration."""
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        # Même appareil (service) que le fournisseur STT : sans `model` ni
        # `name` pour ne pas renommer ni retoucher l'appareil déclaré là-bas.
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
        """Synthétiser le texte via POST /v1/audio/speech Mistral.

        Un appel HTTPS par annonce (pas de session réutilisée) ; l'audio
        base64 renvoyé par l'API est décodé et rendu avec l'extension
        ``mp3``. Les erreurs remontent au pipeline en
        ``HomeAssistantError`` sans jamais exposer la clé ni la requête.
        """
        voice_id = str(self.entry.options.get(CONF_VOICE, DEFAULT_VOICE))
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
                response_format=TTS_AUDIO_FORMAT,
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
            return TTS_AUDIO_FORMAT, base64.b64decode(audio_data)
        except ValueError as err:
            # `audio_data` mal formé (base64 invalide) : erreur générique,
            # jamais le contenu de la réponse dans les logs.
            _LOGGER.warning(
                "Synthèse vocale : l'audio renvoyé par l'API Mistral est indécodable."
            )
            raise HomeAssistantError("La synthèse vocale Mistral a échoué.") from err
