"""CLI de starradar — la superficie humana principal.

Diseño: sin dependencias obligatorias. Se renderiza con ANSI a mano para que
`starradar` funcione en una máquina con sólo Python y httpx; si `rich` está
instalado, se usa para el color. Se detecta el TTY: canalizado a fichero o
consumido por otro proceso, la salida es texto plano sin secuencias de escape,
porque un agente que se come los códigos ANSI recibe basura.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __version__
from .config import Settings
from .github import GitHubClient
from .models import Digest, Score
from .pipeline import run_scan, top_changes
from .store import Store

# -- color (sin dependencias) -------------------------------------------------

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


VERDICT_STYLE = {
    "destacado": (green, "◆"),
    "sólido": (cyan, "◇"),
    "temprano": (dim, "·"),
    "sin verificar": (yellow, "⚠"),
}


# -- render --------------------------------------------------------------------


def _bar(value: float, width: int = 10) -> str:
    """Barra de 0 a value(0-100), en caracteres ASCII/Unicode seguros."""
    filled = int(round(value / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def render_digest(digest: Digest, limit: int = 15, show_unverified: bool = True) -> str:
    """Render legible del digest. La salida que ve la persona."""
    out: list[str] = []
    st = digest.stats or {}

    out.append("")
    out.append(bold("  starradar") + dim(f"  ·  {digest.at:%Y-%m-%d %H:%M}"))
    src = st.get("velocity_source", "?")
    out.append(
        dim(
            f"  {st.get('n_discovered', 0)} descubiertos · "
            f"{st.get('n_verified', 0)} verificados · "
            f"velocidad: {src} ({st.get('tracked_days', 0)} días de datos)"
        )
    )

    if src.startswith("estimada"):
        out.append(
            yellow("  ⓘ  La velocidad aún es estimada: mañana habrá snapshots reales.")
        )
    out.append("")

    if not digest.verified:
        out.append(dim("  Sin repos que pasen el filtro de confianza todavía."))
        out.append("")

    # --- capa 1: verificados -------------------------------------------
    if digest.verified:
        out.append(bold("  VERIFICADOS") + dim("  (crecimiento real, estrellas orgánicas)"))
        out.append("")
        for i, s in enumerate(digest.verified[:limit], 1):
            out.append(_render_score_row(s, i))
        if len(digest.verified) > limit:
            out.append(dim(f"      … y {len(digest.verified) - limit} más"))
        out.append("")

    # --- capa 2: sin verificar ------------------------------------------
    if show_unverified and digest.unverified:
        out.append(
            yellow("  SIN VERIFICAR")
            + dim("  (estrellas sin crecimiento demostrado — no los tomes como señal)")
        )
        out.append("")
        for i, s in enumerate(digest.unverified[:5], 1):
            out.append(_render_score_row(s, i, muted=True))
        if len(digest.unverified) > 5:
            out.append(dim(f"      … y {len(digest.unverified) - 5} más"))
        out.append("")
        why = digest.unverified[0].notes[0] if digest.unverified[0].notes else ""
        if why:
            out.append(dim(f"  motivo típico: {why}"))
            out.append(dim("  ver detalle:  starradar explain <repo>"))
            out.append("")

    q = st.get("quota")
    if q:
        out.append(dim(f"  cuota: {q}"))
    out.append("")
    return "\n".join(out)


def _render_score_row(s: Score, i: int, muted: bool = False) -> str:
    style, glyph = VERDICT_STYLE.get(s.verdict, (dim, "·"))
    paint = dim if muted else style
    name = bold(s.repo) if not muted else s.repo
    wr = "n/d" if s.watch_ratio is None else f"{s.watch_ratio:.4f}"
    return (
        f"  {dim(f'{i:>2}')} {glyph} {name:<38} "
        f"{paint(f'{s.total:>5.1f}')} "
        f"{_bar(s.total, 8)} "
        f"{s.stars:>6,}★ {s.stars_per_day:>7.1f}★/d "
        f"{dim(f'w/★={wr} {s.age_days:.0f}d')}"
    )


def render_explain(s: Score) -> str:
    return "\n" + s.explain() + "\n"


# -- comandos ------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.window:
        settings.window_days = args.window
    if args.floor is not None:
        settings.star_floor = args.floor
    if args.offline:
        settings.live = False
    if args.topics:
        settings.topics = tuple(args.topics)

    digest = run_scan(
        settings,
        topics=tuple(args.topics) if args.topics else None,
        window_days=args.window,
        star_floor=args.floor,
        languages=tuple(args.lang or ()),
    )
    if args.json:
        print(digest.to_json())
    else:
        print(render_digest(digest, limit=args.limit))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Explica un repo con datos reales. Sin token, se lee de la BD."""
    name = args.repo
    if "/" not in name:
        print("usa el formato owner/repo", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    repos = store.get_repos([name])
    repo = repos.get(name)

    # Si no está guardado y hay token, se pregunta a GitHub directamente.
    if repo is None and settings.live and settings.token:
        try:
            detail = GitHubClient(settings).repo_detail(name)
            if detail is not None:
                repo = detail
                store.upsert_repos([detail])
        except Exception as exc:  # noqa: BLE001 - es una ruta de ayuda
            print(f"no se pudo consultar GitHub: {exc}", file=sys.stderr)

    if repo is None:
        print(
            f"{name} no está en la base local. Ejecute `starradar scan` primero,\n"
            f"o exporte STARRADAR_TOKEN para consultarlo directamente.",
            file=sys.stderr,
        )
        return 1

    history = store.history([name]).get(name, [])
    from .scoring import score

    print(render_explain(score(repo, history)))
    return 0


def cmd_moves(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    moves = top_changes(store, since_hours=args.hours, limit=args.limit)
    if args.json:
        print(json.dumps([{"repo": r, "delta": d} for r, d in moves], indent=2))
        return 0
    if not moves:
        print(
            dim(
                "\n  Sin movimientos todavía: hacen falta snapshots de al menos dos días.\n"
                "  Ejecuta `starradar scan` a diario (o activa el cron) y vuelve mañana.\n"
            )
        )
        return 0
    print("\n" + bold("  MAYOR CRECIMIENTO") + dim(f"  últimas {args.hours}h"))
    for i, (name, delta) in enumerate(moves, 1):
        print(f"  {dim(f'{i:>2}')}  {name:<44} {green(f'+{delta:,}★')}")
    print("")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    return serve(host=args.host, port=args.port, settings=Settings.from_env())


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    return mcp_main()


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Muestra la distribución de w/★ para recalibrar el suelo si hace falta."""
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    repos = store.all_repos()
    from .scoring import summarize_watch_ratios

    stats = summarize_watch_ratios(repos)
    print()
    print(bold("  Distribución watchers/estrellas") + f"  ({int(stats['n'])} repos)")
    print(f"    mediana {stats['median']:.4f}   mín {stats['min']:.4f}   máx {stats['max']:.4f}")
    from . import config as cfg

    print(f"    suelo actual {cfg.AUTHENTICITY_FLOOR}   fuerte {cfg.AUTHENTICITY_STRONG}")
    below = sum(
        1 for r in repos if r.watch_ratio is not None and r.watch_ratio < cfg.AUTHENTICITY_FLOOR
    )
    print(f"    {below}/{len(repos)} repos por debajo del suelo")
    print()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    d = store.last_digest()
    print()
    print(bold("  starradar") + dim(f"  ·  {settings.home}"))
    print(f"    repos guardados:  {len(store.all_repos(limit=10000))}")
    print(f"    snapshots:        {store.snapshot_count()}  ({store.tracked_days()} días)")
    if d:
        print(f"    último digest:    {d.get('at')}  ({d.get('stats', {}).get('n_verified', 0)} verificados)")
    else:
        print(dim("    sin digest todavía: ejecuta `starradar scan`"))
    print(f"    token:            {'configurado' if settings.token else 'FALTA'}")
    print()
    return 0


# -- main ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="starradar",
        description=(
            "Radares de repos de GitHub nuevos con estrellas en crecimiento real. "
            "Filtra granjas de estrellas usando watchers/estrellas y mide velocidad "
            "con snapshots históricos."
        ),
    )
    p.add_argument("--version", action="version", version=f"starradar {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="descubre, puntúa y guarda un ciclo completo")
    s.add_argument("--window", type=int, help="ventana de descubrimiento en días")
    s.add_argument("--floor", type=int, help="suelo de estrellas para la búsqueda")
    s.add_argument("--topics", nargs="*", help="topics a consultar (uno por query)")
    s.add_argument("--lang", nargs="*", help="filtra por lenguaje")
    s.add_argument("--limit", type=int, default=15, help="cuántos verificados mostrar")
    s.add_argument("--offline", action="store_true", help="no usa red; recalcula lo guardado")
    s.add_argument("--json", action="store_true", help="salida JSON")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("explain", help="explica por qué un repo puntúa así")
    e.add_argument("repo", help="owner/repo")
    e.set_defaults(func=cmd_explain)

    m = sub.add_parser("moves", help="qué ha subido más desde el último snapshot")
    m.add_argument("--hours", type=int, default=24, help="ventana en horas")
    m.add_argument("--limit", type=int, default=10)
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_moves)

    d = sub.add_parser("digest", help=" reimprime el último digest sin tocar la red")
    d.add_argument("--limit", type=int, default=15)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=_cmd_digest)

    st = sub.add_parser("status", help="estado local (BD, snapshots, token)")
    st.set_defaults(func=cmd_status)

    c = sub.add_parser("calibrate", help="distribución de w/★ para recalibrar")
    c.set_defaults(func=cmd_calibrate)

    sv = sub.add_parser("serve", help="dashboard web de sólo lectura")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(func=cmd_serve)

    mp = sub.add_parser("mcp", help="servidor MCP por stdio (para Hermes)")
    mp.set_defaults(func=cmd_mcp)

    sig = sub.add_parser("signals", help="emite señales Atlas desde el último digest")
    sig.add_argument("--out", help="directorio atlas/ donde escribir YYYY-MM-DD.json")
    sig.add_argument("--limit", type=int, default=8)
    sig.add_argument("--json", action="store_true", help="imprime las señales y sale")
    sig.set_defaults(func=cmd_signals)

    return p


def cmd_signals(args: argparse.Namespace) -> int:
    """Convierte el digest en señales Atlas.

    Sólo repos verificados: una señal con estrellas no demostradas es peor que
    no tener señal, porque Atlas la convertiría en tesis.
    """
    from .atlas import digest_to_signals, write_atlas

    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    raw = store.last_digest()
    if raw is None:
        print("no hay digest guardado; ejecuta `starradar scan`", file=sys.stderr)
        return 1

    digest = _rebuild_digest(raw)
    signals = digest_to_signals(digest, limit=args.limit)

    if args.json or not args.out:
        print(json.dumps(signals, indent=2, ensure_ascii=False))
        if not signals:
            print(
                dim("\n  0 señales: ningún repo verificado. Atlas se queda sin nada,\n"
                    "  que es lo correcto antes que emitir señales no verificadas.\n"),
                file=sys.stderr,
            )
        return 0

    path = write_atlas(digest, args.out)
    print(f"{len(signals)} señales escritas en {path}")
    return 0


def _rebuild_digest(raw: dict[str, Any]) -> Digest:
    """Reconstruye un Digest desde lo serializado en la BD."""
    from .models import ScoreComponent, parse_dt

    def rebuild(d: dict[str, Any]) -> Score:
        c = d["components"]

        def comp(key: str, name: str) -> ScoreComponent:
            return ScoreComponent(name, c[key]["value"], c[key]["weight"], c[key].get("detail", ""))

        return Score(
            repo=d["repo"],
            total=d["total"],
            velocity=comp("velocity", "velocidad"),
            authenticity=comp("authenticity", "autenticidad"),
            momentum=comp("momentum", "momento"),
            verified=d["verified"],
            stars=d["stars"],
            age_days=d["age_days"],
            stars_per_day=d["stars_per_day"],
            watch_ratio=d["watch_ratio"],
            reasons=tuple(d.get("reasons", [])),
            notes=tuple(d.get("notes", [])),
        )

    return Digest(
        at=parse_dt(raw["at"]) or __import__("datetime").datetime.now(),
        verified=[rebuild(s) for s in raw.get("verified", [])],
        unverified=[rebuild(s) for s in raw.get("unverified", [])],
        stats=raw.get("stats", {}),
    )


def _cmd_digest(args: argparse.Namespace) -> int:
    """Reimprime el último digest sin tocar la red."""
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    raw = store.last_digest()
    if raw is None:
        print("no hay digest guardado; ejecuta `starradar scan`", file=sys.stderr)
        return 1
    digest = _rebuild_digest(raw)
    if args.json:
        print(digest.to_json())
    else:
        print(render_digest(digest, limit=args.limit))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrumpido", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
