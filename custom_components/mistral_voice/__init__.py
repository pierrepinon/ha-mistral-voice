"""L'intégration Mistral Voice (domaine `mistral_voice`).

Charge et décharge les entrées de configuration. Client API Mistral
unique par entrée (construit paresseusement, sans requête au setup) via
`MistralVoiceData` ; les plateformes STT (fournisseur de transcription
en flux), TTS (fournisseur de synthèse vocale en flux) et conversation
(agent `ConversationEntity` avec mémoire, enregistré auprès du Pipeline
Assist à la création de l'entité puis désenregistré à sa suppression)
sont chargées ici.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .api import MistralVoiceData
from .const import DOMAIN

# Plateformes à charger pour chaque entrée (STT, TTS et l'agent de
# conversation en entité — l'agent s'enregistre de lui-même auprès du
# Pipeline Assist à la création de l'entité).
PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS, Platform.CONVERSATION]

# Intégration exclusivement configurable via l'UI : aucune config YAML acceptée.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORM_SCHEMA = vol.Schema({})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Configuration YAML globale : non supportée, uniquement l'UI."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Charger une entrée de configuration Mistral Voice."""
    hass.data.setdefault(DOMAIN, {})
    # État partagé (client SDK construit paresseusement, clé jamais exposée).
    hass.data[DOMAIN][entry.entry_id] = MistralVoiceData(entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharger une entrée de configuration Mistral Voice."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharger l'entrée quand ses options changent."""
    await hass.config_entries.async_reload(entry.entry_id)
