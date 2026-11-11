# ha-mistral-voice

Vocabulaire du domaine de l'intégration : assistant vocal Home Assistant 100 % cloud via l'API Mistral (satellite Voice PE, HAOS, domaine `mistral_voice`).

## Langage

**Satellite vocal** :
Appareil Home Assistant Voice PE qui capte le micro et joue la réponse audio. L'interruption (barge-in) est gérée par son VAD/AEC, pas par le flux de transcription.
_Avoid_: enceinte, micro, device vocal

**Pipeline Assist** :
Chaîne de traitement vocal de Home Assistant (STT → agent de conversation → TTS) alimentée par un satellite vocal ; chaque étape est fournie par l'intégration (STT en flux, agent Mistral Voice, fournisseur TTS).
_Avoid_: pipeline audio, chaîne vocale

**Fournisseur STT** :
Entité Home Assistant qui transcrit un flux audio en texte. Ici : transcription en flux via WebSocket vers l'API Mistral.
_Avoid_: service STT, transcripteur

**Transcription en flux** :
Session WebSocket temps réel vers l'API Mistral (`input_audio` → `input_audio.end` → deltas → `transcription.done`). Les résultats partiels ne servent pas au barge-in : le pipeline HA les ignore.
_Avoid_: streaming générique, partials

**Agent de conversation** :
Entité de conversation Home Assistant qui reçoit le texte transcrit, produit la réponse avec les outils et **tient la mémoire de la conversation** (historique multi-tours du ChatLog). Ici : appels en flux `chat/completions` Mistral, deltas diffusés au Pipeline Assist au fil de l'eau.
_Avoid_: chatbot, assistant IA générique, agent programmatique

**ChatLog** :
Historique de conversation persistant de Home Assistant (par identifiant de conversation) : questions, réponses, tool calls et résultats. L'agent y pousse ses deltas (`async_add_delta_content_stream`), le pipeline y branche le TTS.
_Avoid_: session, contexte de dialogue

**Fournisseur TTS** :
Entité Home Assistant qui synthétise en audio la réponse de l'agent de conversation, sans flux (service `tts.speak`, audio complet) ou en flux (`async_stream_tts_audio`, événements `speech.audio.delta` joués par le satellite au fil de l'eau). Ici : appels POST /v1/audio/speech au modèle de synthèse vocale, format choisi par l'option `preferred_format` du pipeline.
_Avoid_: lecteur audio, service TTS, voix (pour l'entité)

**Outil** :
Fonction Home Assistant exposée à Assist (ex. allumer une entité), convertie au schéma function-calling de Mistral puis exécutée côté HA ; le résultat repart en message `tool`.
_Avoid_: fonction, action, tool générique

**Pre-prompt** :
Instruction système envoyée à chaque appel du modèle. Texte par défaut en français (constante), surchargeable dans les options de l'intégration.
_Avoid_: system prompt figé, personnalité

**Sélecteur de modèles** :
Liste des modèles de chat alimentée dynamiquement par `GET /v1/models`, alias `-latest` privilégiés, jamais de liste statique en dur.
_Avoid_: modèles en dur, dropdown fixe
