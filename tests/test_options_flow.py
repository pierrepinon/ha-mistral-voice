"""Tests du options flow de mistral_voice et du pre-prompt par défaut.

Tous les appels `GET /v1/models` (client.models.list) sont mockés avec des
listes factices ; la clé API des tests est factice et n'apparaît jamais
dans les logs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.mistral_voice import api
from custom_components.mistral_voice.config_flow import (
    _validate_options,
    resolve_system_prompt,
)
from custom_components.mistral_voice.const import (
    CONF_LANGUAGE,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_REFRESH_MODELS,
    CONF_STREAMING_DELAY_MS,
    CONF_SYSTEM_PROMPT,
    CONF_VOICE,
    DEFAULT_LANGUAGE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_STREAMING_DELAY_MS,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_VOICE,
    DOMAIN,
    MAX_MAX_TOKENS,
    MAX_STREAMING_DELAY_MS,
    MIN_MAX_TOKENS,
    MIN_STREAMING_DELAY_MS,
)

FAKE_API_KEY = "test-key-123"

# Contenu factice de GET /v1/models : mélange chat / non-chat, ordre en vrac.
FAKE_MODEL_LIST: list[tuple[str, bool]] = [
    ("mistral-medium", True),
    ("mistral-embed", False),
    ("mistral-alpha-latest", True),
    ("mistral-medium-latest", True),
    ("codestral-latest", False),
    ("mistral-large", True),
]

# Attendu : modèles de chat uniquement, alias `-latest` en premier puis A→Z.
EXPECTED_CHAT_OPTIONS = [
    "mistral-alpha-latest",
    "mistral-medium-latest",
    "mistral-large",
    "mistral-medium",
]

# Nombre d'appels GET /v1/models dans le test de rafraîchissement
# (1 au premier affichage + 1 case de rafraîchissement).
EXPECTED_LIST_CALLS = 2
# Deuxième liste (après « Rafraîchir les modèles ») : un nouveau modèle.
REFRESHED_MODEL_LIST: list[tuple[str, bool]] = [
    ("mistral-new-latest", True),
    ("mistral-large", True),
]
EXPECTED_REFRESHED_OPTIONS = ["mistral-new-latest", "mistral-large"]


def _card(model_id: str, *, chat: bool) -> SimpleNamespace:
    """Carte de modèle factice (forme `GET /v1/models`)."""
    return SimpleNamespace(
        id=model_id, capabilities=SimpleNamespace(completion_chat=chat)
    )


def _model_list(cards: list[tuple[str, bool]]) -> SimpleNamespace:
    """Réponse factice de GET /v1/models (conteneur avec `.data`)."""
    return SimpleNamespace(
        data=[_card(model_id, chat=chat) for model_id, chat in cards]
    )


@contextmanager
def _mock_mistral(
    results: list[SimpleNamespace | BaseException] | SimpleNamespace | BaseException,
) -> Iterator[SimpleNamespace]:
    """Patcher le client SDK Mistral avec `models.list()` factice.

    `results` : une liste de réponses/exceptions (une par appel de
    `models.list()`) ou une réponse/exception unique.
    """
    queue: list[SimpleNamespace | BaseException] = (
        list(results) if isinstance(results, list) else [results]
    )

    def _list() -> SimpleNamespace:
        """Réponse suivante de GET /v1/models (ou exception)."""
        if not queue:
            raise AssertionError("models.list() appelé plus de fois que mocké")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    models = SimpleNamespace(list=MagicMock(side_effect=_list))
    client = SimpleNamespace(models=models, chat=SimpleNamespace(complete=MagicMock()))
    with patch.object(api, "Mistral", return_value=client) as mistral_cls:
        yield SimpleNamespace(cls=mistral_cls, client=client, models=models)


pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _setup_entry(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Créer et charger une entrée de config (clé factice)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={"api_key": FAKE_API_KEY},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _unwrap_default(marker: vol.Marker) -> Any:
    """Extraire la valeur par défaut d'un marqueur voluptuous."""
    return marker.default() if callable(marker.default) else None


def _schema_map(result: dict[str, Any]) -> dict[Any, Any]:
    """Champ → marqueur du schéma d'un formulaire de flow."""
    schema = result["data_schema"].schema
    return {key.schema: key for key in schema}


