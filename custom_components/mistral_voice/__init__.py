"""L'intégration Mistral Voice (domaine `mistral_voice`).

Charge et décharge les entrées de configuration. Client API Mistral
unique par entrée (construit paresseusement, sans requête au setup) via
`MistralVoiceData` ; les plateformes STT (fournisseur de transcription
en flux) et TTS (fournisseur de synthèse vocale) sont chargées ici ;
l'agent de conversation (agent de service enregistré
programmatiquement, pas une entité) est enregistré auprès du
Pipeline Assist via `async_set_agent` puis désenregistré au
unload via `async_unset_agent`.
"""

from __future__ import annotations

from homeassistant.components.conversation import (
    async_set_agent,
    async_unset_agent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .api import MistralVoiceData
from .const import DOMAIN
from .conversation import MistralVoiceAgent

# Plateformes à charger pour chaque entrée. L'agent de conversation n'est
# pas une plateforme : il est enregistré programmatiquement ci-dessous
# (pattern de google_generative_ai_conversation / openai_conversation).
PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS]

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

    # Rien de core à recharger tant qu'aucune plateforme n'existe encore.
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Agent de conversation : enregistré programmatiquement auprès du
    # Pipeline Assist (pas une entité).
    async_set_agent(hass, entry, MistralVoiceAgent(hass, entry))

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharger une entrée de configuration Mistral Voice."""
    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Désenregistrement de l'agent de conversation auprès du Pipeline Assist.
    async_unset_agent(hass, entry)

    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharger l'entrée quand ses options changent."""
    await hass.config_entries.async_reload(entry.entry_id)
