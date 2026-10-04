"""Configuración de starradar.

Todos los umbrales viven aquí, en un solo sitio, y son los mismos que usa la
CLI, el MCP server y el digest. Si un número aparece hardcodeado en otro
módulo, es un bug: los umbrales son parte del contrato público y se documentan
en docs/scoring.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------

ENV_HOME = "STARRADAR_HOME"


def default_home() -> Path:
    """Directorio de estado (BD, caché, snapshots).

    Respeta ``STARRADAR_HOME`` y, si no, cae en ``~/.starradar``. Nunca se
    escribe dentro del repo: el estado es de la máquina, no del código.
    """
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".starradar"


# --------------------------------------------------------------------------
# Autenticidad
# --------------------------------------------------------------------------

#: Suelo de watchers/estrellas por debajo del cual no se considera orgánico.
#:
#: Medido el 2026-10-04 sobre 10 proyectos establecidos (linux, rust, deno,
#: react, openai-python...): mediana 0.0240, rango [0.0075, 0.0337]. El suelo
#: se fija en el mínimo observado, no en la mediana, para no descartar de más:
#: preferimos un falso positivo (marcado como "dudoso") a perder un repo real.
AUTHENTICITY_FLOOR = 0.0075

#: Por encima de este ratio el repo pasa a "orgánico confirmado". Cubre
#: ~0.6x la mediana de los establecidos, sin exigir atención unsustainable.
AUTHENTICITY_STRONG = 0.015

# --------------------------------------------------------------------------
# Puntuación
# --------------------------------------------------------------------------

#: Pesos del score compuesto. Suman 1.0 con la lista actual.
WEIGHT_VELOCITY = 0.45
WEIGHT_AUTHENTICITY = 0.35
WEIGHT_MOMENTUM = 0.20

#: Repos por debajo de este score no entran en el ranking verificado, aunque
#: autentiquen bien: score bajo = todavía no hay señal suficiente.
MIN_COMPOSITE_SCORE = 20.0

#: Antigüedad máxima (días) para considerar un repo "nuevo". Pasado esto, el
#: crecimiento deja de ser la historia interesante.
MAX_AGE_DAYS = 90

#: Sin push reciente el repo se considera en abandono, por muy rápido que
#: fuese el mes pasado.
STALE_DAYS = 30


@dataclass(slots=True)
class Query:
    """Una consulta de descubrimiento.

    GitHub no soporta OR entre topics (devuelve 422), así que la discovery
    lanza **una query por Query** y hace UNION + dedupe en cliente. Ver
    docs/architecture.md.
    """

    q: str
    sort: str = "stars"
    per_page: int = 100
    pages: int = 2


@dataclass(slots=True)
class Settings:
    """Ajustes de una ejecución. Todo tiene default: se puede usar sin config.

    ``home`` acepta ``str`` o ``Path`` y se normaliza siempre a ``Path``. Sin
    esa normalización, ``Settings(home="/tmp/x").db_path`` revienta con un
    TypeError en la primera property — un fallo que sólo aparece en el segundo
    uso, que es el más difícil de depurar.
    """

    token: str = ""
    home: Path = field(default_factory=default_home)
    user_agent: str = "starradar/0.1.0 (+https://github.com/dnniz/starradar)"
    timeout: float = 20.0

    #: Ventana de descubrimiento: repos creados en los últimos N días.
    window_days: int = 30
    #: Suelo de estrellas para entrar en la búsqueda. Necesario porque
    #: `sort=stars` trunca a 1000 resultados: sin suelo, un repo nuevo con
    #: 80★ nunca aparecería bajo un incumbent de 38.000★.
    star_floor: int = 100
    per_page: int = 100
    pages: int = 2

    #: Repos por lote de GraphQL. El coste de la query crece con el tamaño, y
    #: por encima de ~50 GitHub empieza a devolver "Resource limits for this
    #: query exceeded" en lugar de un error claro. Con 50 + bisectado al fallar,
    #: un solo repo problemático no tira el lote entero.
    graphql_batch: int = 50

    #: Temas a consultar. Uno por query, por lo de arriba.
    topics: tuple[str, ...] = ("ai", "llm", "agent", "mcp", "cli", "devtools")
    languages: tuple[str, ...] = ()

    #: Si es False, la ejecución no toca la red. Los tests corren así.
    live: bool = True

    def __post_init__(self) -> None:
        # Acepta str y lo normaliza: ver el docstring de la clase.
        self.home = Path(self.home)

    @classmethod
    def from_env(cls) -> Settings:
        """Construye desde el entorno.

        Acepta ``STARRADAR_TOKEN`` y, como alias, ``GH_TOKEN`` / ``GITHUB_TOKEN``
        para no obligar a exportar una variable nueva si ya existe otra.
        """
        return cls(
            token=(
                os.environ.get("STARRADAR_TOKEN")
                or os.environ.get("GH_TOKEN")
                or os.environ.get("GITHUB_TOKEN")
                or ""
            ),
            home=default_home(),
            window_days=int(os.environ.get("STARRADAR_WINDOW_DAYS", "30")),
            star_floor=int(os.environ.get("STARRADAR_STAR_FLOOR", "100")),
            live=os.environ.get("STARRADAR_LIVE", "1") != "0",
        )

    # -- rutas derivadas ---------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.home / "starradar.db"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
