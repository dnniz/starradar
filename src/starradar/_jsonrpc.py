"""JSON-RPC 2.0 sobre stdio — el transporte mínimo que necesita un servidor MCP.

Se implementa aquí, con la librería estándar, para que `starradar mcp` no
requiera instalar el SDK `mcp`. Cubre exactamente lo que un cliente MCP (Hermes
incluido) necesita para descubrir y llamar herramientas:

* ``initialize``               — saludo y versión del protocolo.
* ``notifications/initialized``— se ignora (es notificación, sin id).
* ``tools/list``               — catálogo de herramientas con su JSON Schema.
* ``tools/call``               — ejecuta y devuelve el resultado.
* ``ping``                     — para tests y health checks.

Detalle que importa: **stdout es sólo para el protocolo**. Cualquier print() va
a stderr, o rompe la sesión del cliente. Por eso todo el logging de starradar
va a stderr (ver `github.py`) y aquí se fuerza `stdout` al modo binario.

Errores: se devuelven como resultado con `isError: true`, no como excepción de
transporte. Un cliente MCP espera un resultado; una excepción de socket deja la
sesión en un estado que el cliente no sabe recuperar.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from typing import IO, Any

# Versiones de protocolo que se anuncian. El cliente elige la intersección.
PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = ["2024-11-05", "2025-03-26", "2025-06-18"]


class JsonRpcStdio:
    """Bucle leer-escribir de JSON-RPC sobre dos file objects."""

    def __init__(
        self,
        name: str,
        schemas: list[dict[str, Any]],
        handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
        version: str = "0.0.0",
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
    ) -> None:
        self.name = name
        self.schemas = schemas
        self.handlers = handlers
        self.version = version
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.initialized = False

    # -- IO ---------------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        # ensure_ascii=False evita escapes_FLOSS en nombres con acentos; MCP
        # espera JSON UTF-8.
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def _ok(self, req_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _err(self, req_id: Any, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"jsonrpc": "2.0", "id": req_id, "error": err})

    # -- protocolo --------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # Notificaciones: sin id, no se responde.
        if req_id is None and str(method or "").startswith("notifications/"):
            if method == "notifications/initialized":
                self.initialized = True
            return

        if method == "initialize":
            proto = params.get("protocolVersion")
            negotiated = proto if proto in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
            self._ok(
                req_id,
                {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            )
            return

        if method == "ping":
            self._ok(req_id, {})
            return

        if method == "tools/list":
            self._ok(req_id, {"tools": self.schemas})
            return

        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            handler = self.handlers.get(name)
            if handler is None:
                # Herramienta inexistente: resultado de error, no excepción.
                self._ok(
                    req_id,
                    {
                        "isError": True,
                        "content": [{"type": "text", "text": f"herramienta desconocida: {name}"}],
                    },
                )
                return
            try:
                result = handler(arguments)
            except TypeError as exc:
                # Argumentos que no encajan con la firma: útil para el agente,
                # dice qué esperaba.
                self._ok(
                    req_id,
                    {
                        "isError": True,
                        "content": [
                            {"type": "text", "text": f"argumentos inválidos para {name}: {exc}"}
                        ],
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001 - el servidor no debe morir
                self._ok(
                    req_id,
                    {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": f"{type(exc).__name__}: {exc}",
                            }
                        ],
                    },
                )
                return

            text = result if isinstance(result, str) else json.dumps(
                result, ensure_ascii=False, indent=2
            )
            self._ok(
                req_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
            return

        # Método desconocido.
        if req_id is not None:
            self._err(req_id, -32601, f"método no implementado: {method}")

    def serve_forever(self) -> int:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Línea corrupta: se avisa pero no se muere.
                self._err(None, -32700, "parse error: JSON inválido")
                continue
            if not isinstance(msg, dict):
                self._err(None, -32600, "invalid request: no es un objeto")
                continue
            self.handle(msg)
        return 0
