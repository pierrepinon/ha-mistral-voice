"""Fournisseur STT en streaming : flux du satellite Voice PE -> WebSocket Mistral.

Pour chaque énoncé, une session WebSocket temps réel est ouverte vers
``wss://api.mistral.ai/v1/audio/transcriptions/realtime?model=<modèle STT>``
(uniquement du WSS sortant vers api.mistral.ai — ADR 0001) :

1. les chunks audio du pipeline Assist partent en ``input_audio.append`` ;
2. la fin de parole est signalée avec ``input_audio.end`` ;
3. les résultats partiels arrivent en ``transcription.text.delta`` /
   ``transcription.segment`` et le texte complet en ``transcription.done``.

Pas de barge-in côté streaming : les partials sont journalisés puis
ignorés (le pipeline Home Assistant les ignore aussi) ; l'interruption
provient du VAD/AEC du satellite vocal (voir GLOSSARY.md).

La connexion WebSocket est fermée dans un ``finally``, même en cas
d'erreur : une nouvelle connexion est ouverte à chaque énoncé, aucune
session n'est réutilisée entre deux énoncés. La clé API ne vit que dans
``entry.data`` (lue à l'exécution par le SDK) et n'est jamais loguée.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import contextlib
import logging
from typing import override

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from mistralai.extra.realtime import (
    AudioFormat,
    RealtimeConnection,
    RealtimeTranscription,
)

from .api import MistralVoiceData, get_data
from .const import (
    CONF_LANGUAGE,
    CONF_STREAMING_DELAY_MS,
    DEFAULT_LANGUAGE,
    DEFAULT_STREAMING_DELAY_MS,
    DOMAIN,
    MANUFACTURER,
)

# Modèle de transcription temps réel (WebSocket, seul modèle STT en flux
# de l'offre Mistral) ; le modèle de chat est configuré séparément.
STT_REALTIME_MODEL = "voxtral-mini-transcribe-realtime-3"

# Encodage attendu par l'API temps réel pour le PCM linéaire 16 bits
# (valeur de l'enum ``AudioEncoding`` du SDK : pcm_s16le = linear16).
PCM_LINEAR16_ENCODING = "pcm_s16le"

# Types d'événements du protocole realtime (registre ``_MESSAGE_MODELS``
# de mistralai.extra.realtime.connection).
_EVENT_TEXT_DELTA = "transcription.text.delta"
_EVENT_SEGMENT = "transcription.segment"
_EVENT_DONE = "transcription.done"
_EVENT_ERROR = "error"

MistralVoiceConfigEntry = ConfigEntry[MistralVoiceData]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MistralVoiceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Ajouter le fournisseur STT en streaming de cette entrée."""
    async_add_entities([MistralVoiceSTTEntity(hass, config_entry)])


