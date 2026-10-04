"""Servidor MCP por stdio: el radar como herramienta de cualquier agente.

Por qué MCP y no sólo CLI: el objetivo es que *cualquier* perfil de Hermes (o un
agente externo, o Claude/Codex vía ACP) pueda preguntar "¿qué repos nuevos están
subiendo?" y obtener datos ya filtrados y explicados, sin reimplementar el
scoring ni pelearse con la API de GitHub.

Herramientas expuestas:

* ``radar_scan``      — ejecuta un ciclo y devuelve el digest.
* ``radar_top``       — el ranking, sin ejecutar nada (lee la BD).
* ``radar_explain``   — por qué puntúa así un repo.
* ``radar_moves``     — qué ha más crecido desde el último snapshot.
* ``radar_status``    — estado: cuántos snapshots, velocidad medida o estimada.

Todas devuelven JSON en texto. Es lo que los agentes procesan mejor y evita
depender del cliente para el formato. Los errores se devuelven como texto con
``isError`` para que el agente pueda reintentar con otros parámetros en vez de
recibir una excepción opaca.
"""

from __future__ import annotations

import sys
from typing import Any

from .config import Settings
from .github import GitHubClient, GitHubError
from .pipeline import run_scan, top_changes
from .scoring import score
from .store import Store

SERVER_NAME = "starradar"


def _digest_payload(digest: Any, limit: int = 20) -> dict[str, Any]:
    return {
        "at": digest.at.isoformat(),
        "stats": digest.stats,
        "verified": [s.to_dict() for s in digest.verified[:limit]],
        "unverified_count": len(digest.unverified),
        "unverified_preview": [s.to_dict() for s in digest.unverified[:5]],
        "note": (
            "verified = crecimiento real y estrellas orgánicas. "
            "unverified = no usar como señal; w/★ bajo el suelo significa "
            "estrellas sin crecimiento demostrado."
        ),
    }


def tool_scan(args: dict[str, Any]) -> dict[str, Any]:
    settings = Settings.from_env()
    if args.get("offline"):
        settings.live = False
    topics = tuple(args["topics"]) if args.get("topics") else None
    digest = run_scan(
        settings,
        topics=topics,
        window_days=args.get("window_days"),
        star_floor=args.get("star_floor"),
        languages=tuple(args.get("languages") or ()),
    )
    return _digest_payload(digest, limit=int(args.get("limit", 20)))


def tool_top(args: dict[str, Any]) -> dict[str, Any]:
    """Lee el último digest sin tocar la red. Instantáneo."""
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    raw = store.last_digest()
    if raw is None:
        return {
            "empty": True,
            "message": (
                "Todavía no hay digest. Ejecuta radar_scan (o `starradar scan`) "
                "para descubrir repos."
            ),
        }
    limit = int(args.get("limit", 20))
    return {
        "at": raw.get("at"),
        "stats": raw.get("stats", {}),
        "verified": raw.get("verified", [])[:limit],
        "unverified_count": len(raw.get("unverified", [])),
        "unverified_preview": raw.get("unverified", [])[:5],
    }


def tool_explain(args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("repo", "")).strip()
    if "/" not in name:
        return {"error": "usa el formato owner/repo"}
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    repo = store.get_repos([name]).get(name)
    if repo is None and settings.live and settings.token:
        try:
            detail = GitHubClient(settings).repo_detail(name)
            if detail is not None:
                repo = detail
                store.upsert_repos([detail])
        except GitHubError as exc:
            return {"error": str(exc), "repo": name}
    if repo is None:
        return {
            "error": f"{name} no está en la base local y no se pudo consultar",
            "hint": "ejecuta radar_scan primero",
        }
    history = store.history([name]).get(name, [])
    s = score(repo, history)
    return {**s.to_dict(), "explanation": s.explain(), "url": repo.url}


def tool_moves(args: dict[str, Any]) -> dict[str, Any]:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    hours = int(args.get("hours", 24))
    moves = top_changes(store, since_hours=hours, limit=int(args.get("limit", 10)))
    return {
        "hours": hours,
        "moves": [{"repo": r, "stars_gained": d} for r, d in moves],
        "note": (
            "vacío si no hay historial; se necesitan snapshots de dos días distintos. "
            "La velocidad medida aparece en la primera ejecución del día siguiente."
        )
        if not moves
        else None,
    }


def tool_status(args: dict[str, Any]) -> dict[str, Any]:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    d = store.last_digest() or {}
    return {
        "home": str(settings.home),
        "token_configured": bool(settings.token),
        "repos": len(store.all_repos(limit=10000)),
        "snapshots": store.snapshot_count(),
        "tracked_days": store.tracked_days(),
        "velocity": "medida" if store.tracked_days() >= 2 else "estimada (aún sin histórico)",
        "last_digest_at": d.get("at"),
        "thresholds": {
            "authenticity_floor": 0.0075,
            "authenticity_strong": 0.015,
            "note": "watchers/estrellas; forks/estrellas NO discrimina (ver docs/scoring.md)",
        },
    }


# --------------------------------------------------------------------------


def _schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "radar_scan",
            "description": (
                "Descubre repos nuevos de GitHub con estrellas en crecimiento y "
                "devuelve el ranking filtrado por autenticidad. Usa la red."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "description": "antigüedad máxima en días"},
                    "star_floor": {"type": "integer", "description": "suelo de estrellas"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "languages": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                    "offline": {"type": "boolean", "description": "sin red: recalcula la BD"},
                },
            },
        },
        {
            "name": "radar_top",
            "description": "Devuelve el último ranking guardado. No usa red. Instantáneo.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
        {
            "name": "radar_explain",
            "description": "Explica por qué un repo puntúa así, con sus cifras reales.",
            "inputSchema": {
                "type": "object",
                "properties": {"repo": {"type": "string", "description": "owner/repo"}},
                "required": ["repo"],
            },
        },
        {
            "name": "radar_moves",
            "description": "Repos que más estrellas han ganado desde el último snapshot.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "radar_status",
            "description": "Estado local: snapshots, si la velocidad es medida, umbrales.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


HANDLERS = {
    "radar_scan": tool_scan,
    "radar_top": tool_top,
    "radar_explain": tool_explain,
    "radar_moves": tool_moves,
    "radar_status": tool_status,
}


def _serve_stdio() -> int:
    """Implementación MCP sobre stdio, sin dependencias externas.

    Hablar JSON-RPC a mano evita obligar a instalar el SDK `mcp`. El protocolo
    es pequeño para este caso: initialize / tools/list / tools/call. Se
    implementa lo justo, con la especificación correcta, para que Hermes lo
    registre y anyote como cualquier otro servidor.

    Si algún día se prefiere el SDK, este módulo es el que se sustituye: los
    handlers y los esquemas no cambian.
    """
    from ._jsonrpc import JsonRpcStdio

    rpc = JsonRpcStdio(SERVER_NAME, _schemas(), HANDLERS, _tool_version())
    return rpc.serve_forever()


def _tool_version() -> str:
    from . import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(_tool_version())
        return 0
    # El log va a stderr: stdout es el canal del protocolo.
    try:
        return _serve_stdio()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
