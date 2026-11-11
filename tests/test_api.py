"""Tests unitaires du client Mistral (mistral_voice.api).

Clé jamais réelle dans les tests ; elle ne doit apparaître dans aucun log
ni message d'exception.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mistral_voice import api
from custom_components.mistral_voice.const import CONF_API_KEY, DOMAIN

FAKE_API_KEY = "unit-test-key-dummy"


def _card(model_id: str, *, chat: bool) -> SimpleNamespace:
    """Carte de modèle factice (forme renvoyée par GET /v1/models)."""
    capabilities = SimpleNamespace(completion_chat=chat)
    return SimpleNamespace(id=model_id, capabilities=capabilities)


def _model_list(cards: list[SimpleNamespace]) -> SimpleNamespace:
    """Réponse factice de GET /v1/models (conteneur avec `.data`)."""
    return SimpleNamespace(data=cards)


def _entry(options: dict | None = None) -> MockConfigEntry:
    """Entrée de configuration factice avec une clé API factice."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Mistral Voice",
        data={CONF_API_KEY: FAKE_API_KEY},
        options=options or {},
    )


def test_filter_chat_models_keeps_only_chat_models() -> None:
    """Les modèles sans capacité `completion_chat` sont exclus."""
    model_list = _model_list(
        [
            _card("mistral-large-latest", chat=True),
            _card("codestral-latest", chat=False),
            _card("mistral-embed", chat=False),
        ]
    )

    assert api.filter_chat_models(model_list) == ["mistral-large-latest"]


def test_filter_chat_models_ignores_cards_without_capabilities() -> None:
    """Une carte sans `.capabilities` (ou sans `.id`) est ignorée sans planter."""
    model_list = _model_list(
        [
            SimpleNamespace(id="mistral-small-latest", capabilities=None),
            SimpleNamespace(capabilities=SimpleNamespace(completion_chat=True)),
            "not-a-card",
            None,
        ]
    )

    assert api.filter_chat_models(model_list) == []


def test_filter_chat_models_empty_list() -> None:
    """Fonctionne sur une réponse vide ou inattendue (`data` absent)."""
    assert api.filter_chat_models(_model_list([])) == []
    assert api.filter_chat_models(SimpleNamespace()) == []


def test_latest_aliases_sorted_first() -> None:
    """Les alias `-latest` arrivent en premier, puis l'ordre alphabétique."""
    suffix = "-latest"
    model_list = _model_list(
        [
            _card("mistral-zeta", chat=True),
            _card("mistral-medium", chat=True),
            _card("mistral-medium-latest", chat=True),
            _card("mistral-alpha-latest", chat=True),
            _card("mistral-small", chat=True),
        ]
    )

    ids = api.filter_chat_models(model_list)

    assert ids[:2] == ["mistral-alpha-latest", "mistral-medium-latest"]
    assert [i for i in ids if not i.endswith(suffix)] == [
        "mistral-medium",
        "mistral-small",
        "mistral-zeta",
    ]


async def test_lazy_client_created_once(hass) -> None:
    """Le client est construit paresseusement (aucun appel au setup)."""
    entry = _entry()
    entry.add_to_hass(hass)
    state = hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(
        entry
    )

    fake_client = SimpleNamespace(models=SimpleNamespace(list=MagicMock()))
    with patch.object(api, "Mistral", return_value=fake_client) as mistral_cls:
        client_first = state.get_client()
        client_second = state.get_client()

    assert client_first is fake_client
    assert client_second is fake_client
    mistral_cls.assert_called_once_with(api_key=FAKE_API_KEY)


async def test_async_list_models_runs_in_executor(hass) -> None:
    """`async_list_models` appelle `client.models.list()` hors boucle event."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    list_mock = MagicMock(
        return_value=_model_list([_card("mistral-small-latest", chat=True)])
    )
    fake_client = SimpleNamespace(models=SimpleNamespace(list=list_mock))
    with (
        patch.object(api, "Mistral", return_value=fake_client),
        patch.object(
            api, "filter_chat_models", return_value=["mistral-small-latest"]
        ) as filter_mock,
    ):
        ids = await api.async_list_chat_model_ids(hass, entry)

    list_mock.assert_called_once_with()
    filter_mock.assert_called_once_with(list_mock.return_value)
    assert ids == ["mistral-small-latest"]


async def test_async_list_models_wraps_sdk_errors(hass) -> None:
    """Toute erreur SDK/réseau est enrobée dans `MistralApiError`."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    failing = SimpleNamespace(
        models=SimpleNamespace(list=MagicMock(side_effect=RuntimeError("panic")))
    )
    with (
        patch.object(api, "Mistral", return_value=failing),
        pytest.raises(api.MistralApiError),
    ):
        await api.async_list_chat_model_ids(hass, entry)


