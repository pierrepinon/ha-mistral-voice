# Changelog

Toutes les évolutions notables de ce projet sont documentées ici.
Le format s'inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/)
et le versionnage suit [SemVer](https://semver.org/lang/fr/).

## [1.2.1] — Synthèse vocale : voix de repli obligatoire

### Corrigé

- **L'audio n'était jamais joué sur l'enceinte** : les modèles Voxtral
  TTS 3 (`voxtral-mini-tts-3-0`) refusent les requêtes sans voix
  (`400 Either ref_audio or voice must be provided`, cf. « Common
  issues » de la doc Mistral — le v2 `voxtral-mini-tts-2603` tolérait
  l'omission). L'option « Voix de synthèse vocale » vide envoyait
  donc chaque appel `POST /v1/audio/speech` vers une erreur 400,
  masquée en échec générique. Une option vide retombe maintenant sur
  la voix de repli intégrée `voice_592` (Marie, française,
  bibliothèque de voix préconfigurées Voxtral 3) ; une voix saisie
  continue d'être transmise telle quelle. Tests de régression sur les
  deux chemins (avec flux et sans flux) : l'appel API doit toujours
  recevoir un `voice_id`.

- **Réponse parfois sans texte (erreur « unknown » dans le debug)** :
  le plafond `max_tokens` passe de 150 à 1024. Ces tokens sont un
  plafond de sécurité, pas un objectif — la concision vient du
  pre-prompt. À 150, les modèles qui raisonnent dans le même budget
  (tokens de réflexion avant le texte) pouvaient épuiser le plafond
  sans produire le moindre texte : le tour se terminait sans contenu
  assistant et l'agent renvoyait sa réponse polie d'erreur.

## [1.2.0] — Mémoire de conversation et streaming (agent & TTS)

### Ajouté

- **Mémoire de conversation multi-tours** : l'agent devient une entité
  `conversation.ConversationEntity` (pattern Home Assistant 2026.10 déjà
  utilisé par openai_conversation). Chaque énoncé est traité avec le
  `ChatLog` persistant de la conversation — les tours précédents
  (questions, réponses, appels d'outils et résultats) sont renvoyés au
  modèle, plafonnés à 40 messages coupés sur une frontière de tour
  utilisateur. « Et à côté ? », « allume-la aussi » fonctionnent.
- **Agent en flux** : la réponse du modèle arrive en SSE
  (`chat.stream_async`, POST /v1/chat/completions en flux) et les deltas
  de contenu sont poussés dans le `ChatLog` — le pipeline Assist peut
  les diffuser au TTS au fil de l'eau (`.supports_streaming`). Les
  arguments des tool calls sont assemblés avant exécution côté HA.
- **TTS en flux** : la plateforme `tts` implémente
  `async_stream_tts_audio` (POST /v1/audio/speech SSE,
  `speech.audio.delta`) — le satellite vocal commence à jouer avant la
  fin de la génération. L'option Home Assistant `preferred_format` est
  déclarée supportée : l'API produit directement le format du
  consommateur (WAV 16 kHz/16 bits/mono pour le Voice PE, MP3 par
  défaut) sans conversion ffmpeg hasardeuse sur du PCM brut.
- Compatibilité pipelines : l'agent reste enregistré sous l'id
  d'entrée en plus de son entité — les pipelines existants continuent
  de fonctionner ; la sélection « Mistral Voice » est inchangée.
- La suite de tests passe de 81 à 92 : l'agent conversation est
  retraversé sous la forme entité (mémoire sur deux tours, deltas
  diffusés au listener du pipeline, arguments de tool call assemblés,
  plafond d'historique sur frontière utilisateur, erreurs API/LLM
  polies, vide → poli), la plateforme TTS gagne le chemin en flux
  (6 tests) et le client API les deux fonctions streaming (3 tests).

### Modifié

- L'agent expose maintenant le contrôle (`ConversationEntityFeature.
  CONTROL`) : les pipelines préfèrent les agents qui contrôlent la
  maison.
- Le pre-prompt est placé avec le prompt de l'API LLM Assist et la
  date/heure (composition standard du `ChatLog`) — le pre-prompt
  utilisateur reste en tête de message système.

## [1.1.0] — Synthèse vocale (TTS)

### Ajouté

- Fournisseur **TTS** pour le Pipeline Assist : l'entité
  **« Synthèse vocale (Mistral) »** synthétise la réponse de l'agent de
  conversation en un appel unique `POST /v1/audio/speech`
  (`client.audio.speech.complete`, SDK `mistralai==3.7.0`), modèle de
  synthèse vocale **Voxtral TTS 3** (`voxtral-mini-tts-3-0`, la v2
  `voxtral-mini-tts-2603` étant dépréciée par Mistral). L'audio renvoyé
  en base64 (`audio_data`) est décodé et rendu au pipeline au format
  MP3, joué par le satellite vocal. Comme le fournisseur STT, la langue
  supportée suit l'option `language` et l'entité partage le même appareil
  de service.
- Option **« Voix de synthèse vocale »** : identifiant d'une voix Mistral
  (préréglée ou clonée dans le compte) passé à `POST /v1/audio/speech` ;
  vide = voix par défaut de l'API *(précisé en 1.2.1 : Voxtral TTS 3
  exige une voix, la valeur vide retombe en fait sur la voix de repli
  `voice_592`)*.
- En cas d'échec (API injoignable, modération du contenu), l'erreur
  remonte au pipeline en `HomeAssistantError` générique : jamais de clé
  API, de texte parlé ni de détail de requête dans les logs.
- Onze nouveaux tests : nominal (bytes MP3 décodés), voix transmise
  (absente de l'appel quand vide), erreurs API, réponse sans audio et
  base64 invalide en `HomeAssistantError`, langues suivant les options,
  chargement/déchargement de la plateforme `tts` (appareil partagé avec
  le STT), plus trois tests du client API (`voice_id` conditionnel,
  enrobage des erreurs SDK).

## [1.0.2] — Réponses en liste de parties (modèles raisonnement)

### Corrigé

- Les modèles raisonnement hébergés par Mistral (ex.
  `zai-glm-5-3-flash`) renvoient `message.content` sous forme d'une
  **liste de parties typées** au lieu d'une chaîne : le texte de la
  réponse était perdu et remplacé par la réponse d'erreur polie
  (« Désolé, je n'ai pas réussi à traiter votre demande… »).
  `_message_content` concatène désormais les parties de type `text` et
  ignore les autres (le raisonnement n'est jamais parlé ni renvoyé au
  modèle, que l'API Mistral rejette) ; dans la boucle de tool calling,
  le message assistant est rejoué en texte normalisé. Comportement
  inchangé pour les modèles renvoyant une simple chaîne.
- Trois nouveaux tests : parties texte parlées (raisonnement ignoré),
  replay assistant normalisé, repli poli sans aucune partie texte.

## [1.0.1] — Diagnostic du chemin conversation

### Ajouté

- Instrumentation en log **debug** du chemin conversation de l'agent :
  modèle sélectionné et nombre d'outils exposés en début d'énoncé, puis
  à chaque itération de tool calling le `finish_reason`, le nombre de
  `tool_calls` et la taille du contenu de réponse, et la taille du
  résultat JSON de chaque outil exécuté. Permet de diagnostiquer en
  direct le cas d'un modèle de raisonnement tronqué par `max_tokens`
  (contenu vide, `finish_reason=length`) qui ne laissait jusqu'ici
  **aucune trace** dans les logs. Jamais de secret ni de détail de
  requête dans ces lignes (clé API, pre-prompt et texte brut exclus).

## [1.0.0] — MVP complet

Première version complète de l'intégration `mistral_voice` : assistant vocal
Home Assistant 100 % cloud via l'API Mistral, pour les satellites
[Home Assistant Voice PE](https://www.home-assistant.io/voice-pe/).

### Ajouté

#### Intégration et entrée de configuration
- Intégration custom HACS, domaine `mistral_voice` (`integration_type:
  service`, `config_flow: true`), chargeable par Home Assistant 2026.10+.
- Config flow minimal : saisie de la clé API Mistral (champ masqué) ;
  une seule instance ; la clé est stockée uniquement dans `entry.data`
  (chiffrée au repos par HA) et n'apparaît jamais en clair dans le code,
  les logs, les commits ou les captures d'écran.
- Client API Mistral unique par entrée (SDK `mistralai[realtime]==3.7.0`),
  construit paresseusement dans `hass.data` : `GET /v1/models`
  (`models.list()`) et `POST /v1/chat/completions` (`chat.complete()`),
  erreurs réseau enrobées d'une exception générique qui n'expose ni secret
  ni détail de requête.
- Déchargement propre de l'entrée (plateformes, désenregistrement de
  l'agent de conversation, retrait de `hass.data`).

#### Transcription en flux (STT)
- Fournisseur STT (`stt.py`), entité `SpeechToTextEntity` « Transcription
  (Mistral) » branchée sur le WebSocket temps réel de l'API Mistral
  (`wss://api.mistral.ai/v1/audio/transcriptions/realtime`,
  modèle `voxtral-mini-transcribe-realtime-3`, PCM linéaire 16 bits mono).
- Séquence d'événements respectée : config de session → `input_audio.append`
  (chunks) → `input_audio.end` → `transcription.text.delta` (partials) →
  `transcription.segment` → `transcription.done` (texte complet).
- Une connexion WebSocket par énoncé, fermée proprement dans un `finally` ;
  erreur de session → `SpeechResultState.ERROR` sans aucun secret dans les
  logs ; pas de barge-in côté streaming (les partials sont ignorés,
  l'interruption reste gérée par le VAD/AEC du satellite).
- Options `language` (défaut `fr`) et `target_streaming_delay_ms`
  (plage 80–2400 ms, défaut 480) transmises à la session.

#### Agent de conversation et tool calling
- Agent de conversation enregistré auprès du Pipeline Assist via
  `conversation.async_set_agent` (désenregistré au unload via
  `async_unset_agent`), langues supportées : MATCH_ALL.
- Conversion des outils LLM HA (API Assist : `HassTurnOn`… issus des entités
  exposées à Assist) au schéma function-calling Mistral
  (`{"type": "function", "function": {…}}`) — le mécanisme d'outils de HA
  n'est pas réimplémenté ; schéma des paramètres rendu en JSON Schema,
  repli sûr en cas d'échec de conversion.
- Boucle de tool calling : `tool_calls` du modèle → exécution côté HA via
  `llm.async_get_api` / `api_instance.async_call_tool` → résultats renvoyés
  en messages `tool` → reboucle (limite 5 itérations) jusqu'au texte final.
- Définitions d'outils converties et pre-prompt actif mis en cache
  (byte-stables entre les requêtes) ; réponses courtes, en français ;
  en cas d'erreur (API injoignable, outil en échec), réponse polie en
  français : pas d'exception exposée, jamais de secret.

#### Sélecteur de modèles dynamique
- Liste des modèles alimentée en direct par `GET /v1/models`, filtrée aux
  modèles de chat (`capabilities.completion_chat`), alias `-latest`
  en premier, valeur personnalisée permise — aucune liste statique en dur.
- Options flow complet : modèle de chat, case « Rafraîchir les modèles »
  (réinterroge `GET /v1/models` sans enregistrer), langue de transcription,
  délai de streaming cible (plage 80–2400 ms), pre-prompt surchargeable
  (vide = pre-prompt français intégré, texte exact de la spec #1).
- API injoignable pendant le rafraîchissement : erreur traduite
  (`models_fetch_failed`) et **valeur du modèle courant conservée**.

#### Packaging, documentation et qualité
- Packaging HACS complet : `hacs.json`, manifest complet (domain, name,
  version, `iot_class`, requirements épinglés, `config_flow`,
  `integration_type`, documentation, issue_tracker, codeowners),
  icônes 256×256.
- README en français : badges (HACS, version, licence), installation
  one-click via HACS, schéma ASCII de l'architecture
  (Voice PE → Pipeline Assist HA → `api.mistral.ai`, sortant uniquement),
  section Configuration, section Fonctionnement, FAQ, tests manuels
  documentés, contrat API Mistral utilisé (WebSocket temps réel et
  chat/completions avec outils), section Développement.
- Traductions : `strings.json` + `translations/fr.json` couvrant le config
  flow, l'options flow, les entités et les erreurs — validées par hassfest.
- Licence Apache-2.0, décision d'architecture ADR-0001 (traitement 100 %
  cloud, HTTPS/WSS sortant uniquement).
- Suite de tests : unitaires (conversion des outils, filtrage des modèles
  + préférence `-latest`, pre-prompt par défaut, plage du délai
  80–2400) et d'intégration (`pytest-homeassistant-custom-component` :
  config flow, options flow, chemin d'erreur « API injoignable → valeur
  courante conservée », câblage des entités STT et agent, flux STT
  nominal/erreur/fermeture, boucle tool calling).
- CI GitHub Actions : hassfest + ruff sur chaque PR.
- Environnement de développement épinglé via `mise` (`.mise.toml`).
