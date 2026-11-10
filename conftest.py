"""Conftest pytest : rend le dépôt importable (custom_components.*)."""

from __future__ import annotations

import pathlib
import sys

from homeassistant.components.homeassistant.const import (
    DATA_EXPOSED_ENTITIES,
)
from homeassistant.components.homeassistant.exposed_entities import (
    ExposedEntities,
)
import pytest

REPO_ROOT = pathlib.Path(__file__).parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# pytest-homeassistant-custom-component a déjà importé son propre paquet
# `custom_components` (testing_config) : on y ajoute le chemin du dépôt pour
# que le loader HA découvre mistral_voice.
import custom_components  # noqa: E402

_repo_custom_components = str(REPO_ROOT / "custom_components")
if _repo_custom_components not in custom_components.__path__:
    custom_components.__path__.append(_repo_custom_components)


@pytest.fixture(autouse=True)
async def _exposed_entities_for_test(hass):
    """Prépare `hass.data['homeassistant.exposed_entities']`.

    Le composant `conversation` (dépendance de mistral_voice) enregistre
    l'agent par défaut qui, au démarrage de HA, interroge
    `async_should_expose`. En test, le composant `homeassistant` n'est pas
    chargé : on instancie `ExposedEntities` directement (stockage en
    mémoire, sans websocket) pour que tout le setup passe.
    """
    if DATA_EXPOSED_ENTITIES not in hass.data:
        exposed_entities = ExposedEntities(hass)
        await exposed_entities.async_initialize()
        hass.data[DATA_EXPOSED_ENTITIES] = exposed_entities
    yield