async def test_chat_complete_passes_model_and_messages(hass) -> None:
    """`chat.complete` reçoit le modèle, les messages et les outils."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    complete = MagicMock(return_value=SimpleNamespace(choices=[]))
    fake_client = SimpleNamespace(chat=SimpleNamespace(complete=complete))
    messages = [{"role": "user", "content": "Salut"}]
    tools = [SimpleNamespace(name="HassTurnOn")]

    with patch.object(api, "Mistral", return_value=fake_client):
        await api.async_chat_complete(
            hass,
            entry,
            model="mistral-small-latest",
            messages=messages,
            tools=tools,
        )

    complete.assert_called_once_with(
        model="mistral-small-latest", messages=messages, tools=tools
    )


async def test_get_data_is_lazy_and_cached(hass) -> None:
    """`get_data` crée l'état partagé à la demande, sans construire de client."""
    entry = _entry()
    entry.add_to_hass(hass)

    data = api.get_data(hass, entry)
    assert data is api.get_data(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id] is data
    assert data.client is None  # paresseux : aucune construction au setup


async def test_async_chat_complete_wraps_sdk_errors(hass) -> None:
    """Toute erreur SDK/réseau de chat est enrobée dans `MistralApiError`."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    failing = SimpleNamespace(
        chat=SimpleNamespace(complete=MagicMock(side_effect=RuntimeError("panic")))
    )
    with (
        patch.object(api, "Mistral", return_value=failing),
        pytest.raises(api.MistralApiError),
    ):
        await api.async_chat_complete(
            hass,
            entry,
            model="mistral-small-latest",
            messages=[{"role": "user", "content": "Salut"}],
        )


async def test_speech_complete_passes_model_text_and_format(hass) -> None:
    """`audio.speech.complete` reçoit le modèle, le texte et le format."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    complete = MagicMock(return_value=SimpleNamespace(audio_data="aGVsbG8="))
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(complete=complete))
    )

    with patch.object(api, "Mistral", return_value=fake_client):
        response = await api.async_speech_complete(
            hass,
            entry,
            model="voxtral-mini-tts-3-0",
            text="Bonjour",
            response_format="mp3",
        )

    assert response is complete.return_value
    complete.assert_called_once_with(
        model="voxtral-mini-tts-3-0", input="Bonjour", response_format="mp3"
    )


async def test_speech_complete_passes_voice_only_when_set(hass) -> None:
    """`voice_id` n'est transmis que s'il est renseigné (vide = défaut API)."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    complete = MagicMock(return_value=SimpleNamespace(audio_data="aGVsbG8="))
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(complete=complete))
    )

    with patch.object(api, "Mistral", return_value=fake_client):
        await api.async_speech_complete(
            hass,
            entry,
            model="voxtral-mini-tts-3-0",
            text="Bonjour",
            response_format="mp3",
            voice_id="",
        )
        assert "voice_id" not in complete.call_args.kwargs

        await api.async_speech_complete(
            hass,
            entry,
            model="voxtral-mini-tts-3-0",
            text="Bonjour",
            response_format="mp3",
            voice_id="voice-42",
        )
        assert complete.call_args.kwargs["voice_id"] == "voice-42"


async def test_async_speech_complete_wraps_sdk_errors(hass) -> None:
    """Toute erreur SDK/réseau de TTS est enrobée dans `MistralApiError`."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api.MistralVoiceData(entry)

    failing = SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(
                complete=MagicMock(side_effect=RuntimeError("panic"))
            )
        )
    )
    with (
        patch.object(api, "Mistral", return_value=failing),
        pytest.raises(api.MistralApiError),
    ):
        await api.async_speech_complete(
            hass,
            entry,
            model="voxtral-mini-tts-3-0",
            text="Bonjour",
            response_format="mp3",
        )


async def test_shared_session_is_not_dedicated(hass) -> None:
    """Aucune session aiohttp dédiée : on réutilise celle de Home Assistant."""
    assert api.async_get_shared_http_session(hass) is async_get_clientsession(hass)