def _defaults() -> dict[str, Any]:
    """Saisie attendue quand l'utilisateur garde les valeurs par défaut."""
    return {
        CONF_LANGUAGE: DEFAULT_LANGUAGE,
        CONF_STREAMING_DELAY_MS: DEFAULT_STREAMING_DELAY_MS,
        CONF_SYSTEM_PROMPT: "",
        CONF_MODEL: DEFAULT_MODEL,
        CONF_VOICE: DEFAULT_VOICE,
        CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
    }


async def _open_options(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Ouvrir le formulaire d'options."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    return result


async def test_options_form_shows_defaults(hass: HomeAssistant) -> None:
    """Le formulaire vierge affiche les valeurs par défaut du ticket."""
    entry = await _setup_entry(hass)
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        result = await _open_options(hass, entry)

    schema_map = _schema_map(result)
    assert schema_map[CONF_MODEL].default() == DEFAULT_MODEL
    assert schema_map[CONF_LANGUAGE].default() == DEFAULT_LANGUAGE
    assert schema_map[CONF_STREAMING_DELAY_MS].default() == DEFAULT_STREAMING_DELAY_MS
    assert schema_map[CONF_SYSTEM_PROMPT].default() == ""
    assert schema_map[CONF_VOICE].default() == DEFAULT_VOICE
    assert schema_map[CONF_MAX_TOKENS].default() == DEFAULT_MAX_TOKENS
    assert schema_map[CONF_REFRESH_MODELS].default() is False


async def test_options_defaults_saved(hass: HomeAssistant) -> None:
    """Soumettre sans rien changer → options sauvegardées avec les défauts."""
    entry = await _setup_entry(hass)
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        result = await _open_options(hass, entry)

        configured = await hass.config_entries.options.async_configure(
            result["flow_id"], _defaults()
        )
        await hass.async_block_till_done()

    assert configured["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LANGUAGE] == DEFAULT_LANGUAGE
    assert entry.options[CONF_STREAMING_DELAY_MS] == DEFAULT_STREAMING_DELAY_MS
    assert entry.options[CONF_SYSTEM_PROMPT] == ""
    assert entry.options[CONF_MODEL] == DEFAULT_MODEL
    assert entry.options[CONF_VOICE] == DEFAULT_VOICE
    assert entry.options[CONF_MAX_TOKENS] == DEFAULT_MAX_TOKENS
    # La case de rafraîchissement n'est jamais persistée.
    assert CONF_REFRESH_MODELS not in entry.options


async def test_options_override_saved(hass: HomeAssistant) -> None:
    """Surcharge de toutes les options."""
    entry = await _setup_entry(hass)
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        result = await _open_options(hass, entry)

        configured = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_LANGUAGE: "en",
                CONF_STREAMING_DELAY_MS: 600,
                CONF_SYSTEM_PROMPT: "Réponds très brièvement.",
                CONF_MODEL: "mistral-large-latest",
                CONF_VOICE: "voice-42",
                CONF_MAX_TOKENS: 512,
            },
        )
        await hass.async_block_till_done()

    assert configured["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_LANGUAGE: "en",
        CONF_STREAMING_DELAY_MS: 600,
        CONF_SYSTEM_PROMPT: "Réponds très brièvement.",
        CONF_MODEL: "mistral-large-latest",
        CONF_VOICE: "voice-42",
        CONF_MAX_TOKENS: 512,
    }


@pytest.mark.parametrize("out_of_range", [79, 2401])
async def test_options_delay_out_of_range_rejected(
    hass: HomeAssistant, out_of_range: int
) -> None:
    """Hors bornes 80-2400 → rejet du schéma du flow manager (InvalidData)."""
    entry = await _setup_entry(hass)

    user_input = _defaults() | {CONF_STREAMING_DELAY_MS: out_of_range}
    with (
        _mock_mistral(_model_list(FAKE_MODEL_LIST)),
        pytest.raises(InvalidData),
    ):
        await hass.config_entries.options.async_configure(
            (await _open_options(hass, entry))["flow_id"], user_input
        )
    await hass.async_block_till_done()

    # Options inchangées.
    assert not entry.options


