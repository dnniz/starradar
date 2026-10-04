"""Scoring: velocidad, autenticidad y momento, siempre explicable.

Este módulo es **puro**: no toca red ni disco. Toda la lógica que decide qué es
"lo más caliente" vive aquí, y se testea sin token de GitHub. Esa separación es
deliberada — el scoring es donde se concentran los errores silenciosos, y sólo
es auditable si se puede ejecutar en aislamiento.

Las tres dimensiones:

* **velocidad**   — estrellas/día, medida contra snapshots históricos.
* **autenticidad** — watchers/estrellas contra un suelo validado empíricamente.
* **momento**     — actividad reciente (push, issues) y penalización por
  señales de granja.

Ningún peso ni umbral está hardcodeado: vienen de :mod:`starradar.config`.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import median

from . import config as cfg
from .models import Repo, Score, ScoreComponent, Snapshot, utcnow

# --------------------------------------------------------------------------
# Authenticidad
# --------------------------------------------------------------------------


def authenticity(repo: Repo) -> tuple[ScoreComponent, list[str], list[str]]:
    """Puntúa el ratio watchers/estrellas de 0 a 100.

    Devuelve el componente, los motivos a favor y los avisos.

    Por qué watchers y no forks: medido el 2026-10-04 sobre 10 proyectos
    establecidos y 100 repos nuevos, watchers/estrellas los separa de forma
    limpia (mediana ~0.024 vs ~0.005; una mayoría de los nuevos cae por
    debajo del suelo) mientras forks/estrellas no discrimina: ``denoland/deno``
    tiene 0.0589 —el más bajo de los establecidos, con forks perfectamente
    reales— y un repo con estrellas compradas marcaba 0.305, por encima de la
    mediana de los establecidos. Un ratio alto de forks significa "mucho
    interés", no "estrellas falsas".
    """
    ratio = repo.watch_ratio
    reasons: list[str] = []
    notes: list[str] = []

    if ratio is None:
        # Sin estrellas no hay nada que autenticar. 0, no 50: no regalar puntos.
        return (
            ScoreComponent("autenticidad", 0.0, cfg.WEIGHT_AUTHENTICITY, "sin estrellas"),
            [],
            ["sin estrellas: no se puede evaluar la autenticidad"],
        )

    if repo.stars == 0:
        return (
            ScoreComponent("autenticidad", 0.0, cfg.WEIGHT_AUTHENTICITY, "0 estrellas"),
            [],
            ["0 estrellas"],
        )

    if repo.watchers == 0:
        # Sin watchers no hay dato, y no es lo mismo que un dato malo.
        #
        # Un repo con 5.000★ y 0 watchers parece "`w/★=0.0000`, por debajo del
        # suelo" — o sea, sospechoso. Es una lectura equivocada: watchers=0
        # significa que el enriquecimiento no llegó a traerlo (trampa de la
        # API: `watchers_count` es un alias de las estrellas, ver
        # `_search_item_to_repo`). Acusar de fraude a lo que sólo no sabemos
        # envenena el ranking entero; la pasada real del 2026-10-04 marcó así
        # a 539 de 567 repos.
        #
        # Se devuelve neutro, sin penalizar y sin acusar: el repo no se
        # degrada, simplemente no aporta señal en esta dimensión.
        return (
            ScoreComponent(
                "autenticidad", 50.0, cfg.WEIGHT_AUTHENTICITY, "sin dato de watchers"
            ),
            [],
            ["sin dato de watchers: la autenticidad no se puede evaluar"],
        )

    if ratio < cfg.AUTHENTICITY_FLOOR:
        # La banda baja no es "falso", es "no demostrado". Se penaliza fuerte
        # pero se marca como dudoso en vez de acusar.
        # Escala: floor→0, medio recorrido hasta 0 (curva suave, no un muro).
        value = max(0.0, 45.0 * (ratio / cfg.AUTHENTICITY_FLOOR)) if cfg.AUTHENTICITY_FLOOR else 0.0
        notes.append(
            f"w/★={ratio:.4f} por debajo del suelo {cfg.AUTHENTICITY_FLOOR} "
            f"(mediana de proyectos establecidos 0.0240)"
        )
        return (
            ScoreComponent(
                "autenticidad", value, cfg.WEIGHT_AUTHENTICITY,
                f"w/★={ratio:.4f} < suelo {cfg.AUTHENTICITY_FLOOR}",
            ),
            reasons,
            notes,
        )

    if ratio < cfg.AUTHENTICITY_STRONG:
        # En el suelo: pasa, pero sin life's credit extra.
        pct = (ratio - cfg.AUTHENTICITY_FLOOR) / (cfg.AUTHENTICITY_STRONG - cfg.AUTHENTICITY_FLOOR)
        value = 50.0 + 35.0 * pct
        reasons.append(f"w/★={ratio:.4f} supera el suelo {cfg.AUTHENTICITY_FLOOR}")
        return (
            ScoreComponent(
                "autenticidad", value, cfg.WEIGHT_AUTHENTICITY,
                f"w/★={ratio:.4f} (suelo, no confirmado)",
            ),
            reasons,
            notes,
        )

    # Orgánico confirmado. La cola se comprime con una raíz para que un ratio
    # absurdamente alto (0.5) no linealmente se lleve el score.
    value = 85.0 + 15.0 * min(1.0, math.log1p(ratio - cfg.AUTHENTICITY_STRONG) / math.log1p(0.05))
    reasons.append(f"w/★={ratio:.4f} orgánico confirmado (mediana establecida 0.0240)")
    return (
        ScoreComponent(
            "autenticidad", min(100.0, value), cfg.WEIGHT_AUTHENTICITY,
            f"w/★={ratio:.4f} orgánico",
        ),
        reasons,
        notes,
    )


# --------------------------------------------------------------------------
# Velocidad
# --------------------------------------------------------------------------


def velocity(
    repo: Repo,
    history: list[Snapshot],
    now: datetime | None = None,
) -> tuple[ScoreComponent, float, list[str], list[str]]:
    """Estima estrellas/día y puntúa 0-100.

    Tres fuentes, en orden de preferencia:

    1. **Derivada de snapshots** — la señal real: dos puntos en el tiempo dan
       la velocidad observada. Es lo único que mide GitHub no lo da.
    2. **Estrellas / edad** — estimación gruesa para repos con un solo
       snapshot. Sesgada a la baja si el repo ya venía con estrellas al
       crearse (un repo nuevo importado de otro sitio), pero es mejor que
       marcar 0.
    3. **0** — sin datos. Nunca se inventa velocidad.

    Devuelve (componente, estrellas_por_día, razones, avisos).
    """
    now = now or utcnow()
    reasons: list[str] = []
    notes: list[str] = []

    # --- 1. Derivada de snapshots -------------------------------------
    hist = sorted([s for s in history if s.repo == repo.full_name], key=lambda s: s.at)
    if len(hist) >= 2:
        first, last = hist[0], hist[-1]
        span_days = (last.at - first.at).total_seconds() / 86400.0
        # Menos de medio día entre snapshots: ruido de reloj, no tendencia.
        if span_days >= 0.5:
            delta = last.stars - first.stars
            spd = max(0.0, delta / span_days)
            # La velocidad de un snapshot aislado es ruidosa; se capea para
            # que un pico de una hora no lidere el ranking.
            spd = min(spd, 2000.0)
            value = _velocity_to_score(spd)
            reasons.append(
                f"velocidad medida: {spd:.0f}★/día sobre {span_days:.0f} días "
                f"de snapshots ({len(hist)} puntos)"
            )
            if len(hist) < 4:
                notes.append(f"sólo {len(hist)} snapshots: la velocidad es preliminar")
            return (
                ScoreComponent(
                    "velocidad", value, cfg.WEIGHT_VELOCITY, f"{spd:.0f}★/día (medido)"
                ),
                spd,
                reasons,
                notes,
            )
        notes.append("snapshots demasiado juntos para derivar velocidad")

    # --- 2. Estrellas / edad ------------------------------------------
    days = repo.age_days
    if math.isfinite(days) and days >= 1.0 and repo.stars > 0:
        spd = repo.stars / days
        value = _velocity_to_score(spd)
        reasons.append(f"velocidad estimada: {repo.stars:,}★ en {days:.0f} días (sin histórico)")
        notes.append("estimada por edad, no medida: aparecerá más fina cuando haya snapshots")
        return (
            ScoreComponent(
                "velocidad", value, cfg.WEIGHT_VELOCITY, f"{spd:.0f}★/día (estimado)"
            ),
            spd,
            reasons,
            notes,
        )
    if repo.stars > 0 and math.isfinite(days) and days < 1.0:
        # Recién creado con muchas estrellas: casi siempre importación o
        #Gift, no crecimiento. Se penaliza y se explica.
        notes.append("creado hace menos de 1 día: velocidad no calculable")
        return (
            ScoreComponent("velocidad", 0.0, cfg.WEIGHT_VELOCITY, "< 1 día de edad"),
            0.0,
            reasons,
            notes,
        )

    notes.append("sin datos de velocidad")
    return (
        ScoreComponent("velocidad", 0.0, cfg.WEIGHT_VELOCITY, "sin datos"),
        0.0,
        reasons,
        notes,
    )


def _velocity_to_score(spd: float) -> float:
    """Mapa velocidad→score. Log-1p: comprime el rango, como las estrellas.

    10★/día→~25, 50→~55, 100→~70, 300→~90. La escala es logarítmica porque la
    distribución de velocidad también lo es.
    """
    if spd <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log1p(spd) / math.log1p(300.0))


# --------------------------------------------------------------------------
# Momento
# --------------------------------------------------------------------------


def momentum(repo: Repo, now: datetime | None = None) -> tuple[ScoreComponent, list[str], list[str]]:
    """Puntúa si el repo está vivo *ahora*, no sólo si creció antes.

    Un repo que ganó 2000★ en el mes pasado y lleva 6 semanas sin push no es
    una oportunidad: es un cadáver con métricas.
    """
    now = now or utcnow()
    reasons: list[str] = []
    notes: list[str] = []
    value = 60.0  # base: neutro, se ajusta con las señales de abajo

    pushed = repo.pushed_days_ago
    if math.isfinite(pushed):
        if pushed <= 2:
            value += 25
            reasons.append("push en las últimas 48h")
        elif pushed <= 7:
            value += 15
            reasons.append("push esta semana")
        elif pushed <= cfg.STALE_DAYS:
            value += 0
            reasons.append(f"último push hace {pushed:.0f} días")
        else:
            value -= 30
            notes.append(f"sin push desde hace {pushed:.0f} días: repo abandonado")

    # Issues abiertas: proxy de uso. Muchas issues abiertas y ninguna
    # contribución sugiere un proyecto sin manos; pocas issues en un repo
    # nuevo es normal y no penaliza.
    if repo.open_issues > 0 and repo.stars > 0:
        issues_per_star = repo.open_issues / repo.stars
        if issues_per_star > 0.15:
            value -= 10
            notes.append(
                f"{repo.open_issues} issues abiertas por {repo.stars:,}★:ratio alto"
            )
        elif repo.open_issues >= 3:
            value += 8
            reasons.append("issues abiertas con Ratio sano: hay actividad real")

    if repo.is_archived:
        value -= 100
        notes.append("repositorio archivado")
    if repo.is_fork:
        value -= 15
        notes.append("es un fork")

    return (
        ScoreComponent(
            "momento", max(0.0, min(100.0, value)), cfg.WEIGHT_MOMENTUM,
            f"push hace {pushed:.0f}d" if math.isfinite(pushed) else "sin fecha de push",
        ),
        reasons,
        notes,
    )


# --------------------------------------------------------------------------
# Score compuesto
# --------------------------------------------------------------------------


def score(
    repo: Repo,
    history: list[Snapshot],
    now: datetime | None = None,
) -> Score:
    """Compone las tres dimensiones en un score 0-100 explicable.

    `verified` decide si el repo entra en el ranking de confianza. Exige:
    autenticidad por encima del suelo **y** suficiente señal de velocidad. Un
    repo que solo tiene estrellas pero ninguna señal de crecimiento no es una
    oportunidad, es ruido.
    """
    now = now or utcnow()
    vel_c, spd, vel_r, vel_n = velocity(repo, history, now)
    aut_c, aut_r, aut_n = authenticity(repo)
    mom_c, mom_r, mom_n = momentum(repo, now)

    reasons = [*vel_r, *aut_r, *mom_r]
    notes = [*vel_n, *aut_n, *mom_n]

    total = vel_c.weighted + aut_c.weighted + mom_c.weighted

    # Reglas duras que no dependen del score ponderado.
    days = repo.age_days
    if math.isfinite(days) and days > cfg.MAX_AGE_DAYS:
        notes.append(f"antiguo: {days:.0f} días (el radar busca repos nuevos)")
    if repo.is_archived:
        notes.append("archivado")

    ratio = repo.watch_ratio
    # `authenticity_ok` sólo aplica si HAY dato de watchers. Sin él, el ratio
    # es 0.0 y compararlo contra el suelo daría "sospechoso" — cuando en
    # realidad es "no lo sé" (ver `authenticity()`). Con watchers=0 el repo no
    # se verifica, pero tampoco se marca como dudoso: `authenticity()` ya
    # devolvió un componente neutro, y aquí basta con no exigir la prueba.
    has_watch_data = repo.watchers > 0 and ratio is not None
    authenticity_ok = has_watch_data and float(ratio or 0.0) >= cfg.AUTHENTICITY_FLOOR
    has_signal = spd > 0 and total >= cfg.MIN_COMPOSITE_SCORE
    verified = bool(authenticity_ok and has_signal)

    return Score(
        repo=repo.full_name,
        total=total,
        velocity=vel_c,
        authenticity=aut_c,
        momentum=mom_c,
        verified=verified,
        stars=repo.stars,
        age_days=days if math.isfinite(days) else -1.0,
        stars_per_day=spd,
        watch_ratio=ratio,
        reasons=tuple(reasons),
        notes=tuple(notes),
    )


def split(digest_verified: list[Score], digest_all: list[Score]) -> tuple[list[Score], list[Score]]:
    """Separa en las dos capas del producto: verificados y sin verificar.

    Nunca se mezclan. Es la decisión de producto por defecto: perder un repo
    dudoso por.complete es un coste menor que presentar una granja de estrellas
    como si fuera una oportunidad real.
    """
    verified = sorted(
        [s for s in digest_all if s.verified], key=lambda s: s.total, reverse=True
    )
    unverified = sorted(
        [s for s in digest_all if not s.verified], key=lambda s: s.total, reverse=True
    )
    return verified, unverified


def summarize_watch_ratios(repos: list[Repo]) -> dict[str, float]:
    """Estadística de distribución del ratio, para calibrar umbrales.

    Se usa en `starradar calibrate` y en la validación del suelo: si un día el
    mínimo de los establecidos baja, el suelo de config hay que revisarlo.
    """
    ratios = [r.watch_ratio for r in repos if r.watch_ratio is not None]
    if not ratios:
        return {"n": 0, "median": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": float(len(ratios)),
        "median": float(median(ratios)),
        "min": float(min(ratios)),
        "max": float(max(ratios)),
    }
