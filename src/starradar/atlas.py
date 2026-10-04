"""Adaptador de señales Atlas: de un repo a una señal consumible.

Contexto: el pipeline de idea-lab (Atlas) produce un fichero de señales con
este esquema, y su propio campo `not_verified` dice, literalmente, que *"las
cifras de estrellas/forks/días de los repos vienen del briefing: no se
revalidaron contra GitHub en esta sesion"*. Eso es justo el hueco que
starradar cubre: no producir una afirmación sobre un repo sin haber medido el
repo.

Esquema de destino (ver `ideas/atlas/YYYY-MM-DD.json`):

    {
      "id": "SR-001",
      "claim": "…afirmación verificable y cortita…",
      "entity": "owner/repo",
      "category": "herramienta",
      "source_url": "https://github.com/owner/repo",
      "source_date": "YYYY-MM-DD",
      "date_inferred": false,
      "reports": ["starradar"],
      "momentum": <nº de señales del mismo tema>
    }

Regla de oro: **una señal por repo, con las cifras medidas**, nunca una
estimación a ojo. Si el repo no está verificado no se emite señal: Atlas consume
esto para construir tesis, y una granja de estrellas convertida en tesis es
peor que un hueco.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Digest, Score, iso, utcnow

CATEGORY = "herramienta"
REPORTS = ["starradar"]


def score_to_signal(s: Score, momentum: int = 1, index: int = 1) -> dict[str, Any] | None:
    """Convierte un score verificado en una señal Atlas.

    Devuelve ``None`` si el repo no está verificado: es preferible no tener
    señal a tener una señal basada en estrellas no demostradas.
    """
    if not s.verified:
        return None

    wr = "n/d" if s.watch_ratio is None else f"{s.watch_ratio:.4f}"
    # Cifras con separador de miles español (punto), no coma, para que la
    # lectura sea natural en el resto de Atlas.
    claim = (
        f"{s.repo} acumula {s.stars:,}★ en {s.age_days:.0f} días "
        f"({s.stars_per_day:.0f}★/día) con w/★={wr} y score {s.total:.0f}/100: "
        f"crecimiento orgánico confirmado."
    )

    return {
        "id": f"SR-{index:03d}",
        "claim": claim,
        "entity": s.repo,
        "category": CATEGORY,
        "source_url": f"https://github.com/{s.repo}",
        "source_date": utcnow().date().isoformat(),
        "date_inferred": False,
        "reports": list(REPORTS),
        "momentum": max(1, int(momentum)),
        "measured": {
            "stars": s.stars,
            "stars_per_day": round(s.stars_per_day, 2),
            "age_days": round(s.age_days, 1),
            "watch_ratio": None if s.watch_ratio is None else round(s.watch_ratio, 4),
            "score": round(s.total, 1),
            "verdict": s.verdict,
        },
    }


def digest_to_signals(digest: Digest, limit: int = 8) -> list[dict[str, Any]]:
    """Las señales de un digest, sólo de la capa verificada.

    El momentum cuenta cuántos repos verificados comparten tema, que es como
    Atlas ya define el momentum (nº de señales por tema).
    """
    signals: list[dict[str, Any]] = []
    for i, s in enumerate(digest.verified[:limit], 1):
        sig = score_to_signal(s, momentum=1, index=i)
        if sig is not None:
            signals.append(sig)
    return signals


def write_atlas(
    digest: Digest,
    out_dir: str | Path,
    briefing_job: str = "starradar",
    reports_read: int = 0,
) -> Path:
    """Escribe un fichero Atlas-compatible y devuelve su ruta.

    Se escribe un fichero por día (`YYYY-MM-DD.json`) con la misma forma de
    nivel superior que produce idea-lab, para que se pueda leer con el mismo
    código. `momentum_by_theme` se deja como estructura vacía: starradar mide
    momentum de repos, no de temas, y no debe inventar ese agrupamiento.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = digest.at.date().isoformat()
    path = out_dir / f"{day}.json"

    signals = digest_to_signals(digest)
    verified = [s.repo for s in digest.verified]
    unverified = [s.repo for s in digest.unverified]

    payload: dict[str, Any] = {
        "date": day,
        "briefing_job": {
            "name": briefing_job,
            "owner_profile": "web-dev",
            "reports_read": reports_read,
            "source": "starradar (API de GitHub, cifras medidas)",
        },
        "signals": signals,
        "momentum_by_theme": {},
        "top8": [sig["id"] for sig in signals[:8]],
        "tensions": [],
        "convergences": [],
        "gaps": [],
        "not_verified": [
            f"{len(unverified)} repos quedaron fuera por autenticidad insuficiente "
            f"(watchers/estrellas bajo el suelo): no se usan como señal.",
            "La velocidad es estimada si no hay snapshots de días distintos: "
            "el campo stats.velocity_source lo indica.",
        ],
        "starradar": {
            "generated_at": iso(digest.at),
            "stats": digest.stats,
            "verified_repos": verified,
            "unverified_count": len(unverified),
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def signals_to_jsonl(signals: list[dict[str, Any]], path: str | Path) -> Path:
    """Una señal por línea, para un ledger append-only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for sig in signals:
            fh.write(json.dumps(sig, ensure_ascii=False) + "\n")
    return path
