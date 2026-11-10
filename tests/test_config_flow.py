"""Tests du config flow de mistral_voice.

Aucune clé réelle : la valeur utilisée dans les tests est factice
(`test-key-123`) et ne doit jamais apparaître dans les logs.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.selector import TextSelectorType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.mistral_voice.config_flow import _api_key_schema
from custom_components.mistral_voice.const import CONF_API_KEY, DOMAIN

FAKE_API_KEY = "test-key-123"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _submit(hass: HomeAssistant, user_input: dict[str, Any]) -> Any:
    """Créer puis soumettre un flow de configuration."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_form_is_shown(hass: HomeAssistant) -> None:
    """Le flow s'affiche avec un champ de type mot de passe."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    schema = _api_key_schema()
    (key,) = schema.schema.keys()
    assert key.schema == CONF_API_KEY
    assert schema.schema[key].config["type"] == TextSelectorType.PASSWORD


async def test_flow_creates_entry_with_key(hass: HomeAssistant) -> None:
    """Clé factice valide → entrée créée, clé stockée dans entry.data."""
    result = await _submit(hass, {CONF_API_KEY: FAKE_API_KEY})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Mistral Voice"
    assert result["data"] == {CONF_API_KEY: FAKE_API_KEY}

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].state is config_entries.ConfigEntryState.LOADED


async def test_flow_rejects_empty_key(hass: HomeAssistant) -> None:
    """Clé vide ou espaces → formulaire réaffiché, aucune entrée créée."""
    result = await _submit(hass, {CONF_API_KEY: "   "})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_API_KEY: "api_key_missing"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_flow_rejects_missing_key(hass: HomeAssistant) -> None:
    """Champ absent de la saisie → rejet volatility du schéma du flow."""
    with pytest.raises(InvalidData):
        await _submit(hass, {})
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_flow_rejects_duplicate(hass: HomeAssistant) -> None:
    """Une deuxième saisie depuis le même flow → abort déjà configuré.

    L'identifiant unique est le domaine ; l'abort ne contient jamais la clé.
    """
    first = await _submit(hass, {CONF_API_KEY: FAKE_API_KEY})
    assert first["type"] == FlowResultType.CREATE_ENTRY

    # async_init avorte immédiatement (identifiant déjà configuré).
    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert second["type"] == FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert FAKE_API_KEY not in str(second)
    assert "another-test-key-456" not in str(second)


async def test_existing_entry_aborts_new_flow(hass: HomeAssistant) -> None:
    """Une entrée préexistante (même unique_id) fait avorter un nouveau flow."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        unique_id=DOMAIN,
        data={CONF_API_KEY: FAKE_API_KEY},
    )
    entry.add_to_hass(hass)

    # async_init avorte immédiatement, avant même d'afficher le formulaire.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Le flow n'a pas écrasé l'entrée existante.
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data == {CONF_API_KEY: FAKE_API_KEY}


async def test_api_key_never_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """La clé factice n'apparaît dans aucun log pendant le flow."""
    with caplog.at_level(logging.DEBUG):
        result = await _submit(hass, {CONF_API_KEY: FAKE_API_KEY})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert FAKE_API_KEY not in caplog.text


def test_api_key_schema_marks_required() -> None:
    """Le schéma marque le champ requis."""
    required = [k for k in _api_key_schema().schema if isinstance(k, vol.Required)]
    assert len(required) == 1
    assert required[0].schema == CONF_API_KEY
