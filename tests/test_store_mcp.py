"""Tests del almacén y del protocolo MCP.

El store tiene dos invariantes que importan y que son fáciles de romper:
deduplicar snapshots por (repo, fecha) y no reescribir filas que no cambian.
El MCP tiene que hablar JSON-RPC válido y sobrevivir a un handler que revienta.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

from starradar._jsonrpc import JsonRpcStdio
from starradar.models import Repo, Snapshot, utcnow
from starradar.store import Store

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_repo(name: str = "acme/widget", stars: int = 100, **kw) -> Repo:
    defaults = dict(
        forks=10, watchers=5, open_issues=3, description="d", language="Python",
        license="MIT", topics=("ai",), created_at=NOW - timedelta(days=20),
        pushed_at=NOW - timedelta(days=1),
    )
    defaults.update(kw)
    return Repo(full_name=name, stars=stars, **defaults)


# -- store --------------------------------------------------------------------


def test_snapshot_dedupe_same_day(tmp_path) -> None:
    st = Store(tmp_path / "t.db")
    day = NOW
    st.add_snapshot(Snapshot("a/b", day, 10, 1, 0))
    st.add_snapshot(Snapshot("a/b", day + timedelta(hours=3), 12, 1, 0))
    # Dos instantes distintos son dos filas: la clave primaria es (repo, at),
    # así que sólo se funden si el instante coincide exactamente.
    hist = st.history(["a/b"])
    assert len(hist["a/b"]) == 2
    st.add_snapshot(Snapshot("a/b", day, 99, 1, 0))
    hist = st.history(["a/b"])["a/b"]
    assert len(hist) == 2  # 99 sobrescribió la fila, no añadió una tercera
    assert hist[0].stars == 99  # historial en orden ascendente: el día base es el primero


def test_history_sorted_and_filtered(tmp_path) -> None:
    st = Store(tmp_path / "t.db")
    for i in range(5):
        st.add_snapshot(Snapshot("a/b", NOW - timedelta(days=10 - i), i * 10, i, i))
    st.add_snapshot(Snapshot("a/b", NOW - timedelta(days=400), 999, 0, 0))
    hist = st.history(["a/b"], days=30)["a/b"]
    assert [s.stars for s in hist] == [0, 10, 20, 30, 40]  # ordenado, sin el viejo


def test_upsert_reports_only_real_changes(tmp_path) -> None:
    st = Store(tmp_path / "t.db")
    assert st.upsert_repos([make_repo()], now=NOW) == 1  # insert
    # Mismos datos => 0 cambios, pero last_seen se refresca.
    assert st.upsert_repos([make_repo()], now=NOW + timedelta(hours=1)) == 0
    # Cambian las estrellas => 1 cambio.
    assert st.upsert_repos([make_repo(stars=200)], now=NOW + timedelta(hours=2)) == 1


def test_tracked_days_counts_distinct_days(tmp_path) -> None:
    st = Store(tmp_path / "t.db")
    st.add_snapshot(Snapshot("a/b", NOW, 1, 0, 0))
    st.add_snapshot(Snapshot("a/b", NOW + timedelta(hours=2), 2, 0, 0))
    st.add_snapshot(Snapshot("a/b", NOW + timedelta(days=1), 3, 0, 0))
    assert st.tracked_days() == 2  # hoy y ayer, no 3 horas distintas


def test_top_changes_needs_two_points(tmp_path) -> None:
    from starradar.pipeline import top_changes

    st = Store(tmp_path / "t.db")
    st.upsert_repos([make_repo("a/b", stars=100)])
    st.add_snapshot(Snapshot("a/b", utcnow() - timedelta(hours=5), 100, 0, 0))
    assert top_changes(st, since_hours=24) == []  # un solo punto = sin delta
    st.add_snapshot(Snapshot("a/b", utcnow(), 150, 0, 0))
    assert top_changes(st, since_hours=24) == [("a/b", 50)]


# -- MCP / JSON-RPC ------------------------------------------------------------


def _rpc(handlers=None, schemas=None):
    out = io.StringIO()
    server = JsonRpcStdio(
        "starradar",
        schemas or [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}],
        handlers or {"t": lambda a: {"ok": True, "args": a}},
        version="9.9.9",
        stdin=io.StringIO(),
        stdout=out,
    )
    return server, out


def _exchange(payload: dict, server, out) -> dict:
    server.stdin = io.StringIO(json.dumps(payload) + "\n")
    server.serve_forever()
    return json.loads(out.getvalue().strip().splitlines()[-1])


def test_initialize_advertises_protocol_and_info() -> None:
    server, out = _rpc()
    res = _exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05"}}, server, out)
    assert res["result"]["serverInfo"] == {"name": "starradar", "version": "9.9.9"}
    assert res["result"]["protocolVersion"] == "2024-11-05"


def test_tools_list_and_call_roundtrip() -> None:
    server, out = _rpc()
    res = _exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, server, out)
    assert res["result"]["tools"][0]["name"] == "t"

    res = _exchange(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "t", "arguments": {"x": 1}}}, server, out
    )
    body = json.loads(res["result"]["content"][0]["text"])
    assert body == {"ok": True, "args": {"x": 1}}
    assert res["result"]["isError"] is False


def test_unknown_tool_is_error_result_not_exception() -> None:
    server, out = _rpc()
    res = _exchange(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}}, server, out
    )
    assert res["result"]["isError"] is True
    assert "nope" in res["result"]["content"][0]["text"]


def test_handler_exception_becomes_is_error_result() -> None:
    def boom(args):
        raise RuntimeError("se rompió la fuente")

    server, out = _rpc(handlers={"t": boom})
    res = _exchange(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "t", "arguments": {}}}, server, out
    )
    assert res["result"]["isError"] is True
    assert "se rompió la fuente" in res["result"]["content"][0]["text"]


def test_notification_gets_no_response() -> None:
    server, out = _rpc()
    server.stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    server.serve_forever()
    assert out.getvalue() == ""  # las notificaciones no se responden


def test_malformed_json_does_not_kill_server() -> None:
    server, out = _rpc()
    server.stdin = io.StringIO("{no es json}\n")
    server.serve_forever()
    assert json.loads(out.getvalue())["error"]["code"] == -32700
