"""Tests du fournisseur TTS Mistral Voice.

Cas couverts :
- flux nominal sans stream : texte -> `POST /v1/audio/speech` -> audio
  base64 décodé en bytes, rendu au pipeline avec son extension ;
- option ``voice`` (identifiant de voix Mistral) transmise à l'appel,
  et absente de l'appel quand elle est vide (voix par défaut de l'API) ;
- erreur API -> `HomeAssistantError` générique (jamais de détail de
  requête ni de clé API dans le message ou les logs) ;
- réponse sans `audio_data` ou base64 invalide -> `HomeAssistantError` ;
- **chemin en flux** (`async_stream_tts_audio`, pipeline Assist /
  satellite vocal) : `message_gen` consommé, flux `speech.audio.delta`
  décodé chunk par chunk, format tiré de l'option Home Assistant
  `preferred_format` (wav pour le satellite, mp3 par défaut, repli mp3
  pour toute valeur inconnue), erreurs d'ouverture et de lecture en
  `HomeAssistantError` générique ;
- option `preferred_format` déclarée supportée ;
- langues supportées / langue par défaut qui suivent l'option `language` ;
- intégration légère : la plateforme ``tts`` est chargée au setup de
  l'entrée (enable_custom_integrations), même appareil que le STT.

La clé API n'est jamais loguée : les tests utilisent une clé factice.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.tts import ATTR_PREFERRED_FORMAT, TTSAudioRequest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mistral_voice.api import MistralApiError
from custom_components.mistral_voice.const import (
    CONF_LANGUAGE,
    CONF_VOICE,
    DEFAULT_LANGUAGE,
    DOMAIN,
)
import custom_components.mistral_voice.tts as tts_module

FAKE_API_KEY = "placeholder-key-from-test"

# Audio factice renvoyé par l'API (base64), décodé attendu côté entité.
AUDIO_BYTES = b"\x00\x01\x02mp3-frames"
AUDIO_DATA_B64 = base64.b64encode(AUDIO_BYTES).decode()


def _response() -> Any:
    """Réponse factice de POST /v1/audio/speech (audio base64)."""
    return SimpleNamespace(audio_data=AUDIO_DATA_B64)


def _audio_event(audio_bytes: bytes) -> Any:
    """Événement factice `speech.audio.delta` du flux SSE."""
    return SimpleNamespace(
        data=SimpleNamespace(
            audio_data=base64.b64encode(audio_bytes).decode(),
            type="speech.audio.delta",
        )
    )


def _stream(events: list[Any]) -> Any:
    """Flux SSE asynchrone factice (itérable async d'événements)."""

    async def gen():
        for event in events:
            yield event

    return gen()


async def _message_gen(chunks: list[str]) -> Any:
    """`message_gen` du pipeline : diffs la réponse de l'agent."""
    for chunk in chunks:
        yield chunk


async def _make_entity(
    hass: Any, options: dict[str, Any] | None = None
) -> tuple[MockConfigEntry, Any]:
    """Construire l'entité TTS via async_setup_entry (entrée factice)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": FAKE_API_KEY},
        options=options or {},
    )
    entry.add_to_hass(hass)
    entities: list[Any] = []
    await tts_module.async_setup_entry(hass, entry, entities.extend)
    assert len(entities) == 1
    return entry, entities[0]


async def test_nominal_tts(hass) -> None:
    """Texte -> audio/speech -> (\"mp3\", bytes décodés)."""
    with patch.object(
        tts_module,
        "async_speech_complete",
        AsyncMock(return_value=_response()),
    ) as speech:
        _, entity = await _make_entity(hass)

        result = await entity.async_get_tts_audio(
            "Bonjour, la maison est à vous.", "fr", {}
        )

    assert result == ("mp3", AUDIO_BYTES)
    call_kwargs = speech.call_args.kwargs
    assert call_kwargs["model"] == tts_module.TTS_MODEL
    assert call_kwargs["text"] == "Bonjour, la maison est à vous."
    assert call_kwargs["response_format"] == "mp3"
    assert call_kwargs["voice_id"] is None


async def test_voice_option_transmitted(hass) -> None:
    """L'option voice (identifiant Mistral) part dans l'appel."""
    with patch.object(
        tts_module,
        "async_speech_complete",
        AsyncMock(return_value=_response()),
    ) as speech:
        _, entity = await _make_entity(hass, options={CONF_VOICE: "voice-42"})

        await entity.async_get_tts_audio("Bonjour", "fr", {})

    assert speech.call_args.kwargs["voice_id"] == "voice-42"


async def test_api_error_raises_home_assistant_error(hass) -> None:
    """Erreur de l'API -> HomeAssistantError générique, aucun détail exposé."""
    with patch.object(
        tts_module,
        "async_speech_complete",
        AsyncMock(side_effect=MistralApiError("POST /v1/audio/speech a échoué")),
    ):
        _, entity = await _make_entity(hass)

        with pytest.raises(HomeAssistantError):
            await entity.async_get_tts_audio("Bonjour", "fr", {})


async def test_missing_audio_data_raises_home_assistant_error(hass) -> None:
    """Réponse sans audio_data -> HomeAssistantError (audio jamais joué)."""
    with patch.object(
        tts_module,
        "async_speech_complete",
        AsyncMock(return_value=SimpleNamespace()),
    ):
        _, entity = await _make_entity(hass)

        with pytest.raises(HomeAssistantError):
            await entity.async_get_tts_audio("Bonjour", "fr", {})


async def test_invalid_base64_raises_home_assistant_error(hass) -> None:
    """audio_data mal formé -> HomeAssistantError générique, sans crash."""
    with patch.object(
        tts_module,
        "async_speech_complete",
        AsyncMock(return_value=SimpleNamespace(audio_data="*** pas du base64 ***")),
    ):
        _, entity = await _make_entity(hass)

        with pytest.raises(HomeAssistantError):
            await entity.async_get_tts_audio("Bonjour", "fr", {})


async def test_languages_follow_options(hass) -> None:
    """supported_languages / default_language suivent l'option language."""
    _, entity_default = await _make_entity(hass)
    assert entity_default.supported_languages == [DEFAULT_LANGUAGE]
    assert entity_default.default_language == DEFAULT_LANGUAGE

    _, entity_en = await _make_entity(hass, options={CONF_LANGUAGE: "en"})
    assert entity_en.supported_languages == ["en"]
    assert entity_en.default_language == "en"


# ------------------------------------------------- chemin en flux


def test_preferred_format_option_declared(hass) -> None:
    """L'option `preferred_format` de HA est déclarée supportée (mp3 défaut)."""
    entity = MistralVoiceTTSEntityStub()
    assert entity.supported_options == [ATTR_PREFERRED_FORMAT]
    assert entity.default_options == {ATTR_PREFERRED_FORMAT: "mp3"}


class MistralVoiceTTSEntityStub(tts_module.MistralVoiceTTSEntity):
    """Entité instanciable sans entrée (lecture des attributs de classe)."""

    def __init__(self) -> None:
        """Aucune initialisation : on lit les attributs de classe."""


async def test_stream_nominal(hass) -> None:
    """message_gen -> flux audio -> chunks décodés, extension wav du satellite."""
    chunks = [b"\x01wav-hdr", b"\x02frames"]
    with patch.object(
        tts_module,
        "async_speech_stream",
        AsyncMock(return_value=_stream([_audio_event(c) for c in chunks])),
    ) as stream_mock:
        _, entity = await _make_entity(hass)

        response = await entity.async_stream_tts_audio(
            TTSAudioRequest(
                language="fr",
                options={ATTR_PREFERRED_FORMAT: "wav"},
                message_gen=_message_gen(["Salut le ", "monde."]),
            )
        )

    assert response.extension == "wav"
    received = [chunk async for chunk in response.data_gen]
    assert received == chunks

    call_kwargs = stream_mock.call_args.kwargs
    assert call_kwargs["text"] == "Salut le monde."
    assert call_kwargs["response_format"] == "wav"
    assert call_kwargs["model"] == tts_module.TTS_MODEL
    assert call_kwargs["voice_id"] is None


async def test_stream_default_and_unknown_format_fall_back_to_mp3(hass) -> None:
    """Sans option ou avec `raw` (hors scope) : MP3, jamais de PCM brut."""
    with patch.object(
        tts_module,
        "async_speech_stream",
        AsyncMock(return_value=_stream([_audio_event(b"\x01mp3")])),
    ) as stream_mock:
        _, entity = await _make_entity(hass)

        response = await entity.async_stream_tts_audio(
            TTSAudioRequest(language="fr", options={}, message_gen=_message_gen(["a"]))
        )
        assert response.extension == "mp3"
        assert stream_mock.call_args.kwargs["response_format"] == "mp3"

        response = await entity.async_stream_tts_audio(
            TTSAudioRequest(
                language="fr",
                options={ATTR_PREFERRED_FORMAT: "raw"},
                message_gen=_message_gen(["a"]),
            )
        )
        assert response.extension == "mp3"
        assert stream_mock.call_args.kwargs["response_format"] == "mp3"


async def test_stream_voice_option_transmitted(hass) -> None:
    """L'option voice part dans l'appel en flux comme dans l'appel complet."""
    with patch.object(
        tts_module,
        "async_speech_stream",
        AsyncMock(return_value=_stream([_audio_event(b"\x01mp3")])),
    ) as stream_mock:
        _, entity = await _make_entity(hass, options={CONF_VOICE: "voice-42"})

        await entity.async_stream_tts_audio(
            TTSAudioRequest(language="fr", options={}, message_gen=_message_gen(["a"]))
        )

    assert stream_mock.call_args.kwargs["voice_id"] == "voice-42"


async def test_stream_api_error_raises_home_assistant_error(hass) -> None:
    """Erreur à l'ouverture du flux -> HomeAssistantError générique."""
    with patch.object(
        tts_module,
        "async_speech_stream",
        AsyncMock(side_effect=MistralApiError("POST /v1/audio/speech a échoué")),
    ):
        _, entity = await _make_entity(hass)

        with pytest.raises(HomeAssistantError):
            await entity.async_stream_tts_audio(
                TTSAudioRequest(
                    language="fr", options={}, message_gen=_message_gen(["a"])
                )
            )


async def test_stream_mid_stream_error_raises_home_assistant_error(hass) -> None:
    """Erreur pendant la lecture du flux -> HomeAssistantError générique."""

    async def failing_stream():
        yield _audio_event(b"\x01mp3")
        raise RuntimeError("websocket gone")

    with patch.object(
        tts_module, "async_speech_stream", AsyncMock(return_value=failing_stream())
    ):
        _, entity = await _make_entity(hass)

        response = await entity.async_stream_tts_audio(
            TTSAudioRequest(language="fr", options={}, message_gen=_message_gen(["a"]))
        )
        first = await response.data_gen.__anext__()
        assert first == b"\x01mp3"
        with pytest.raises(HomeAssistantError):
            async for _ in response.data_gen:
                pass


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_tts_platform_loaded_with_entry(hass) -> None:
    """Le setup de l'entrée charge la plateforme tts (même appareil que le STT)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": FAKE_API_KEY},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    entity_registry = er.async_get(hass)
    tts_entities = [
        reg_entry
        for reg_entry in entity_registry.entities.values()
        if reg_entry.platform == DOMAIN and reg_entry.domain == "tts"
    ]
    assert len(tts_entities) == 1
    assert tts_entities[0].unique_id == entry.entry_id

    # STT et TTS déclarent le même appareil de service.
    stt_entities = [
        reg_entry
        for reg_entry in entity_registry.entities.values()
        if reg_entry.platform == DOMAIN and reg_entry.domain == "stt"
    ]
    assert len(stt_entities) == 1
    assert tts_entities[0].device_id == stt_entities[0].device_id

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(tts_entities[0].device_id)
    assert device is not None
    assert (DOMAIN, entry.entry_id) in device.identifiers


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unload_entry_unregisters_tts_entity(hass) -> None:
    """Le déchargement de l'entrée retire bien l'entité TTS."""
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

    states = hass.states.async_all("tts")
    assert all(state.state == "unavailable" for state in states)
