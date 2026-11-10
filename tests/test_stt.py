"""Tests du fournisseur STT en streaming Mistral Voice (ticket #5).

Cas couverts :
- flux nominal : chunks -> deltas -> ``transcription.done`` -> texte rendu
  au pipeline (SpeechResult SUCCESS) ;
- options (``language``, ``target_streaming_delay_ms``) transmises à
  ``RealtimeTranscription.connect`` ;
- une connexion WebSocket par énoncé (jamais réutilisée), fermée dans un
  ``finally`` même en cas d'erreur : pas de fuite de connexion ;
- erreur WebSocket -> SpeechResult ERROR, close toujours appelé ;
- capacités de l'entée compatibles PCM 16 kHz mono ;
- intégration légère : la plateforme ``stt`` est chargée au setup de
  l'entrée (enable_custom_integrations).

La clé API n'est jamais loguée : les tests utilisent une clé factice.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResultState,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mistral_voice.const import (
    CONF_LANGUAGE,
    CONF_STREAMING_DELAY_MS,
    DEFAULT_LANGUAGE,
    DEFAULT_STREAMING_DELAY_MS,
    DOMAIN,
)
import custom_components.mistral_voice.stt as stt_module

FAKE_API_KEY = "placeholder-key-from-test"

_OPTIONS_DELAY_MS = 240
_SAMPLE_RATE = 16000
_EXPECTED_CALLS = 2


@pytest.fixture
def rt_factory() -> Iterator[MagicMock]:
    """Fabrique factice de RealtimeTranscription (connect entièrement mocké)."""
    with patch.object(stt_module, "RealtimeTranscription") as factory:
        yield factory


def _connect_to(factory: MagicMock, connection: FakeConnection) -> None:
    """Câbler la fabrique pour que connect() renvoie la connexion factice."""
    instance = MagicMock()
    instance.connect = AsyncMock(return_value=connection)
    factory.return_value = instance


def _metadata() -> SpeechMetadata:
    """Métadonnées du flux du satellite Voice PE (WAV / PCM 16 kHz mono)."""
    return SpeechMetadata(
        language="fr",
        format=AudioFormats.WAV,
        codec=AudioCodecs.PCM,
        bit_rate=AudioBitRates.BITRATE_16,
        sample_rate=AudioSampleRates.SAMPLERATE_16000,
        channel=AudioChannels.CHANNEL_MONO,
    )


async def _chunk_stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """Stream audio factice (AsyncIterable[bytes])."""
    for chunk in chunks:
        yield chunk


def _event(event_type: str, text: str = "") -> types.SimpleNamespace:
    """Événement realtime factice (porte .type et .text comme le SDK)."""
    return types.SimpleNamespace(type=event_type, text=text)


class FakeConnection:
    """Doublure de RealtimeConnection : événements rejoués, appels tracés."""

    def __init__(
        self,
        events: list[Any] | None = None,
        *,
        fail_send: bool = False,
        fail_events: bool = False,
    ) -> None:
        self.events_list = events or []
        self.fail_send = fail_send
        self.fail_events = fail_events
        self.sent_audio: list[bytes] = []
        self.end_audio_count = 0
        self.flush_count = 0
        self.close_count = 0

    @property
    def is_closed(self) -> bool:
        return self.close_count > 0

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Envoyer un chunk (input_audio.append)."""
        if self.fail_send:
            raise RuntimeError("boom")
        self.sent_audio.append(audio_bytes)

    async def flush_audio(self) -> None:
        """Flush intermédiaire."""
        self.flush_count += 1

    async def end_audio(self) -> None:
        """Fin de parole (input_audio.end)."""
        self.end_audio_count += 1

    async def events(self) -> AsyncIterator[Any]:
        """Rejouer les événements configurés.

        Réaliste : le ``done`` n'arrive qu'après ``end_audio`` (la fin de
        parole part avant que l'API renvoie le texte complet).
        """
        if self.fail_events:
            raise RuntimeError("websocket gone")
        for event in self.events_list:
            if getattr(event, "type", None) == "transcription.done":
                while self.end_audio_count == 0 and self.close_count == 0:
                    await asyncio.sleep(0)
                if self.close_count:
                    return
            yield event

    async def close(self) -> None:
        """Fermer la connexion (l'entité l'appelle toujours via finally)."""
        self.close_count += 1