@pytest.mark.parametrize("in_range", [80, 2400])
async def test_options_delay_bounds_accepted(
    hass: HomeAssistant, in_range: int
) -> None:
    """80 et 2400 exactement sont acceptés aux bornes."""
    entry = await _setup_entry(hass)
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        result = await _open_options(hass, entry)

        configured = await hass.config_entries.options.async_configure(
            result["flow_id"], _defaults() | {CONF_STREAMING_DELAY_MS: in_range}
        )
        await hass.async_block_till_done()

    assert configured["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_STREAMING_DELAY_MS] == in_range


async def test_options_reload_entry_on_change(hass: HomeAssistant) -> None:
    """Changer les options recharge l'entrée (update listener) sans erreur."""
    entry = await _setup_entry(hass)
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        result = await _open_options(hass, entry)

        await hass.config_entries.options.async_configure(
            result["flow_id"], _defaults()
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.options[CONF_LANGUAGE] == DEFAULT_LANGUAGE


def test_resolve_system_prompt_falls_back() -> None:
    """Pre-prompt vide ou blanc → pre-prompt par défaut de const.py."""
    assert resolve_system_prompt({}) == DEFAULT_SYSTEM_PROMPT
    assert resolve_system_prompt({CONF_SYSTEM_PROMPT: ""}) == DEFAULT_SYSTEM_PROMPT
    assert resolve_system_prompt({CONF_SYSTEM_PROMPT: "  "}) == DEFAULT_SYSTEM_PROMPT
    assert (
        resolve_system_prompt({CONF_SYSTEM_PROMPT: " Toujours en breton. "})
        == "Toujours en breton."
    )


# ---------------------------------------------------------------------------
# Ticket #4 : sélecteur de modèles dynamique + rafraîchissement + erreurs.
# ---------------------------------------------------------------------------
async def test_model_selector_lists_chat_models_latest_first(
    hass: HomeAssistant,
) -> None:
    """Le sélecteur `model` reçoit les modèles de chat, alias `-latest` en premier."""
    entry = await _setup_entry(hass)
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)) as mock:
        result = await _open_options(hass, entry)

    selector_map = _schema_map(result)
    selector = result["data_schema"].schema[selector_map[CONF_MODEL]]
    assert selector.config["custom_value"] is True
    # La valeur par défaut (aucun modèle configuré) est injectée en tête,
    # suivie des modèles de chat de GET /v1/models triés (-latest d'abord).
    assert selector.config["options"] == [DEFAULT_MODEL, *EXPECTED_CHAT_OPTIONS]

    mock.models.list.assert_called_once_with()
    # Client SDK construit paresseusement, une seule fois pour le flow.
    mock.cls.assert_called_once_with(api_key=FAKE_API_KEY)


async def test_current_model_kept_when_absent_from_fetch(
    hass: HomeAssistant,
) -> None:
    """Le modèle configuré reste sélectionnable s'il disparaît de /v1/models."""
    entry = await _setup_entry(
        hass,
        options={
            CONF_MODEL: "mistral-vintage-2024",
            CONF_LANGUAGE: "fr",
            CONF_STREAMING_DELAY_MS: 400,
            CONF_SYSTEM_PROMPT: "",
        },
    )
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        result = await _open_options(hass, entry)

    schema_map = _schema_map(result)
    assert schema_map[CONF_MODEL].default() == "mistral-vintage-2024"
    selector = result["data_schema"].schema[schema_map[CONF_MODEL]]
    assert "mistral-vintage-2024" in selector.config["options"]


