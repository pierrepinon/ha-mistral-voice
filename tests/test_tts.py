"""Tests du fournisseur TTS Mistral Voice.

Cas couverts :
- flux nominal : texte -> `POST /v1/audio/speech` -> audio base64 décodé en
  bytes MP3, rendu au pipeline avec l'extension `mp3` ;
- option ``voice`` (identifiant de voix Mistral) transmise à l'appel,
  et absente de l'appel quand elle est vide (voix par défaut de l'API) ;
- erreur API -> `HomeAssistantError` générique (jamais de détail de
  requête ni de clé API dans le message ou les logs) ;
- réponse sans `audio_data` ou base64 invalide -> `HomeAssistantError` ;
- langues supportées / langue par défaut qui suivent l'option ``language``
  (récupérées à la création de l'entité, l'entrée est rechargée à la
  modification des options) ;
- intégration légère : la plateforme ``tts`` est chargée au setup de
  l'entrée (enable_custom_integrations), même appareil que le STT.

La clé API n'est jamais loguée : les tests utilisent une clé factice.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

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
