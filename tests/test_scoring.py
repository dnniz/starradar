"""Tests del scoring: la parte donde un error es silencioso y caro."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from starradar import config as cfg
from starradar.models import Repo, Snapshot
from starradar.scoring import authenticity, momentum, score, velocity

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_repo(**kw) -> Repo:
    base = dict(
        full_name="acme/tool",
        stars=1000,
        forks=100,
        watchers=30,  # w/★ = 0.03 → orgánico confirmado
        created_at=NOW - timedelta(days=10),
        pushed_at=NOW - timedelta(days=1),
        open_issues=20,
    )
    base.update(kw)
    return Repo(**base)


# --------------------------------------------------------------------------
# Autenticidad
# --------------------------------------------------------------------------


def test_watch_ratio_is_none_without_stars() -> None:
    assert make_repo(stars=0, watchers=0).watch_ratio is None


def test_established_project_is_verified_authentic() -> None:
    """Un ratio en rango de proyecto establecido (0.0240 mediana) es orgánico."""
    s = make_repo(stars=40000, watchers=int(40000 * 0.024)).watch_ratio
    assert s is not None and s == pytest.approx(0.024)
    comp, reasons, notes = authenticity(make_repo(stars=40000, watchers=960))
    assert comp.value >= 85
    assert any("orgánico" in r for r in reasons)
    assert not notes


def test_below_floor_is_penalised_hard() -> None:
    """El caso de la granja: muchas estrellas, casi nadie observando."""
    comp, reasons, notes = authenticity(make_repo(stars=5000, watchers=5))
    assert comp.value < 50
    assert any("suelo" in n for n in notes)


def test_starred_farm_does_not_verify() -> None:
    """Reproduce el caso real: 5540★ y 18 watchers (w/★=0.0032)."""
    s = score(make_repo(stars=5540, watchers=18), history=[], now=NOW)
    assert not s.verified
    assert s.watch_ratio == pytest.approx(0.00325, abs=0.0001)


def test_fork_ratio_is_reported_but_not_used_for_authenticity() -> None:
    """f/★ alto no debe absolver ni condenar: sólo es descriptivo.

    deno tiene f/★=0.0589 (el más bajo de los establecidos) y sus forks son
    reales. Si forks puntuara, deno sería "dudoso" — el heuristic heredado
    fallaría justo en el control de calidad.
    """
    deno = make_repo(full_name="denoland/deno", stars=95000, forks=5600, watchers=2000)
    comp, reasons, _ = authenticity(deno)
    assert comp.value >= 85, "deno debe autenticarse por watchers, no penalizarse por forks"
    # ...pero el ratio de forks sigue disponible como dato.
    assert deno.fork_ratio == pytest.approx(0.0589, abs=0.0001)


def test_suspected_farm_with_high_fork_ratio_not_absolved() -> None:
    """El caso simétrico: f/★ altísimo pero w/★ bajo sigue sin verificar."""
    suspect = make_repo(stars=1000, forks=305, watchers=2)  # f/★=0.305, w/★=0.002
    s = score(suspect, history=[], now=NOW)
    assert not s.verified
    assert suspect.fork_ratio == pytest.approx(0.305)


def test_authenticity_zero_when_no_stars() -> None:
    comp, _, notes = authenticity(make_repo(stars=0, watchers=0))
    assert comp.value == 0.0
    assert notes


# --------------------------------------------------------------------------
# Velocidad
# --------------------------------------------------------------------------


def test_velocity_measured_from_snapshots() -> None:
    """La señal buena: dos snapshots, velocidad observada."""
    r = make_repo(stars=1200)
    hist = [
        Snapshot("acme/tool", NOW - timedelta(days=7), 200),
        Snapshot("acme/tool", NOW, 1200),
    ]
    comp, spd, reasons, _ = velocity(r, hist, NOW)
    assert spd == pytest.approx(1000 / 7, rel=0.01)
    assert any("medida" in d or "medido" in comp.detail for d in [comp.detail])
    assert any("velocidad medida" in x for x in reasons)


def test_velocity_estimated_without_history() -> None:
    r = make_repo(stars=1000, created_at=NOW - timedelta(days=10))
    comp, spd, reasons, notes = velocity(r, [], NOW)
    # 10 días exactos son 10.02 en reloj real; tolerate el desfase de horas.
    assert spd == pytest.approx(100.0, rel=0.05)
    assert any("estimada" in x for x in reasons)
    assert any("estimada" in x for x in notes)


def test_velocity_zero_when_brand_new_with_stars() -> None:
    """Repo creado hace horas con muchas★: importación, no crecimiento."""
    r = make_repo(stars=2000, created_at=NOW - timedelta(hours=3))
    comp, spd, _, notes = velocity(r, [], NOW)
    assert spd == 0.0
    assert comp.value == 0.0
    assert any("1 día" in n for n in notes)


def test_velocity_ignores_snapshots_too_close() -> None:
    r = make_repo(stars=200)
    hist = [
        Snapshot("acme/tool", NOW - timedelta(minutes=10), 100),
        Snapshot("acme/tool", NOW, 200),
    ]
    _, _, _, notes = velocity(r, hist, NOW)
    assert any("juntos" in n or "medio día" in n for n in notes)


def test_velocity_of_zero_delta_is_zero() -> None:
    r = make_repo(stars=500)
    hist = [
        Snapshot("acme/tool", NOW - timedelta(days=5), 500),
        Snapshot("acme/tool", NOW, 500),
    ]
    _, spd, _, _ = velocity(r, hist, NOW)
    assert spd == 0.0


def test_velocity_score_is_monotonic() -> None:
    from starradar.scoring import _velocity_to_score

    values = [_velocity_to_score(s) for s in (1, 10, 50, 100, 300, 1000)]
    assert values == sorted(values)
    assert all(0 <= v <= 100 for v in values)


# --------------------------------------------------------------------------
# Momento
# --------------------------------------------------------------------------


def test_momentum_rewards_recent_push() -> None:
    fresh, _, _ = momentum(make_repo(pushed_at=NOW - timedelta(days=1)), NOW)
    stale, _, stale_notes = momentum(make_repo(pushed_at=NOW - timedelta(days=60)), NOW)
    assert fresh.value > stale.value
    assert any("abandonado" in n for n in stale_notes)


def test_momentum_penalises_archived() -> None:
    comp, _, notes = momentum(make_repo(is_archived=True), NOW)
    assert comp.value < 50
    assert any("archivado" in n for n in notes)


def test_momentum_flags_abandoned() -> None:
    _, _, notes = momentum(make_repo(pushed_at=NOW - timedelta(days=100)), NOW)
    assert any("abandonado" in n for n in notes)


# --------------------------------------------------------------------------
# Compuesto
# --------------------------------------------------------------------------


def test_score_total_is_weighted_sum() -> None:
    r = make_repo()
    s = score(r, history=[], now=NOW)
    expected = (
        s.velocity.value * cfg.WEIGHT_VELOCITY
        + s.authenticity.value * cfg.WEIGHT_AUTHENTICITY
        + s.momentum.value * cfg.WEIGHT_MOMENTUM
    )
    assert s.total == pytest.approx(expected)


def test_verified_requires_authenticity_and_signal() -> None:
    """Orgánico + con velocidad → verificado. Falla cualquiera de las dos."""
    good = score(make_repo(stars=1000, watchers=30), history=[], now=NOW)
    assert good.verified

    # Auténtico pero sin velocidad medible (0★/día y nada de historia).
    no_signal = score(
        make_repo(stars=1000, watchers=30, created_at=NOW - timedelta(hours=2)), history=[], now=NOW
    )
    assert not no_signal.verified


def test_explain_is_human_readable() -> None:
    s = score(make_repo(), history=[], now=NOW)
    text = s.explain()
    assert "acme/tool" in text
    assert "velocidad" in text
    assert "autenticidad" in text
    assert "momento" in text
    assert "/" in text


def test_score_survives_missing_dates() -> None:
    """Un repo sin created_at/pushed_at no debe reventar ni ganar puntos."""
    r = make_repo(created_at=None, pushed_at=None, stars=500, watchers=15)
    s = score(r, history=[], now=NOW)
    assert 0 <= s.total <= 100
    assert s.age_days == -1.0
    assert not math.isnan(s.total)


def test_split_never_mixes_layers() -> None:
    from starradar.scoring import split

    r1 = make_repo(full_name="a/ok", stars=1000, watchers=30)
    r2 = make_repo(full_name="b/farm", stars=5000, watchers=5)
    s1, s2 = score(r1, [], NOW), score(r2, [], NOW)
    verified, unverified = split([s1], [s1, s2])
    assert [s.repo for s in verified] == ["a/ok"]
    assert [s.repo for s in unverified] == ["b/farm"]
    assert not {s.repo for s in verified} & {s.repo for s in unverified}