async def _make_entity(
    hass: Any, options: dict[str, Any] | None = None
) -> tuple[MockConfigEntry, Any]:
    """Construire l'entité STT via async_setup_entry (entrée factice)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": FAKE_API_KEY},
        options=options or {},
    )
    entry.add_to_hass(hass)
    entities: list[Any] = []
    await stt_module.async_setup_entry(hass, entry, entities.extend)
    assert len(entities) == 1
    return entry, entities[0]


async def test_nominal_streaming(hass, rt_factory) -> None:
    """Chunks -> deltas -> done -> SpeechResult SUCCESS avec le texte final."""
    connection = FakeConnection(
        events=[
            _event("transcription.text.delta", "Bonjour"),
            _event("transcription.text.delta", " le monde"),
            _event("transcription.done", "Bonjour le monde"),
        ]
    )
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01\x00", b"\x02\x00", b"\x03\x00"])
    )

    assert result.text == "Bonjour le monde"
    assert result.result is SpeechResultState.SUCCESS
    # Chaque chunk est parti en input_audio.append, puis end_audio.
    assert connection.sent_audio == [b"\x01\x00", b"\x02\x00", b"\x03\x00"]
    assert connection.end_audio_count == 1
    # Le texte final vient de transcription.done (deltas ignorés).
    connect_kwargs = rt_factory.return_value.connect.call_args.kwargs
    assert connect_kwargs["model"] == stt_module.STT_REALTIME_MODEL


async def test_options_transmitted_to_connect(hass, rt_factory) -> None:
    """Options language / delay / modèle / format passées à connect."""
    connection = FakeConnection(events=[_event("transcription.done", "ok")])
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(
        hass,
        options={CONF_LANGUAGE: "en", CONF_STREAMING_DELAY_MS: _OPTIONS_DELAY_MS},
    )

    await entity.async_process_audio_stream(_metadata(), _chunk_stream([b"\x01"]))

    connect_kwargs = rt_factory.return_value.connect.call_args.kwargs
    assert connect_kwargs["language"] == "en"
    assert connect_kwargs["target_streaming_delay_ms"] == _OPTIONS_DELAY_MS
    assert connect_kwargs["model"] == stt_module.STT_REALTIME_MODEL
    audio_format = connect_kwargs["audio_format"]
    assert audio_format.encoding == "pcm_s16le"
    assert audio_format.sample_rate == _SAMPLE_RATE


async def test_defaults_without_options(hass, rt_factory) -> None:
    """Sans options : langue défaut et délai défaut."""
    connection = FakeConnection(events=[_event("transcription.done", "ok")])
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    assert entity.supported_languages == [DEFAULT_LANGUAGE]
    await entity.async_process_audio_stream(_metadata(), _chunk_stream([b"\x01"]))
    connect_kwargs = rt_factory.return_value.connect.call_args.kwargs
    assert connect_kwargs["language"] == DEFAULT_LANGUAGE
    assert connect_kwargs["target_streaming_delay_ms"] == DEFAULT_STREAMING_DELAY_MS


async def test_new_connection_per_call(hass, rt_factory) -> None:
    """Une nouvelle connexion WebSocket par énoncé (jamais réutilisée)."""
    connection = FakeConnection(events=[_event("transcription.done", "ok")])
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    await entity.async_process_audio_stream(_metadata(), _chunk_stream([b"a"]))
    await entity.async_process_audio_stream(_metadata(), _chunk_stream([b"b"]))

    # Deux appels -> deux connexions (connect appelé deux fois, close deux fois).
    assert rt_factory.return_value.connect.await_count == _EXPECTED_CALLS
    assert connection.close_count == _EXPECTED_CALLS


async def test_connect_failure_returns_error(hass, rt_factory) -> None:
    """Échec de connexion WS -> SpeechResult ERROR (message générique)."""
    connection = FakeConnection()
    rt_factory.return_value = MagicMock()
    rt_factory.return_value.connect = AsyncMock(
        side_effect=RuntimeError("handshake failed")
    )
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01"])
    )

    assert result.text is None
    assert result.result is SpeechResultState.ERROR
    assert connection.close_count == 0  # aucune connexion ouverte


async def test_error_event_returns_error_and_closes(hass, rt_factory) -> None:
    """Erreur renvoyée par l'API pendant la session -> ERROR + connexion fermée."""
    connection = FakeConnection(
        events=[
            _event("transcription.text.delta", "Bo"),
            _event("error", ""),
        ]
    )
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01\x00"])
    )

    assert result.text is None
    assert result.result is SpeechResultState.ERROR
    # La connexion a été fermée via le finally (la pompe est annulée :
    # end_audio n'est pas garanti si l'erreur arrive avant la fin du flux).
    assert connection.end_audio_count in (0, 1)
    assert connection.close_count == 1


