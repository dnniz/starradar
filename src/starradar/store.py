"""Almacén SQLite: repos, snapshots históricos, digests y señales.

Por qué SQLite y no un fichero JSON: el radar necesita (a) deduplicar por
``owner/repo``, (b) guardar un snapshot por repo y día para derivar velocidad,
y (c) consultar "top cambios desde la última vez" sin releer todo. Eso es
justo lo que un `INSERT OR REPLACE` con índice resuelve y un JSON no.

El esquema es deliberadamente pequeño: cuatro tablas y un índice. Si alguna vez
hace falta más, es señal de que falta una decisión, no de que falte una tabla.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Digest, Repo, Snapshot, iso, parse_dt, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    full_name   TEXT PRIMARY KEY,
    stars       INTEGER NOT NULL DEFAULT 0,
    forks       INTEGER NOT NULL DEFAULT 0,
    watchers    INTEGER NOT NULL DEFAULT 0,
    open_issues INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    language    TEXT,
    license     TEXT,
    homepage    TEXT,
    topics      TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT,
    pushed_at   TEXT,
    is_fork     INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    owner_type  TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    repo     TEXT NOT NULL,
    at       TEXT NOT NULL,
    stars    INTEGER NOT NULL,
    forks    INTEGER NOT NULL DEFAULT 0,
    watchers INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, at)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_at ON snapshots(at);
CREATE INDEX IF NOT EXISTS idx_repos_last_seen ON repos(last_seen);

CREATE TABLE IF NOT EXISTS digests (
    at         TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    n_verified INTEGER NOT NULL DEFAULT 0,
    n_all      INTEGER NOT NULL DEFAULT 0,
    window     TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload    TEXT NOT NULL,
    exported   INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    """Acceso a la base. Todos los métodos abren y cierran su conexión.

    Se usa una conexión por operación en vez de una persistente: el proceso es
    de vida corta (CLI, cron) y así no hay estado que se quede colgando entre
    ejecuciones ni bloqueos si dos procesos escriben a la vez (el modo WAL y el
    `busy_timeout` cubren eso).
    """

    def __init__(self, path: Path | str) -> None:
        # Acepta str a propósito: en CLI y tests es más cómodo pasar
        # "/tmp/x.db" que Path("/tmp/x.db"), y normalizar aquí evita el
        # TypeError diferido que de otro modo aparece en la línea siguiente.
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # -- repos ------------------------------------------------------------

    def upsert_repos(self, repos: list[Repo], now: datetime | None = None) -> int:
        """Inserta o actualiza repos. Devuelve cuántos han cambiado de verdad.

        "Cambiado de verdad" = estrellas, forks, watchers o push distintos. Es
        lo que evita que cada ejecución escriba 200 filas idénticas.
        """
        now = now or utcnow()
        stamp = iso(now)
        changed = 0
        with self._conn() as conn:
            for r in repos:
                row = conn.execute(
                    "SELECT stars, forks, watchers, pushed_at, last_seen FROM repos"
                    " WHERE full_name = ?",
                    (r.full_name,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO repos (full_name, stars, forks, watchers, open_issues,"
                        " description, language, license, homepage, topics, created_at,"
                        " pushed_at, is_fork, is_archived, owner_type, first_seen, last_seen)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            r.full_name, r.stars, r.forks, r.watchers, r.open_issues,
                            r.description, r.language, r.license, r.homepage,
                            json.dumps(list(r.topics)), iso(r.created_at), iso(r.pushed_at),
                            int(r.is_fork), int(r.is_archived), r.owner_type, stamp, stamp,
                        ),
                    )
                    changed += 1
                    continue

                if (
                    row["stars"] != r.stars
                    or row["forks"] != r.forks
                    or row["watchers"] != r.watchers
                    or row["pushed_at"] != iso(r.pushed_at)
                ):
                    conn.execute(
                        "UPDATE repos SET stars=?, forks=?, watchers=?, open_issues=?,"
                        " description=?, language=?, license=?, homepage=?, topics=?,"
                        " created_at=?, pushed_at=?, is_fork=?, is_archived=?, owner_type=?,"
                        " last_seen=? WHERE full_name=?",
                        (
                            r.stars, r.forks, r.watchers, r.open_issues, r.description,
                            r.language, r.license, r.homepage, json.dumps(list(r.topics)),
                            iso(r.created_at), iso(r.pushed_at), int(r.is_fork),
                            int(r.is_archived), r.owner_type, stamp, r.full_name,
                        ),
                    )
                    changed += 1
                else:
                    conn.execute(
                        "UPDATE repos SET last_seen=? WHERE full_name=?",
                        (stamp, r.full_name),
                    )
        return changed

    def get_repos(self, names: list[str]) -> dict[str, Repo]:
        if not names:
            return {}
        out: dict[str, Repo] = {}
        with self._conn() as conn:
            # Chunked para no pasarse del límite de variables de SQLite.
            for i in range(0, len(names), 400):
                chunk = names[i : i + 400]
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT * FROM repos WHERE full_name IN ({marks})", chunk
                ):
                    out[row["full_name"]] = _row_to_repo(row)
        return out

    def all_repos(self, limit: int = 5000) -> list[Repo]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM repos ORDER BY stars DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_repo(r) for r in rows]

    # -- snapshots --------------------------------------------------------

    def add_snapshot(self, snap: Snapshot) -> None:
        """Guarda la medición de hoy. PRIMARY KEY (repo, at) lo deduplica."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (repo, at, stars, forks, watchers)"
                " VALUES (?,?,?,?,?)",
                (snap.repo, iso(snap.at), snap.stars, snap.forks, snap.watchers),
            )

    def add_snapshots(self, snaps: list[Snapshot]) -> None:
        if not snaps:
            return
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO snapshots (repo, at, stars, forks, watchers)"
                " VALUES (?,?,?,?,?)",
                [
                    (s.repo, iso(s.at), s.stars, s.forks, s.watchers)
                    for s in snaps
                ],
            )

    def history(self, names: list[str], days: int = 60) -> dict[str, list[Snapshot]]:
        """Historial de los repos indicados, para derivar velocidad."""
        if not names:
            return {}
        since = iso(utcnow() - timedelta(days=days))
        out: dict[str, list[Snapshot]] = {}
        with self._conn() as conn:
            for i in range(0, len(names), 400):
                chunk = names[i : i + 400]
                marks = ",".join("?" * len(chunk))
                params = [*chunk, since]
                for row in conn.execute(
                    f"SELECT repo, at, stars, forks, watchers FROM snapshots"
                    f" WHERE repo IN ({marks}) AND at >= ? ORDER BY repo, at",
                    params,
                ):
                    at = parse_dt(row["at"])
                    if at is None:
                        continue
                    out.setdefault(row["repo"], []).append(
                        Snapshot(
                            repo=row["repo"],
                            at=at,
                            stars=int(row["stars"]),
                            forks=int(row["forks"]),
                            watchers=int(row["watchers"]),
                        )
                    )
        return out

    def snapshot_count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"])

    def tracked_days(self) -> int:
        """Días distintos con datos. Determina si la velocidad es real o estimada."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT substr(at, 1, 10)) c FROM snapshots"
            ).fetchone()
        return int(row["c"])

    # -- digests ----------------------------------------------------------

    def save_digest(self, digest: Digest, window: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO digests (at, payload, n_verified, n_all, window)"
                " VALUES (?,?,?,?,?)",
                (
                    iso(digest.at),
                    json.dumps(digest.to_dict(), ensure_ascii=False),
                    len(digest.verified),
                    len(digest.verified) + len(digest.unverified),
                    window,
                ),
            )

    def last_digest(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM digests ORDER BY at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    # -- señales ----------------------------------------------------------

    def add_signal(self, sid: str, payload: dict[str, Any], now: datetime | None = None) -> None:
        """Guarda una señal para exportar a Atlas. Idempotente por id."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signals (id, created_at, payload, exported)"
                " VALUES (?,?,?,0)",
                (sid, iso(now or utcnow()), json.dumps(payload, ensure_ascii=False)),
            )

    def pending_signals(self, limit: int = 100) -> list[tuple[str, dict[str, Any]]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM signals WHERE exported = 0 ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["id"], json.loads(r["payload"])) for r in rows]

    def mark_exported(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE signals SET exported = 1 WHERE id = ?", [(i,) for i in ids]
            )


def _row_to_repo(row: sqlite3.Row) -> Repo:
    try:
        topics = tuple(json.loads(row["topics"] or "[]"))
    except json.JSONDecodeError:
        topics = ()
    return Repo(
        full_name=row["full_name"],
        stars=int(row["stars"]),
        forks=int(row["forks"]),
        watchers=int(row["watchers"]),
        open_issues=int(row["open_issues"]),
        description=row["description"],
        language=row["language"],
        license=row["license"],
        homepage=row["homepage"],
        topics=topics,
        created_at=parse_dt(row["created_at"]),
        pushed_at=parse_dt(row["pushed_at"]),
        is_fork=bool(row["is_fork"]),
        is_archived=bool(row["is_archived"]),
        owner_type=row["owner_type"],
    )
