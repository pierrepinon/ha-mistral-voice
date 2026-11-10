"""Garantie de fidélité : `DEFAULT_SYSTEM_PROMPT` = texte exact de la spec.

Ce test copie volontairement le texte de l'issue #1 en dur : toute
modification de `const.py` sans mise à jour de la spec fait échouer la
suite (et inversement), pour éviter que le pre-prompt ne dérive.
"""

from __future__ import annotations

from custom_components.mistral_voice.const import DEFAULT_SYSTEM_PROMPT

# Texte exact du « Pre-prompt par défaut » dans l'issue #1.
SPEC_DEFAULT_SYSTEM_PROMPT = (
    "Tu es l'assistant vocal de la maison, joignable par la voix via Home "
    "Assistant. Règles : toujours répondre en français, à l'oral, concis "
    "(phrases courtes, pas de listes sauf si demandé) ; contrôler la maison "
    'uniquement via les outils fournis, jamais répondre "je ne peux pas" '
    "quand un outil existe ; ne jamais inventer l'état d'un appareil, le "
    "lire d'abord ; demander une confirmation orale avant toute action "
    "irréversible (alarme, portail, suppression) ; en cas d'ambiguïté, "
    "demander une précision courte ; ne jamais mentionner que tu es un "
    "modèle ou que tu utilises des outils."
)


def test_default_system_prompt_matches_spec() -> None:
    """La constante est le texte exact (identique octet par octet) de #1."""
    assert DEFAULT_SYSTEM_PROMPT == SPEC_DEFAULT_SYSTEM_PROMPT
