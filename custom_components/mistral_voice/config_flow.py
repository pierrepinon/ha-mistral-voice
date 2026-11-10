"""Config flow et options flow de l'intégration Mistral Voice.

La clé API est saisie une seule fois via un champ masqué (PASSWORD) puis
stockée dans `entry.data` ; Home Assistant la chiffre au repos. Elle n'est
jamais loguée, jamais réaffichée en clair et jamais incluse dans un message
d'erreur : tous les messages d'erreur sont génériques.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import api
from .const import (
    CONF_API_KEY,
    CONF_LANGUAGE,
    CONF_MODEL,
    CONF_REFRESH_MODELS,
    CONF_STREAMING_DELAY_MS,
    CONF_SYSTEM_PROMPT,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    DEFAULT_STREAMING_DELAY_MS,
    DEFAULT_SYSTEM_PROMPT,
    DOMAIN,
    MAX_STREAMING_DELAY_MS,
    MIN_STREAMING_DELAY_MS,
    SUPPORTED_LANGUAGES,
)

_LOGGER = logging.getLogger(__name__)


def _api_key_schema() -> vol.Schema:
    """Schéma de l'étape de saisie de la clé API (champ masqué)."""
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


def _model_options(
    current: Mapping[str, Any], model_options: Sequence[str]
) -> list[str]:
    """Options du sélecteur de modèles (dynamiques + modèle courant).

    La liste vient de GET /v1/models (`client.models.list()`), filtrée
    aux modèles de chat et triée alias `-latest` en premier — aucune
    liste statique en dur. Le modèle courant reste toujours
    sélectionnable même s'il a disparu de la liste fraîchement
    récupérée : on ne perd jamais la configuration existante.
    """
    options = list(model_options)
    model = str(current.get(CONF_MODEL) or DEFAULT_MODEL)
    if model and model not in options:
        options.insert(0, model)
    if not options:
        # Le client est injoignable dès le départ : seule la valeur
        # par défaut (ou celle déjà configurée) est proposée.
        options = [model or DEFAULT_MODEL]
    return options


def _options_schema(
    current: Mapping[str, Any], model_options: Sequence[str]
) -> vol.Schema:
    """Schéma du formulaire d'options (valeurs par défaut ou actuelles)."""
    return vol.Schema(
        {
            vol.Required(
                CONF_MODEL, default=current.get(CONF_MODEL, DEFAULT_MODEL)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=_model_options(current, model_options),
                    custom_value=True,
                    translation_key=CONF_MODEL,
                )
            ),
            vol.Required(
                CONF_LANGUAGE, default=current.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=sorted(SUPPORTED_LANGUAGES),
                    translation_key=CONF_LANGUAGE,
                )
            ),
            vol.Required(
                CONF_STREAMING_DELAY_MS,
                default=current.get(
                    CONF_STREAMING_DELAY_MS, DEFAULT_STREAMING_DELAY_MS
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_STREAMING_DELAY_MS,
                    max=MAX_STREAMING_DELAY_MS,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="ms",
                )
            ),
            vol.Required(
                CONF_SYSTEM_PROMPT, default=current.get(CONF_SYSTEM_PROMPT, "")
            ): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
            ),
            vol.Required(CONF_REFRESH_MODELS, default=False): BooleanSelector(),
        }
    )


def _normalise_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Normaliser la saisie en options stockées.

    La case de rafraîchissement n'est pas persistée : elle ne sert qu'à
    réinterroger GET /v1/models avant de réafficher le formulaire.
    """
    return {
        CONF_LANGUAGE: str(user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)),
        CONF_STREAMING_DELAY_MS: int(user_input[CONF_STREAMING_DELAY_MS]),
        CONF_SYSTEM_PROMPT: str(user_input.get(CONF_SYSTEM_PROMPT) or ""),
        CONF_MODEL: str(user_input.get(CONF_MODEL) or DEFAULT_MODEL),
    }


def _validate_options(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Valider les options saisies ; retourne les erreurs (clés génériques)."""
    errors: dict[str, str] = {}

    language = user_input.get(CONF_LANGUAGE)
    if language is not None and str(language) not in SUPPORTED_LANGUAGES:
        errors[CONF_LANGUAGE] = "invalid_language"

    delay_raw = user_input.get(CONF_STREAMING_DELAY_MS)
    if (
        delay_raw is None
        or isinstance(delay_raw, bool)
        or not isinstance(delay_raw, (int, float, str))
    ):
        errors[CONF_STREAMING_DELAY_MS] = "delay_out_of_range"
    else:
        try:
            delay = int(delay_raw)
        except ValueError:
            errors[CONF_STREAMING_DELAY_MS] = "delay_out_of_range"
        else:
            if not MIN_STREAMING_DELAY_MS <= delay <= MAX_STREAMING_DELAY_MS:
                errors[CONF_STREAMING_DELAY_MS] = "delay_out_of_range"

    return errors


class MistralVoiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gérer le config flow de Mistral Voice."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> MistralVoiceOptionsFlow:
        """Créer le flow d'options pour cette entrée."""
        return MistralVoiceOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Étape unique : saisie de la clé API Mistral."""
        # Une seule intégration Mistral Voice : identifiant unique = domaine.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            # Aucun contenu de la saisie ne part dans un message d'erreur.
            api_key = str(user_input.get(CONF_API_KEY) or "").strip()
            if not api_key:
                errors[CONF_API_KEY] = "api_key_missing"
            else:
                return self.async_create_entry(
                    title="Mistral Voice",
                    data={CONF_API_KEY: api_key},
                )

        return self.async_show_form(
            step_id="user", data_schema=_api_key_schema(), errors=errors
        )


class MistralVoiceOptionsFlow(config_entries.OptionsFlow):
    """Options de l'intégration Mistral Voice.

    `self.config_entry` est résolu à l'exécution par le flow manager :
    la clé API n'est jamais lue ni affichée ici, les options ne portent
    que sur la langue, le délai de streaming, le pre-prompt et le modèle.

    Le sélecteur de modèles est alimenté dynamiquement par
    GET /v1/models (`client.models.list()`), filtré aux modèles de chat
    avec les alias `-latest` en premier — aucune liste statique en dur.
    """

    def __init__(self) -> None:
        """Initialiser l'état local du flow (liste des modèles vide au départ)."""
        self._model_options: list[str] = []

    async def _async_fetch_model_options(self, errors: dict[str, str]) -> None:
        """Interroger GET /v1/models ; en cas d'échec, erreur de formulaire.

        L'erreur traduite (`models_fetch_failed`) s'affiche dans le
        formulaire et aucune valeur ne disparaît : le modèle courant
        reste sélectionnable (valeur personnalisée permise) et les
        options enregistrées ne bougent pas.
        """
        try:
            self._model_options = await api.async_list_chat_model_ids(
                self.hass, self.config_entry
            )
        except Exception:
            self._model_options = []
            errors["base"] = "models_fetch_failed"
            # Journalisation volontairement générique : ni la clé API,
            # ni le détail de l'exception ne sont logués.
            _LOGGER.warning(
                "Options %s : impossible de récupérer la liste des modèles "
                "depuis GET /v1/models.",
                self.config_entry.entry_id,
            )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Étape unique d'édition des options."""
        errors: dict[str, str] = {}
        refresh = False

        if user_input is None:
            # Premier affichage : alimentation du sélecteur de modèles.
            await self._async_fetch_model_options(errors)
        else:
            errors = _validate_options(user_input)
            refresh = not errors and bool(user_input.get(CONF_REFRESH_MODELS, False))
            if refresh:
                # « Rafraîchir les modèles » : réinterroger /v1/models avant
                # de réafficher le formulaire, sans rien enregistrer.
                await self._async_fetch_model_options(errors)

        if user_input is not None and not errors and not refresh:
            return self.async_create_entry(
                title="Mistral Voice",
                data=_normalise_options(user_input),
            )

        if user_input is not None:
            # Conserver la saisie courante (la case de rafraîchissement
            # n'est pas une option) — notamment la valeur du modèle.
            shown: Mapping[str, Any] = {
                key: value
                for key, value in user_input.items()
                if key != CONF_REFRESH_MODELS
            }
        else:
            shown = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(shown, self._model_options),
            errors=errors,
        )


def resolve_system_prompt(options: Mapping[str, Any]) -> str:
    """Retourner le pre-prompt actif (surcharge utilisateur ou valeur par défaut).

    Un pre-prompt laissé vide revient au pre-prompt par défaut de `const.py`.
    """
    prompt = str(options.get(CONF_SYSTEM_PROMPT) or "").strip()
    return prompt or DEFAULT_SYSTEM_PROMPT