class MistralVoiceSTTEntity(SpeechToTextEntity):
    """Transcription en flux du satellite vocal via l'API temps réel Mistral."""

    _attr_has_entity_name = True
    _attr_translation_key = "mistral_stt"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MistralVoiceConfigEntry,
    ) -> None:
        """Initialiser le fournisseur avec l'entrée de configuration."""
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=STT_REALTIME_MODEL,
            name="Mistral Voice",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    @override
    def supported_languages(self) -> list[str]:
        """Langues supportées : la langue choisie dans les options.

        Lue à chaque accès : un rechargement de l'entrée (déclenché à la
        modification des options) met la valeur à jour.
        """
        language = self.entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        return [language]

    @property
    @override
    def supported_formats(self) -> list[AudioFormats]:
        """Le pipeline Assist (Voice PE) envoie du WAV."""
        return [AudioFormats.WAV]

    @property
    @override
    def supported_codecs(self) -> list[AudioCodecs]:
        """Codec PCM (linéaire 16 bits) pour le WebSocket Mistral."""
        return [AudioCodecs.PCM]

    @property
    @override
    def supported_bit_rates(self) -> list[AudioBitRates]:
        """Le satellite Voice PE émet du PCM 16 bits."""
        return [AudioBitRates.BITRATE_16]

    @property
    @override
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        """Le satellite Voice PE émet du PCM 16 kHz."""
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    @override
    def supported_channels(self) -> list[AudioChannels]:
        """Le flux du satellite est mono."""
        return [AudioChannels.CHANNEL_MONO]

    @override
    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Transcrire le flux audio via une session WebSocket Mistral.

        Une connexion WebSocket par énoncé (jamais réutilisée), fermée
        dans un ``finally`` même en cas d'erreur : pas de fuite de
        connexion. Pas de barge-in côté streaming : les partials
        (``transcription.text.delta``, ``transcription.segment``) sont
        journalisés puis ignorés, le pipeline Home Assistant ignore aussi
        les partials ; l'interruption vient du VAD/AEC du satellite vocal
        (voir GLOSSARY.md).
        """
        language = self.entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        streaming_delay_ms = self.entry.options.get(
            CONF_STREAMING_DELAY_MS, DEFAULT_STREAMING_DELAY_MS
        )
        client = get_data(self.hass, self.entry).get_client()
        realtime = RealtimeTranscription(client.sdk_configuration)

        connection: RealtimeConnection | None = None
        send_task: asyncio.Task[None] | None = None
        # Texte partiel accumulé (utilisé seulement si l'événement
        # ``transcription.done`` n'apporte pas de texte complet).
        delta_parts: list[str] = []
        done_text: str | None = None
        failed = False

        try:
            connection = await realtime.connect(
                model=STT_REALTIME_MODEL,
                audio_format=AudioFormat(
                    encoding=PCM_LINEAR16_ENCODING,
                    sample_rate=metadata.sample_rate,
                ),
                target_streaming_delay_ms=streaming_delay_ms,
                language=language,
            )
            assert connection is not None  # pour le typage de la closure

            async def _send() -> None:
                """Pomper les chunks du pipeline vers le WebSocket."""
                async for chunk in stream:
                    if connection.is_closed:
                        return
                    await connection.send_audio(chunk)
                await connection.flush_audio()
                await connection.end_audio()

            send_task = asyncio.create_task(_send())

            async for event in connection.events():
                event_type = getattr(event, "type", None)
                if event_type == _EVENT_DONE:
                    # ``transcription.done`` porte le texte complet.
                    done_text = getattr(event, "text", None)
                    break
                if event_type == _EVENT_ERROR:
                    failed = True
                    _LOGGER.warning(
                        "Transcription temps réel : erreur renvoyée par "
                        "l'API Mistral pendant la session"
                    )
                    break
                if event_type == _EVENT_TEXT_DELTA:
                    # Partiel : journalisé au niveau debug puis ignoré —
                    # pas de barge-in côté streaming.
                    _LOGGER.debug(
                        "Transcription partielle reçue (barge-in non géré : "
                        "le pipeline ignore les partials)"
                    )
                    delta_parts.append(event.text)
                elif event_type == _EVENT_SEGMENT:
                    # Segment finalisé : remplace les deltas accumulés
                    # (un segment est la version définitive du texte).
                    delta_parts = [event.text]
        except Exception as err:
            _LOGGER.warning(
                "Transcription temps réel : échec de la session WebSocket Mistral (%s)",
                type(err).__name__,
            )
            failed = True
        finally:
            if send_task is not None:
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task
            if connection is not None:
                await connection.close()

        if failed:
            return SpeechResult(text=None, result=SpeechResultState.ERROR)

        text = done_text or ("".join(delta_parts) if delta_parts else None)
        if not text:
            _LOGGER.warning(
                "Transcription temps réel : la session s'est terminée sans "
                "texte transcrit"
            )
            return SpeechResult(text=None, result=SpeechResultState.ERROR)

        return SpeechResult(text=text, result=SpeechResultState.SUCCESS)
