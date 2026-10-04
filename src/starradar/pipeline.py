"""Pipeline: descubrir → enriquecer → guardar → puntuar → digerir.

Es el único sitio que orquesta. Todo lo que hace tiene un motivo medido:

1. **Descubrir** por búsqueda REST, una query por topic (no hay OR).
2. **Enriquecer** con GraphQL en lotes de 50, bisectando si GitHub agota los
   límites de la query, y respaldando por REST lo que quede sin `watchers`.
   `watchers` es la base del score de autenticidad: sin él, el ranking sería
   una lista deneau de estrellas. Ese fallback existe porque perder watchers
   no degrada el dato, lo invierte: un repo sin watchers puntúa como
   "no verificado" aunque sea perfectamente orgánico.
3. **Guardar** repo + snapshot del día. El snapshot de ayer es lo que permite
   medir velocidad mañana.
4. **Puntuar** con el historial completo.
5. **Separar** en las dos capas: verificado y sin verificar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import Settings
from .github import GitHubClient, GitHubError
from .models import Digest, Repo, Snapshot, utcnow
from .scoring import score, split
from .store import Store


def run_scan(
    settings: Settings | None = None,
    store: Store | None = None,
    client: GitHubClient | None = None,
    topics: tuple[str, ...] | None = None,
    window_days: int | None = None,
    star_floor: int | None = None,
    languages: tuple[str, ...] = (),
    now: datetime | None = None,
    verbose: bool = True,
) -> Digest:
    """Ejecuta un ciclo completo y devuelve el digest."""
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    now = now or utcnow()
    store = store or Store(settings.db_path)

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    # --- 1. descubrir ---------------------------------------------------
    if not settings.live:
        # Modo sin red: sólo recalcula con lo ya guardado. Útil para probar
        # el scoring y el digest sin gastar cuota.
        log("modo offline: se recalcula con los datos guardados")
        repos = store.all_repos()
        history = store.history([r.full_name for r in repos])
    else:
        own_client = client is None
        client = client or GitHubClient(settings)
        try:
            log(
                f"buscando repos creados en los últimos "
                f"{window_days or settings.window_days}d "
                f"(suelo {settings.star_floor if star_floor is None else star_floor}★)…"
            )
            found = client.discover(
                topics=topics, window_days=window_days, star_floor=star_floor,
                languages=languages,
            )
            log(f"  {len(found)} repos únicos descubiertos")
            if not found:
                log("  (sin resultados: se recalcula con lo guardado)")

            # --- 2. enriquecer -----------------------------------------
            names = [r.full_name for r in found]
            enriched = client.enrich(names) if names else {}
            # Respaldo: lo que GraphQL no devolvió se pide por REST. Cuesta
            # 1 punto de cuota por repo, así que sólo se usa para los que
            # quedaron sin watchers —que es exactamente donde el score de
            # autenticidad no puede calcularse—. Sin watchers, un repo cae
            # en "sin verificar" por falta de dato, no por sospecha.
            missing = [n for n in names if n not in enriched]
            if missing:
                log(f"  {len(missing)} sin enriquecer: respaldo por REST…")
                for name in missing:
                    detail = client.repo_detail(name)
                    if detail is not None and detail.watchers:
                        enriched[name] = detail
            merged: list[Repo] = []
            for r in found:
                e = enriched.get(r.full_name)
                if e is not None:
                    merged.append(e)
                else:
                    merged.append(r)  # sin datos extra: se puntúa igual
            repos = merged
            log(f"  {len(enriched)}/{len(names)} enriquecidos con watchers+topics")
        except GitHubError as exc:
            log(f"  ! GitHub falló: {exc}")
            repos = store.all_repos()
            history = store.history([r.full_name for r in repos])
        finally:
            if own_client and client is not None:
                log(
                    f"  cuota: {client.rate.summary()} · "
                    f"rest {client.stats['rest']} (304: {client.stats['rest_304']}) · "
                    f"búsquedas {client.stats['search']} · graphql {client.stats['graphql']}"
                )

    if not repos:
        return Digest(at=now, verified=[], unverified=[], stats={"n_discovered": 0})

    # --- 3. guardar ------------------------------------------------------
    if settings.live:
        store.upsert_repos(repos, now=now)
        store.add_snapshots(
            [Snapshot(r.full_name, now, r.stars, r.forks, r.watchers) for r in repos]
        )

    # --- 4. historial y scoring -----------------------------------------
    history = store.history([r.full_name for r in repos])
    scored = [score(r, history.get(r.full_name, []), now) for r in repos]
    verified, unverified = split(scored, scored)

    # --- 5. digest -------------------------------------------------------
    stats: dict[str, Any] = {
        "n_discovered": len(repos),
        "n_verified": len(verified),
        "n_unverified": len(unverified),
        "window_days": window_days or settings.window_days,
        "star_floor": settings.star_floor if star_floor is None else star_floor,
        "topics": list(topics or settings.topics),
        "velocity_source": (
            "medida (snapshots)" if store.tracked_days() >= 2 else "estimada (sin histórico)"
        ),
        "tracked_days": store.tracked_days(),
    }
    if client is not None and not settings.live:
        stats["quota"] = client.rate.summary()
    elif client is not None:
        stats["quota"] = client.rate.summary()
        stats["requests"] = dict(client.stats)

    digest = Digest(at=now, verified=verified, unverified=unverified, stats=stats)
    store.save_digest(digest, window=f"{window_days or settings.window_days}d")
    return digest


def top_changes(
    store: Store, since_hours: int = 24, limit: int = 10
) -> list[tuple[str, int]]:
    """Repos que más han crecido en las últimas horas. Vacío si no hay historial.

    Es la pregunta que responde el digest diario: "qué ha subido desde ayer".
    """
    from datetime import timedelta

    since = utcnow() - timedelta(hours=since_hours)
    data = store.history(
        [r.full_name for r in store.all_repos()], days=max(2, since_hours // 24 + 2)
    )
    moves: list[tuple[str, int]] = []
    for name, snaps in data.items():
        recent = [s for s in snaps if s.at >= since]
        if len(recent) >= 2:
            delta = recent[-1].stars - recent[0].stars
            if delta > 0:
                moves.append((name, delta))
    return sorted(moves, key=lambda x: x[1], reverse=True)[:limit]
