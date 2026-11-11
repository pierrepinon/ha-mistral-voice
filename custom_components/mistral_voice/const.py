"""Constantes de l'intégration Mistral Voice."""

from __future__ import annotations

DOMAIN = "mistral_voice"

MANUFACTURER = "pierrepinon"

# Clé de config (entrée de configuration) — jamais en clair dans les logs.
CONF_API_KEY = "api_key"

# Options
CONF_LANGUAGE = "language"
CONF_STREAMING_DELAY_MS = "target_streaming_delay_ms"
CONF_SYSTEM_PROMPT = "system_prompt"
CONF_MODEL = "model"
CONF_VOICE = "voice"
CONF_REFRESH_MODELS = "refresh_models"

DEFAULT_LANGUAGE = "fr"
DEFAULT_STREAMING_DELAY_MS = 480
MIN_STREAMING_DELAY_MS = 80
MAX_STREAMING_DELAY_MS = 2400

# Modèles par défaut (la liste complète vient de GET /v1/models, jamais en dur).
DEFAULT_MODEL = "mistral-small-latest"

# Voix de synthèse vocale : identifiant d'une voix (préréglée ou clonée) du
# compte Mistral ; vide = voix par défaut de l'API.
DEFAULT_VOICE = ""

# Synthèse vocale : appel POST /v1/audio/speech. Le modèle de TTS est fixe
# (seul modèle de l'offre Mistral) ; la réponse est demandée en MP3
# (extension renvoyée au Pipeline Assist, l'audio part décodé en bytes).
TTS_MODEL = "voxtral-mini-tts-2603"
TTS_AUDIO_FORMAT = "mp3"

# Langues de transcription pour le champ `language` des options.
SUPPORTED_LANGUAGES = {"fr", "en", "de", "es", "it"}

# Pre-prompt par défaut envoyé au modèle de conversation : texte exact de la
# spec (issue #1), surchargeable dans les options (vide = revenir à ce texte).
DEFAULT_SYSTEM_PROMPT = (
    "Tu es l'assistant vocal de la maison, joignable par la voix "
    "via Home Assistant. Règles : toujours répondre en français, à "
    "l'oral, concis (phrases courtes, pas de listes sauf si "
    "demandé) ; contrôler la maison uniquement via les outils "
    'fournis, jamais répondre "je ne peux pas" quand un outil '
    "existe ; ne jamais inventer l'état d'un appareil, le lire "
    "d'abord ; demander une confirmation orale avant toute action "
    "irréversible (alarme, portail, suppression) ; en cas "
    "d'ambiguïté, demander une précision courte ; ne jamais "
    "mentionner que tu es un modèle ou que tu utilises des outils."
)

# Conversation : appel POST /v1/chat/completions.
MAX_TOKENS = 150
TOOL_CHOICE_AUTO = "auto"

# Boucle de tool calling : nombre max d'allers-retours avec le modèle,
# pour éviter les boucles infinies (tool_call -> tool -> tool_call…).
MAX_TOOL_ITERATIONS = 5

# Réponses d'erreur polies (jamais de message d'exception : ni la clé API
# ni les détails de requête ne sont exposés à l'utilisateur).
ERROR_RESPONSE_TEXT = (
    "Désolé, je n'ai pas réussi à traiter votre demande. "
    "Veuillez réessayer dans un instant."
)
ERROR_LOOP_LIMIT_TEXT = (
    "Désolé, cette demande est trop complexe à traiter pour l'instant."
)
