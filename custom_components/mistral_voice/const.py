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
CONF_MAX_TOKENS = "max_tokens"
CONF_REFRESH_MODELS = "refresh_models"

DEFAULT_LANGUAGE = "fr"
DEFAULT_STREAMING_DELAY_MS = 480
MIN_STREAMING_DELAY_MS = 80
MAX_STREAMING_DELAY_MS = 2400

# Modèles par défaut (la liste complète vient de GET /v1/models, jamais en dur).
DEFAULT_MODEL = "mistral-small-latest"

# Voix de synthèse vocale : identifiant d'une voix (préréglée ou clonée) du
# compte Mistral. Voxtral TTS 3 REFUSE les requêtes sans voix (400 « Either
# ref_audio or voice must be provided ») : l'option vide retombe sur cette
# voix de repli documentée (Marie, française — cf. bibliothèque de voix
# préconfigurées Voxtral 3 de la doc Mistral).
DEFAULT_VOICE = "voice_592"

# Synthèse vocale : appel POST /v1/audio/speech. Le modèle de TTS est fixe :
# Voxtral TTS 3 (remplace le v2 `voxtral-mini-tts-2603`, déprécié par Mistral),
# qui garde les mêmes paramètres d'appel. L'option `voice` (vide = voix de
# repli `DEFAULT_VOICE`) est commune aux deux chemins audio :
# - sans flux (service tts.speak, cache) : réponse audio complète en base64 ;
# - en flux (async_stream_tts_audio, pipeline Assist / satellite) :
#   événements `speech.audio.delta` au fil de l'eau. L'entité `tts` annonce
#   le format au pipeline (option Home Assistant `preferred_format` :
#   wav, mp3, flac ou opus ; toute autre valeur demandée retombe sur mp3)
#   et l'API produit directement ce format.
TTS_MODEL = "voxtral-mini-tts-3-0"

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

# Conversation : appels POST /v1/chat/completions en flux (SSE, SDK async).
# Plafond de sécurité par réponse — la concision vient du pre-prompt. Il
# doit laisser la place au raisonnement des modèles qui raisonnent dans
# le même budget (leurs tokens de réflexion précèdent le texte parlé) ;
# un plafond trop bas produit une réponse sans aucun texte -> réponse
# polie d'erreur. Valeur configurable dans les options (`max_tokens`).
DEFAULT_MAX_TOKENS = 1024
MIN_MAX_TOKENS = 64
MAX_MAX_TOKENS = 4096
TOOL_CHOICE_AUTO = "auto"

# Boucle de tool calling : nombre max d'allers-retours avec le modèle,
# pour éviter les boucles infinies (tool_call -> tool -> tool_call…).
MAX_TOOL_ITERATIONS = 5

# Mémoire de conversation : nombre max de messages (hors pre-prompt)
# renvoyés au modèle ; la coupure se fait sur une frontière de tour
# utilisateur pour ne jamais séparer un appel d'outil de sa réponse.
MAX_HISTORY_MESSAGES = 40

# Réponses d'erreur polies (jamais de message d'exception : ni la clé API
# ni les détails de requête ne sont exposés à l'utilisateur).
ERROR_RESPONSE_TEXT = (
    "Désolé, je n'ai pas réussi à traiter votre demande. "
    "Veuillez réessayer dans un instant."
)
ERROR_LOOP_LIMIT_TEXT = (
    "Désolé, cette demande est trop complexe à traiter pour l'instant."
)
