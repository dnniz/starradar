"""Tests de la CLI: los cuatro estados que ve una persona.

Un radar que sólo funciona cuando todo va bien es un radar que falla en
producción. Estos tests fijan el texto de cada estado, porque el texto es la
única cosa que lee un agente cuando no hay nadie mirando.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import pytest

from starradar import cli
from starradar.atlas import digest_to_signals, write_atlas
from starradar.config import Settings
from starradar.models import Digest, Score, ScoreComponent

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_score(repo: str, total: float, verified: bool, verdict: str = "sólido") -> Score:
    return Score(
        repo=repo,
        total=total,
        velocity=ScoreComponent("velocidad", total, 0.45, "50★/día"),
        authenticity=ScoreComponent(
            "autenticidad", 80.0 if verified else 10.0, 0.35, "w/★=0.0240"
        ),
        momentum=ScoreComponent("momento", 70.0, 0.15, "push hace 2 días"),
        verified=verified,
        stars=1200,
        age_days=20.0,
        stars_per_day=60.0,
        watch_ratio=0.0240 if verified else 0.0021,
        reasons=("crece rápido",),
        notes=() if verified else ("w/★ por debajo del suelo 0.0075",),
    )


def _settings(tmp_path, monkeypatch):
    """Aísla el entorno para que los tests no toquen la base ni el token real.

    Se borran los tres nombres porque `Settings.from_env` acepta STARRADAR_TOKEN
    y, como alias, GH_TOKEN y GITHUB_TOKEN: borrar sólo el primero deja pasar
    el token del entorno del desarrollador y hace que el test dependa de la
    máquina donde corra.
    """
    monkeypatch.setenv("STARRADAR_HOME", str(tmp_path))
    for var in ("STARRADAR_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return Settings.from_env()


def _args(tmp_path, monkeypatch, argv: list[str]) -> argparse.Namespace:
    """Parsea argv de verdad: si el parser cambia, el test falla aquí."""
    _settings(tmp_path, monkeypatch)
    return cli.build_parser().parse_args(argv)


# -- render -------------------------------------------------------------------


def test_render_empty_digest_explains_what_to_do(capsys) -> None:
    d = Digest(at=NOW, verified=[], unverified=[], stats={"n_discovered": 0})
    out = cli.render_digest(d)
    assert "Sin repos" in out


def test_render_separates_the_two_layers() -> None:
    d = Digest(
        at=NOW,
        verified=[make_score("a/good", 82.0, True, "destacado")],
        unverified=[make_score("b/bad", 30.0, False, "sin verificar")],
        stats={"n_discovered": 2, "n_verified": 1, "n_unverified": 1,
               "velocity_source": "medida (snapshots)", "tracked_days": 5},
    )
    out = cli.render_digest(d)
    assert "VERIFICADOS" in out and "SIN VERIFICAR" in out
    # El repo dudoso no aparece en la sección verificada.
    head, _, tail = out.partition("SIN VERIFICAR")
    assert "a/good" in head and "b/bad" not in head
    assert "b/bad" in tail
    # Y la sección dudosa advierte explícitamente.
    assert "no los tomes como señal" in tail


def test_render_warns_when_velocity_is_estimated() -> None:
    d = Digest(
        at=NOW,
        verified=[make_score("a/x", 60.0, True)],
        unverified=[],
        stats={"velocity_source": "estimada (sin histórico)", "tracked_days": 1},
    )
    assert "estimada" in cli.render_digest(d)


def test_no_ansi_when_not_a_tty() -> None:
    """Canalizado, la salida es texto plano: un agente se come los códigos."""
    d = Digest(at=NOW, verified=[make_score("a/x", 60.0, True)], unverified=[], stats={})
    out = cli.render_digest(d)
    assert "\033[" not in out


# -- comandos con estado vacío -----------------------------------------------


def test_status_command_works_with_empty_db(tmp_path, monkeypatch, capsys) -> None:
    args = _args(tmp_path, monkeypatch, ["status"])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "repos guardados:  0" in out
    assert "FALTA" in out  # sin token: lo dice, en vez de fallar luego
    assert "sin digest todavía" in out


def test_moves_command_handles_no_history(tmp_path, monkeypatch, capsys) -> None:
    args = _args(tmp_path, monkeypatch, ["moves"])
    assert cli.cmd_moves(args) == 0
    out = capsys.readouterr().out
    assert "Sin movimientos" in out
    # Dice qué hacer, no sólo que no hay nada.
    assert "starradar scan" in out


def test_digest_command_without_data_fails_cleanly(tmp_path, monkeypatch, capsys) -> None:
    args = _args(tmp_path, monkeypatch, ["digest"])
    assert args.func(args) == 1  # el parser guarda la función sin ligar
    assert "starradar scan" in capsys.readouterr().err


def test_signals_command_without_digest_fails_cleanly(tmp_path, monkeypatch, capsys) -> None:
    args = _args(tmp_path, monkeypatch, ["signals", "--json"])
    assert cli.cmd_signals(args) == 1
    assert "starradar scan" in capsys.readouterr().err


def test_explain_rejects_bad_repo_format(tmp_path, monkeypatch, capsys) -> None:
    args = _args(tmp_path, monkeypatch, ["explain", "sin-slash"])
    assert cli.cmd_explain(args) == 2


def test_explain_of_unknown_repo_points_at_scan(tmp_path, monkeypatch, capsys) -> None:
    args = _args(tmp_path, monkeypatch, ["explain", "nadie/nada"])
    assert cli.cmd_explain(args) == 1
    # Va a stderr: es un diagnóstico, no la salida principal.
    assert "starradar scan" in capsys.readouterr().err


# -- señales Atlas ------------------------------------------------------------


def test_explain_renders_real_numbers_not_placeholders() -> None:
    """`explain()` es la respuesta a "¿por qué puntúa así?".

    Un f-string partido entre dos literales es fácil de romper al editar (una
    de las dos líneas deja de ser f y la cifra sale como `{self.stars:,}`).
    Este test fija el comportamiento para que ese fallo sea visible.
    """
    s = make_score("a/good", 82.0, True, "destacado")
    text = s.explain()
    assert "a/good" in text
    assert "1,200" in text          # stars formateadas
    assert "82.0/100" in text
    assert "0.0240" in text        # w/★
    assert "{self." not in text     # ningún f-string roto
    assert "{comp." not in text


def test_explain_lists_reasons_and_notes() -> None:
    s = make_score("b/bad", 30.0, False)
    text = s.explain()
    assert "A favor:" in text
    assert "En contra / avisos:" in text
    assert "w/★ por debajo del suelo" in text
    assert "[sin verificar]" in text


def test_only_verified_repos_become_signals() -> None:
    d = Digest(
        at=NOW,
        verified=[make_score("a/good", 82.0, True)],
        unverified=[make_score("b/bad", 30.0, False)],
        stats={},
    )
    sigs = digest_to_signals(d)
    assert len(sigs) == 1
    assert sigs[0]["entity"] == "a/good"
    assert sigs[0]["date_inferred"] is False
    assert sigs[0]["reports"] == ["starradar"]
    # Las cifras medidas viajan con la señal, para poder auditarla.
    assert sigs[0]["measured"]["stars"] == 1200
    assert sigs[0]["measured"]["watch_ratio"] == pytest.approx(0.0240)


def test_atlas_file_notes_unverified_were_excluded(tmp_path) -> None:
    d = Digest(
        at=NOW,
        verified=[make_score("a/good", 82.0, True)],
        unverified=[make_score("b/bad", 30.0, False)],
        stats={"n_verified": 1, "n_unverified": 1},
    )
    path = write_atlas(d, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["signals"]) == 1
    assert data["starradar"]["unverified_count"] == 1
    # Se declara que hubo repos descartados: nada se calla.
    assert any("autenticidad insuficiente" in n for n in data["not_verified"])
    # El momentum de temas se deja vacío: es trabajo de Atlas, no nuestro.
    assert data["momentum_by_theme"] == {}


def test_atlas_produces_zero_signals_when_nothing_verified(tmp_path) -> None:
    d = Digest(at=NOW, verified=[], unverified=[make_score("b/bad", 30.0, False)], stats={})
    data = json.loads(write_atlas(d, tmp_path).read_text(encoding="utf-8"))
    assert data["signals"] == []
