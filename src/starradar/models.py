"""Modelos de dominio.

Son dataclasses planas a propósito: se serializan tal cual a SQLite, a JSON
(para el MCP server y el dashboard) y a las señales de Atlas, sin capas de
traducción intermedias.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_dt(value: str | datetime | None) -> datetime | None:
    """Parsea los timestamps de GitHub, que llegan en formatos mixtos."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def age_days(created: datetime | None, now: datetime | None = None) -> float:
    """Días desde la creación. `inf` si no se sabe, para que no puntúe alto."""
    if created is None:
        return float("inf")
    now = now or utcnow()
    return max(0.0, (now - created).total_seconds() / 86400.0)


@dataclass(slots=True)
class Repo:
    """Un repositorio, tal como lo devuelve la API (normalizado)."""

    full_name: str
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    open_issues: int = 0
    description: str | None = None
    language: str | None = None
    license: str | None = None
    homepage: str | None = None
    topics: tuple[str, ...] = ()
    created_at: datetime | None = None
    pushed_at: datetime | None = None
    is_fork: bool = False
    is_archived: bool = False
    owner_type: str | None = None

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def age_days(self) -> float:
        return age_days(self.created_at)

    @property
    def pushed_days_ago(self) -> float:
        return age_days(self.pushed_at)

    @property
    def watch_ratio(self) -> float | None:
        """watchers/estrellas. El discriminador de autenticidad.

        ``None`` si no hay estrellas (división por cero) para que el consumidor
        tenga que decidir explícitamente, en vez de heredar un 0.0 accidental.
        """
        if self.stars <= 0:
            return None
        return self.watchers / self.stars

    @property
    def fork_ratio(self) -> float | None:
        """forks/estrellas. Descriptivo; NO se usa para juzgar autenticidad.

        Se mantiene porque aparece en los informes, pero medido sobre repos
        reales no discrimina granjas de crecimiento orgánico.
        """
        if self.stars <= 0:
            return None
        return self.forks / self.stars

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = iso(self.created_at)
        data["pushed_at"] = iso(self.pushed_at)
        data["topics"] = list(self.topics)
        data["url"] = self.url
        return data


@dataclass(slots=True)
class Snapshot:
    """Una medición de un repo en un instante. La base de la velocidad."""

    repo: str
    at: datetime
    stars: int
    forks: int = 0
    watchers: int = 0

    @property
    def key(self) -> str:
        """Clave de upsert: un snapshot por repo por día natural."""
        return f"{self.repo}@{self.at.date().isoformat()}"


@dataclass(slots=True)
class ScoreComponent:
    """Una dimensión del score, con su peso y su valor bruto."""

    name: str
    value: float
    weight: float
    detail: str = ""

    @property
    def weighted(self) -> float:
        return self.value * self.weight


@dataclass(slots=True)
class Score:
    """Score compuesto + veredicto de autenticidad. Siempre explicable.

    `explain()` es la razón de ser del módulo: un score que no se puede
    explicar no se puede depurar, y sin depurar no se puede confiar.
    """

    repo: str
    total: float
    velocity: ScoreComponent
    authenticity: ScoreComponent
    momentum: ScoreComponent
    verified: bool
    stars: int
    age_days: float
    stars_per_day: float
    watch_ratio: float | None
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        if not self.verified:
            return "sin verificar"
        if self.total >= 70:
            return "destacado"
        if self.total >= 45:
            return "sólido"
        return "temprano"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "total": round(self.total, 1),
            "verdict": self.verdict,
            "verified": self.verified,
            "stars": self.stars,
            "age_days": round(self.age_days, 1),
            "stars_per_day": round(self.stars_per_day, 2),
            "watch_ratio": None if self.watch_ratio is None else round(self.watch_ratio, 4),
            "components": {
                "velocity": {
                    "value": round(self.velocity.value, 1),
                    "weight": self.velocity.weight,
                    "detail": self.velocity.detail,
                },
                "authenticity": {
                    "value": round(self.authenticity.value, 1),
                    "weight": self.authenticity.weight,
                    "detail": self.authenticity.detail,
                },
                "momentum": {
                    "value": round(self.momentum.value, 1),
                    "weight": self.momentum.weight,
                    "detail": self.momentum.detail,
                },
            },
            "reasons": list(self.reasons),
            "notes": list(self.notes),
        }

    def explain(self) -> str:
        """Explicación legible, en español y sin jerga."""
        lines = [
            f"{self.repo}  →  {self.total:.1f}/100  [{self.verdict}]",
            f"  {self.stars:,}★ · {self.age_days:.0f} días · {self.stars_per_day:.1f}★/día"
            f" · w/★ {self._ratio_str()}",
            "",
        ]
        for comp in (self.velocity, self.authenticity, self.momentum):
            lines.append(
                f"  {comp.name:<13} {comp.value:5.1f}/100 × {comp.weight:.2f}"
                f" = {comp.weighted:5.1f}   {comp.detail}"
            )
        if self.reasons:
            lines.append("")
            lines.append("  A favor:")
            lines.extend(f"    + {r}" for r in self.reasons)
        if self.notes:
            lines.append("  En contra / avisos:")
            lines.extend(f"    - {n}" for n in self.notes)
        return "\n".join(lines)

    def _ratio_str(self) -> str:
        return "n/d" if self.watch_ratio is None else f"{self.watch_ratio:.4f}"


@dataclass(slots=True)
class Digest:
    """El resultado de una ejecución: ranking + los datos para reconstruirlo."""

    at: datetime
    verified: list[Score] = field(default_factory=list)
    unverified: list[Score] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": iso(self.at),
            "verified": [s.to_dict() for s in self.verified],
            "unverified": [s.to_dict() for s in self.unverified],
            "stats": self.stats,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
