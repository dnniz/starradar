"""Dashboard web de sólo lectura.

Servido con la librería estándar (`http.server`): cero dependencias de runtime,
arranca en milisegundos y expone exactamente el mismo digest que la CLI. La
alternativa (FastAPI/uvicorn) añadiría dos dependencias y arranque lento para una
página que sólo lee de un SQLite local y devuelve JSON.

Decisiones de producto:

* **Sólo lectura y sólo localhost por defecto.** Es un radar personal; no
  expone nada al exterior sin que alguien lo pida explícitamente.
* **Sin JS frameworks**: una sola página con un pequeño script. Se lee igual
  de bien en el móvil y no hay build step que mantener.
* **Accesible**: contraste AA, foco visible, respeta `prefers-reduced-motion`,
  y los números no dependen sólo del color para distinguirse.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .store import Store

TEMPLATE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>starradar</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --fg: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --good: #3fb950; --warn: #d29922; --bad: #f85149;
    --radius: 10px; --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f8fa; --panel: #fff; --border: #d0d7de;
      --fg: #1f2328; --muted: #59636e; --accent: #0969da;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 6px; }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  .meta {
    display: flex; gap: 18px; flex-wrap: wrap; color: var(--muted);
    font-size: 13px; margin: 14px 0 26px; padding-bottom: 18px;
    border-bottom: 1px solid var(--border);
  }
  .meta b { color: var(--fg); font-variant-numeric: tabular-nums; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
       color: var(--muted); margin: 30px 0 12px; font-weight: 600; }
  h2 .n { color: var(--fg); }
  .row {
    display: grid; grid-template-columns: 1fr 84px 78px 78px 86px;
    gap: 12px; align-items: center; padding: 11px 14px; margin-bottom: 6px;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  .row:hover { border-color: var(--accent); }
  .row a { color: var(--fg); text-decoration: none; font-weight: 600; }
  .row a:hover { color: var(--accent); text-decoration: underline; }
  .desc { color: var(--muted); font-size: 13px; margin-top: 3px;
          display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
          overflow: hidden; }
  .num { font-family: var(--mono); font-variant-numeric: tabular-nums;
         text-align: right; font-size: 13px; }
  .score { font-weight: 700; font-size: 15px; }
  .bar { height: 5px; background: var(--border); border-radius: 3px;
         overflow: hidden; margin-top: 5px; }
  .bar > i { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
  .tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 20px;
         border: 1px solid var(--border); color: var(--muted); margin-right: 4px; }
  .verdict { font-size: 11px; padding: 1px 8px; border-radius: 20px; }
  .v-destacado { color: var(--good); border: 1px solid var(--good); }
  .v-solido { color: var(--accent); border: 1px solid var(--accent); }
  .v-temprano { color: var(--muted); border: 1px solid var(--border); }
  .v-verificar { color: var(--warn); border: 1px solid var(--warn); }
  .empty, .note {
    background: var(--panel); border: 1px dashed var(--border); border-radius: var(--radius);
    padding: 20px; color: var(--muted); font-size: 14px;
  }
  .note { margin-top: 10px; border-style: solid; border-left: 3px solid var(--warn); }
  a:focus-visible, .row:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
  footer { color: var(--muted); font-size: 12px; margin-top: 40px;
           padding-top: 16px; border-top: 1px solid var(--border); }
  @media (max-width: 720px) {
    .row { grid-template-columns: 1fr 64px 64px; }
    .hide-s { display: none; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>starradar</h1>
    <span class="sub">repos nuevos con estrellas en crecimiento real</span>
  </header>
  <div class="meta" id="meta"></div>
  <div id="app"></div>
  <footer id="foot"></footer>
</div>
<script>
const esc = s => (s || "").replace(/[&<>"]/g, c => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => (n || 0).toLocaleString("es");
const VCLASS = {destacado:"v-destacado", "sólido":"v-solido", temprano:"v-temprano",
                "sin verificar":"v-verificar"};

function row(s) {
  const url = "https://github.com/" + s.repo;
  const pct = Math.max(0, Math.min(100, s.total));
  const wr = s.watch_ratio == null ? "n/d" : s.watch_ratio.toFixed(4);
  return `<div class="row">
    <div>
      <a href="${url}" target="_blank" rel="noopener">${esc(s.repo)}</a>
      <span class="verdict ${VCLASS[s.verdict] || "v-temprano"}">${esc(s.verdict)}</span>
      <div class="desc">${esc(s.components.authenticity.detail)}</div>
    </div>
    <div class="num"><span class="score">${s.total.toFixed(1)}</span>
      <div class="bar"><i style="width:${pct}%"></i></div></div>
    <div class="num">${fmt(s.stars)}★</div>
    <div class="num">${s.stars_per_day.toFixed(1)}/d</div>
    <div class="num hide-s">w/★ ${wr}</div>
  </div>`;
}

function render(d) {
  if (!d || d.empty) {
    document.getElementById("app").innerHTML =
      '<div class="empty">Sin digest todavía. Ejecuta <code>starradar scan</code>.</div>';
    return;
  }
  const st = d.stats || {};
  document.getElementById("meta").innerHTML = [
    ["descubiertos", st.n_discovered], ["verificados", st.n_verified],
    ["sin verificar", st.n_unverified_count ?? (d.unverified || []).length],
    ["velocidad", st.velocity_source], ["días de datos", st.tracked_days],
  ].map(([k, v]) => `<span>${k} <b>${esc(String(v ?? "—"))}</b></span>`).join("");

  const v = d.verified || [];
  const u = d.unverified || [];
  let html = "";
  html += `<h2>Verificados <span class="n">${v.length}</span></h2>`;
  html += v.length ? v.map(row).join("")
    : '<div class="empty">Todavía ningún repo pasa el filtro de confianza. ' +
      'Se necesitan snapshots de dos días para medir velocidad.</div>';
  if (u.length) {
    html += `<h2>Sin verificar <span class="n">${u.length}</span></h2>`;
    html += '<div class="note">Estas estrellas no han demostrado crecimiento ' +
            '(watchers/estrellas por debajo del suelo). No las tomes como señal. ' +
            '<code>starradar explain &lt;repo&gt;</code> da el detalle.</div>';
    html += u.slice(0, 8).map(row).join("");
  }
  document.getElementById("app").innerHTML = html;
  document.getElementById("foot").textContent =
    "Actualizado: " + (d.at || "—") + (st.quota ? " · cuota " + st.quota : "");
}

fetch("api/digest").then(r => r.json()).then(render).catch(e => {
  document.getElementById("app").innerHTML =
    '<div class="empty">No se pudo cargar el digest: ' + esc(e.message) + "</div>";
});
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    settings: Settings
    store: Store

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silencia el log a stdout: si no, corrompe la salida de la CLI."""
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # El dashboard es local: no necesita CSP estricta ni nada caro, pero
        # sí no-cache para que recargar muestra datos frescos.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - nombre impuesto por la clase base
        path = urlparse(self.path)
        if path.path in ("/", "/index.html"):
            self._send(200, TEMPLATE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.path == "/api/digest":
            raw = self.store.last_digest()
            if raw is None:
                payload: dict[str, Any] = {
                    "empty": True,
                    "message": "sin digest; ejecuta `starradar scan`",
                }
            else:
                raw.setdefault("unverified_count", len(raw.get("unverified", [])))
                payload = raw
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path.path == "/api/repo":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            self._send(
                200,
                json.dumps({"repo": name, "found": bool(self.store.get_repos([name]))}).encode(),
                "application/json; charset=utf-8",
            )
            return
        if path.path == "/healthz":
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b"no encontrado", "text/plain; charset=utf-8")


def serve(host: str = "127.0.0.1", port: int = 8787, settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()

    class Handler(_Handler):
        pass

    Handler.settings = settings
    Handler.store = Store(settings.db_path)

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"starradar dashboard en http://{host}:{port}  (Ctrl-C para salir)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nparado")
    finally:
        httpd.server_close()
    return 0