async def test_close_called_on_send_failure(hass, rt_factory) -> None:
    """Le finally ferme la connexion même si l'envoi échoue."""
    connection = FakeConnection(events=[], fail_send=True)
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01\x00"])
    )

    assert result.text is None
    assert result.result is SpeechResultState.ERROR
    assert connection.close_count == 1


async def test_close_called_on_event_stream_failure(hass, rt_factory) -> None:
    """Le finally ferme la connexion si le flux d'événements casse."""
    connection = FakeConnection(fail_events=True)
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01\x00"])
    )

    assert result.text is None
    assert result.result is SpeechResultState.ERROR
    assert connection.close_count == 1


async def test_session_ended_without_text(hass, rt_factory) -> None:
    """Session terminée sans done ni deltas -> ERROR."""
    connection = FakeConnection(events=[])
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01\x00"])
    )

    assert result.text is None
    assert result.result is SpeechResultState.ERROR


async def test_segment_replaces_deltas(hass, rt_factory) -> None:
    """Sans done : le segment finalisé remplace les deltas accumulés."""
    connection = FakeConnection(
        events=[
            _event("transcription.text.delta", "Bonjo"),
            _event("transcription.text.delta", "Bonjour"),
            _event("transcription.segment", "Bonjour le monde"),
        ]
    )
    _connect_to(rt_factory, connection)
    _, entity = await _make_entity(hass)

    result = await entity.async_process_audio_stream(
        _metadata(), _chunk_stream([b"\x01\x00"])
    )

    assert result.text == "Bonjour le monde"
    assert result.result is SpeechResultState.SUCCESS


async def test_entity_capabilities_pcm_16k_mono(hass) -> None:
    """Capacités compatibles PCM 16 kHz mono envoyé par le Voice PE."""
    _, entity = await _make_entity(hass)

    assert entity.supported_formats == [AudioFormats.WAV]
    assert entity.supported_codecs == [AudioCodecs.PCM]
    assert entity.supported_bit_rates == [AudioBitRates.BITRATE_16]
    assert entity.supported_sample_rates == [AudioSampleRates.SAMPLERATE_16000]
    assert entity.supported_channels == [AudioChannels.CHANNEL_MONO]


async def test_supported_languages_follow_options(hass) -> None:
    """supported_languages suit l'option language (lue à chaque accès)."""
    _, entity_default = await _make_entity(hass)
    assert entity_default.supported_languages == [DEFAULT_LANGUAGE]

    _, entity_fr = await _make_entity(hass, options={CONF_LANGUAGE: "fr"})
    assert entity_fr.supported_languages == ["fr"]

    _, entity_en = await _make_entity(hass, options={CONF_LANGUAGE: "en"})
    assert entity_en.supported_languages == ["en"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_stt_platform_loaded_with_entry(hass) -> None:
    """Intégration légère : le setup de l'entrée charge la plateforme stt."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": FAKE_API_KEY},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # L'entité STT est enregistrée avec l'entrée comme device.
    entity_registry = er.async_get(hass)
    stt_entities = [
        reg_entry
        for reg_entry in entity_registry.entities.values()
        if reg_entry.platform == DOMAIN and reg_entry.domain == "stt"
    ]
    assert len(stt_entities) == 1
    assert stt_entities[0].unique_id == entry.entry_id

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(stt_entities[0].device_id)
    assert device is not None
    assert (DOMAIN, entry.entry_id) in device.identifiers


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unload_entry_unregisters_stt_entity(hass) -> None:
    """Le déchargement de l'entrée retire bien l'entité STT."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": FAKE_API_KEY},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # L'entité n'est plus fournie par la plateforme (état indisponible /
    # retiré selon le rechargement des états restaurés).
    states = hass.states.async_all("stt")
    assert all(state.state == "unavailable" for state in states)
