# Changelog

Toutes les évolutions notables de ce projet sont documentées ici.
Le format s'inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/)
et le versionnage suit [SemVer](https://semver.org/lang/fr/).

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
