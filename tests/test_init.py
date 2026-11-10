"""Tests de chargement du squelette de l'intégration mistral_voice."""

from homeassistant.config_entries import ConfigEntryState
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mistral_voice import api
from custom_components.mistral_voice.const import DOMAIN


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_and_unload_entry(hass) -> None:
    """L'entrée de config se charge puis se décharge sans erreur."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": "placeholder-key-from-test"},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # État partagé stocké dans hass.data : client construit paresseusement.
    stored = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(stored, api.MistralVoiceData)
    assert stored.entry is entry
    assert stored.client is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data