async def test_refresh_models_refetches_before_reshowing(
    hass: HomeAssistant,
) -> None:
    """Case cochée + soumission → réinterrogation /v1/models, formulaire réaffiché."""
    entry = await _setup_entry(hass)
    with _mock_mistral(
        [_model_list(FAKE_MODEL_LIST), _model_list(REFRESHED_MODEL_LIST)]
    ) as mock:
        result = await _open_options(hass, entry)

        refreshed = await hass.config_entries.options.async_configure(
            result["flow_id"],
            _defaults() | {CONF_REFRESH_MODELS: True, CONF_MODEL: "mistral-new-latest"},
        )
        await hass.async_block_till_done()

    # Formulaire réaffiché avec la nouvelle liste, sans avoir rien enregistré.
    assert refreshed["type"] == FlowResultType.FORM
    assert not refreshed["errors"]
    schema_map = _schema_map(refreshed)
    assert schema_map[CONF_MODEL].default() == "mistral-new-latest"
    selector = refreshed["data_schema"].schema[schema_map[CONF_MODEL]]
    assert selector.config["options"] == EXPECTED_REFRESHED_OPTIONS
    assert mock.models.list.call_count == EXPECTED_LIST_CALLS
    # Un seul client SDK pour l'entrée (construction paresseuse unique).
    mock.cls.assert_called_once_with(api_key=FAKE_API_KEY)
    assert not entry.options  # rien d'enregistré au moment du rafraîchissement

    # Soumission suivante (sans rafraîchissement) → options sauvegardées.
    with _mock_mistral(_model_list(REFRESHED_MODEL_LIST)):
        final = await hass.config_entries.options.async_configure(
            refreshed["flow_id"], _defaults() | {CONF_MODEL: "mistral-new-latest"}
        )
        await hass.async_block_till_done()

    assert final["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MODEL] == "mistral-new-latest"


async def test_refresh_models_api_error_keeps_current_value(
    hass: HomeAssistant,
) -> None:
    """RAF + API injoignable → erreur affichée, valeur du modèle conservée."""
    entry = await _setup_entry(
        hass,
        options={
            CONF_MODEL: "mistral-large-latest",
            CONF_LANGUAGE: "fr",
            CONF_STREAMING_DELAY_MS: 400,
            CONF_SYSTEM_PROMPT: "",
        },
    )
    with _mock_mistral([_model_list(FAKE_MODEL_LIST), RuntimeError("network down")]):
        result = await _open_options(hass, entry)

        refreshed = await hass.config_entries.options.async_configure(
            result["flow_id"],
            _defaults()
            | {CONF_REFRESH_MODELS: True, CONF_MODEL: "mistral-large-latest"},
        )
        await hass.async_block_till_done()

    assert refreshed["type"] == FlowResultType.FORM
    assert refreshed["errors"] == {"base": "models_fetch_failed"}
    # Options enregistrées inchangées ; la valeur courante du modèle est conservée.
    assert entry.options == {
        CONF_MODEL: "mistral-large-latest",
        CONF_LANGUAGE: "fr",
        CONF_STREAMING_DELAY_MS: 400,
        CONF_SYSTEM_PROMPT: "",
    }
    schema_map = _schema_map(refreshed)
    assert schema_map[CONF_MODEL].default() == "mistral-large-latest"
    selector = refreshed["data_schema"].schema[schema_map[CONF_MODEL]]
    assert "mistral-large-latest" in selector.config["options"]


async def test_models_fetch_failed_on_open_shows_error(
    hass: HomeAssistant,
) -> None:
    """API injoignable dès l'ouverture → erreur traduite, config conservée."""
    entry = await _setup_entry(
        hass,
        options={
            CONF_MODEL: "mistral-large-latest",
            CONF_LANGUAGE: "fr",
            CONF_STREAMING_DELAY_MS: 400,
            CONF_SYSTEM_PROMPT: "",
        },
    )
    with _mock_mistral(RuntimeError("network down")):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "models_fetch_failed"}

    schema_map = _schema_map(result)
    assert schema_map[CONF_MODEL].default() == "mistral-large-latest"
    selector = result["data_schema"].schema[schema_map[CONF_MODEL]]
    # La valeur courante reste proposée même quand la liste est vide.
    assert "mistral-large-latest" in selector.config["options"]

    # Options inchangées, puis sauvegarde possible malgré le modèle absent
    # de la liste (custom value).
    with _mock_mistral(_model_list(FAKE_MODEL_LIST)):
        saved = await hass.config_entries.options.async_configure(
            result["flow_id"],
            _defaults() | {CONF_MODEL: "mistral-large-latest"},
        )
        await hass.async_block_till_done()

    assert saved["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MODEL] == "mistral-large-latest"


async def test_options_flow_api_key_never_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """La clé factice n'apparaît dans aucun log pendant le options flow."""
    entry = await _setup_entry(hass)
    with (
        _mock_mistral(_model_list(FAKE_MODEL_LIST)),
        caplog.at_level(logging.DEBUG),
    ):
        result = await _open_options(hass, entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"], _defaults()
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_MODEL] == DEFAULT_MODEL
    assert FAKE_API_KEY not in caplog.text


# ---------------------------------------------------------------------------
# Ticket #7 : validation unitaire de la plage `target_streaming_delay_ms`.
# ---------------------------------------------------------------------------


def test_validate_options_bounds_accepted() -> None:
    """Les bornes (80 et 2400) et une valeur médiane sont acceptées."""
    for delay in (
        MIN_STREAMING_DELAY_MS,
        MIN_STREAMING_DELAY_MS + 1,
        480,
        MAX_STREAMING_DELAY_MS - 1,
        MAX_STREAMING_DELAY_MS,
    ):
        errors = _validate_options(
            {
                CONF_LANGUAGE: DEFAULT_LANGUAGE,
                CONF_STREAMING_DELAY_MS: delay,
                CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
            }
        )
        assert errors == {}, delay


def test_validate_options_delay_out_of_range_rejected() -> None:
    """Hors de la plage [80, 2400] (ou non convertible) : erreur de formulaire."""
    for delay in (
        0,
        40,
        MIN_STREAMING_DELAY_MS - 1,
        -80,
        MAX_STREAMING_DELAY_MS + 1,
        5000,
        "abc",
        "",
    ):
        errors = _validate_options(
            {
                CONF_LANGUAGE: DEFAULT_LANGUAGE,
                CONF_STREAMING_DELAY_MS: delay,
                CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
            }
        )
        assert errors == {"target_streaming_delay_ms": "delay_out_of_range"}, delay


def test_validate_options_missing_delay_rejected() -> None:
    """Absence du champ : erreur `delay_out_of_range` (valeur requise)."""
    errors = _validate_options(
        {CONF_LANGUAGE: DEFAULT_LANGUAGE, CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS}
    )
    assert errors == {CONF_STREAMING_DELAY_MS: "delay_out_of_range"}


def test_validate_options_invalid_language_rejected() -> None:
    """Langue inconnue : erreur `invalid_language`."""
    errors = _validate_options(
        {
            CONF_LANGUAGE: "klingon",
            CONF_STREAMING_DELAY_MS: 480,
            CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
        }
    )
    assert errors == {CONF_LANGUAGE: "invalid_language"}


def test_validate_options_max_tokens_bounds_accepted() -> None:
    """Les bornes (64 et 4096) et une valeur médiane sont acceptées."""
    for tokens in (
        MIN_MAX_TOKENS,
        MIN_MAX_TOKENS + 1,
        1024,
        MAX_MAX_TOKENS - 1,
        MAX_MAX_TOKENS,
    ):
        errors = _validate_options(
            {
                CONF_LANGUAGE: DEFAULT_LANGUAGE,
                CONF_STREAMING_DELAY_MS: 480,
                CONF_MAX_TOKENS: tokens,
            }
        )
        assert errors == {}, tokens


def test_validate_options_max_tokens_out_of_range_rejected() -> None:
    """Hors de la plage [64, 4096] (ou non convertible) : erreur de formulaire."""
    for tokens in (
        0,
        16,
        MIN_MAX_TOKENS - 1,
        -64,
        MAX_MAX_TOKENS + 1,
        10000,
        "abc",
        "",
    ):
        errors = _validate_options(
            {
                CONF_LANGUAGE: DEFAULT_LANGUAGE,
                CONF_STREAMING_DELAY_MS: 480,
                CONF_MAX_TOKENS: tokens,
            }
        )
        assert errors == {"max_tokens": "max_tokens_out_of_range"}, tokens


def test_validate_options_missing_max_tokens_rejected() -> None:
    """Absence du champ : erreur `max_tokens_out_of_range` (valeur requise)."""
    errors = _validate_options(
        {CONF_LANGUAGE: DEFAULT_LANGUAGE, CONF_STREAMING_DELAY_MS: 480}
    )
    assert errors == {CONF_MAX_TOKENS: "max_tokens_out_of_range"}
