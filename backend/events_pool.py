"""Contextual suggestions — generic templates, admin-approved.

No scenario-specific prescripted events. Each suggestion is a neutral
template the admin can load into the compose box and edit before sending.
"""
from __future__ import annotations

from .models import RunState


def compute_suggestions(state: RunState) -> list[dict]:
    suggestions: list[dict] = []
    day = state.clock.day

    suggestions.append({
        "id": "call_assembly",
        "label": "🏛️ Convoca assemblea straordinaria",
        "body": (
            "Convoco un'assemblea condominiale straordinaria (seconda "
            "convocazione). Ordine del giorno: le questioni aperte del "
            "condominio. La vostra presenza è necessaria."
        ),
        "target": "main",
    })
    suggestions.append({
        "id": "request_feedback",
        "label": "🗣️ Chiedi opinione al gruppo",
        "body": (
            "Buongiorno a tutti. Prima di prendere decisioni importanti "
            "vorrei sentire le vostre opinioni. Scrivete pure qui oppure in "
            "privato, ascolto volentieri tutti i punti di vista."
        ),
        "target": "main",
    })
    suggestions.append({
        "id": "remind_progress",
        "label": "⏰ Sollecita una decisione",
        "body": (
            "Rimango in attesa di una posizione condivisa. Più aspettiamo, "
            "più la situazione si complica. Fatemi sapere come volete procedere."
        ),
        "target": "main",
    })
    suggestions.append({
        "id": "morosi",
        "label": "📋 Segnala stato pagamenti",
        "body": (
            "Vi ricordo che alcuni condomini sono indietro con le quote. "
            "Chiedo a chi fosse in questa situazione di mettersi in regola "
            "o contattarmi per definire un piano di rientro."
        ),
        "target": "main",
    })

    # Motion-based
    open_motions = [m for m in state.motions if m.status == "open"]
    if open_motions:
        m = open_motions[0]
        suggestions.append({
            "id": f"close_motion_{m.id}",
            "label": f"🗳️ Chiudi votazione \"{m.title[:20]}…\"",
            "body": None,
            "action": "close_motion",
            "motion_id": m.id,
            "target": "main",
        })

    return suggestions
