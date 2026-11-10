# Traitement 100 % cloud via l'API Mistral

Contexte : HAOS avec satellite Voice PE, aucun GPU local, aucune exposition entrante de Home Assistant. Décision : tout le traitement vocal (transcription en flux et conversation) passe par `api.mistral.ai` en HTTPS/WSS sortant uniquement. La clé API ne vit que dans l'entrée de configuration HA (chiffrée au repos par HA) ; elle n'apparaît jamais dans le code, les logs, les commits ni les captures d'écran — l'utilisateur la copie depuis son gestionnaire de mots de passe (Bitwarden).

## Considered Options

- **Modèles locaux auto-hébergés** : rejeté — nécessite un GPU et qualité inférieure sur STT temps réel et conversation outillée.
- **Exposer HA en entrée** (webhook) : rejeté — surface d'attaque inutile alors que tout peut être tiré en sortie.

## Consequences

- Nécessite une connexion internet et une clé API Mistral valide ; la maison devient dépendante du cloud Mistral pour la voix.
- Confidentialité : le flux audio quitte la maison vers api.mistral.ai.
- Aucun port ouvert, aucune règle de pare-feu entrante à gérer.
