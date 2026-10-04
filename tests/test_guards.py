"""Guardarraíles de calidad que no cubren ruff ni pytest.

Dos trampas concretas de este repo:

1. **Corrupción por redacción de secretos.** Al escribir ficheros, cualquier
   línea que *parezca* contener un secreto (un ``NOMBRE=`` pegado a texto que el
   filtro no puede distinguir de un token) se trunca. Ha pasado varias veces en
   comentarios y docstrings. Este test falla si aparece CJK o un resto
   claramente corrupto en el código, para no dejarlo pasar a revisión.

2. **Umbrales duplicados.** Los umbrales de scoring deben vivir sólo en
   :mod:`starradar.config`. Este test falla si aparece un literal mágico de
   umbral en otro módulo del paquete.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "starradar"
_ROOT = Path(__file__).resolve().parents[1]
# El filtro de redacción ha corrupto tanto el código como los tests y la
# documentación, así que la guarda cubre los tres.
FILES = sorted(set(SRC.glob("*.py")) | set((_ROOT / "tests").glob("*.py")))
DOCS = sorted(
    p
    for p in (_ROOT / "docs").rglob("*.md")
) + [_ROOT / "README.md", _ROOT / "AGENTS.md"]
DOCS = [p for p in DOCS if p.exists()]
# Este fichero contiene, por definición, los patrones que busca: se excluye
# de los chequeos de texto para que no se flags a sí mismo.
CHECKED = [p for p in FILES if p.name != "test_guards.py"]

# Cualquier glifo CJK o hangul, suelto o en grupo. Basta UNO para sospechar:
# los casos reales eran "sólo<GLIFO> no" (pegado a una palabra española) y
# "tres <GLIFOS> en producción" (en un párrafo normal).
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")

# Frases concretas que aparecieron al redactar y que no son español. Se listan
# una a una porque un diccionario de "palabras inglesas" produciría falsos
# positivos (nombres propios, `search`, `score`, `topic`...).
CORRUPTION_MARKERS = (
    ".httpspects",
    "corps[e] con",
    "*** ",
    "(?)",
    "tienedefaults",
    "accuse ",
    "reating",
    "occurred ",
)


def _iter_strings_and_comments(path: Path) -> list[tuple[int, str]]:
    """Saca (línea, texto) de todos los literales y comentarios del fichero."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            out.append((node.lineno, str(node.value.value)))
    return out


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: p.name)
def test_no_corrupted_text(path: Path) -> None:
    """Ningún literal o comentario con CJK ni marcadores de corrupción."""
    for lineno, text in _iter_strings_and_comments(path):
        assert not CJK.search(text), (
            f"{path.name}:{lineno} contiene CJK → texto corrupto en tránsito: {text!r}"
        )
        for marker in CORRUPTION_MARKERS:
            assert marker not in text, (
                f"{path.name}:{lineno} contiene marcador de corrupción {marker!r}: {text!r}"
            )


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: p.name)
def test_no_stray_assignment_asterisks(path: Path) -> None:
    """Ni asignaciones truncadas tipo ``NOMBRE=*** `` (revox, 2026-10-04)."""
    for lineno, text in _iter_strings_and_comments(path):
        assert not re.search(r"=\s*\*\*", text), (
            f"{path.name}:{lineno} asignación truncada por el filtro de secretos: {text!r}"
        )


def test_thresholds_only_in_config() -> None:
    """Los umbrales viven en config.py y nowhere más."""
    offenders: list[str] = []
    # Nombres de umbral que no pueden reaparecer como literales fuera de config.
    thresholds = {
        "AUTHENTICITY_FLOOR",
        "AUTHENTICITY_STRONG",
        "WEIGHT_VELOCITY",
        "WEIGHT_AUTHENTICITY",
        "WEIGHT_MOMENTUM",
        "MIN_COMPOSITE_SCORE",
        "MAX_AGE_DAYS",
        "STALE_DAYS",
    }
    for path in FILES:
        if path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        # Busca reasignaciones tipo `X = 0.4` o `X=0.4` fuera de config.
        for name in thresholds:
            if re.search(rf"^\s*{name}\s*=\s*[\d.]", text, re.MULTILINE):
                offenders.append(f"{path.name}: reasigna {name}")
    assert not offenders, "Umbrales duplicados fuera de config.py: " + ", ".join(offenders)


def test_weights_sum_to_one() -> None:
    """Los pesos del score compuesto deben sumar 1.0 o el score no es 0-100."""
    from starradar import config as cfg

    total = cfg.WEIGHT_VELOCITY + cfg.WEIGHT_AUTHENTICITY + cfg.WEIGHT_MOMENTUM
    assert abs(total - 1.0) < 1e-9, f"los pesos suman {total}, no 1.0"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_are_not_corrupted(path: Path) -> None:
    """La documentación también: los ADR son la parte que más se leyó.

    Los tres bugs del scan real se escribieron primero como ADR, y los tres
    textos salieron con glifos pegados al español. Una línea de doc con un
    glifo suelto es peor que un typo: cambia el significado de la frase sin
    que salte a la vista en la revisión.
    """
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert not CJK.search(line), (
            f"{path.name}:{lineno} contiene CJK → texto corrupto: {line!r}"
        )
        for marker in CORRUPTION_MARKERS:
            assert marker not in line, (
                f"{path.name}:{lineno} contiene marcador {marker!r}: {line!r}"
            )
