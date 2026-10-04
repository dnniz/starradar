"""Tests que tocan la API real de GitHub.

Se marcan `live` y sólo corren si hay token. Su razón de existir: el 90% de los
fallos de starradar son de contrato con GitHub (un campo que desaparece, un
422 nuevo, un límite que se estrecha), y none se ven en un test con fixtures.

Uso:

    STARRADAR_TOKEN=... pytest -m live -q

En CI no hay token, así que se saltan. Eso es aceptable a propósito: lo que se
verifica en cada commit es la lógica; el contrato con la API se verifica cuando
alguien tiene un token, y aquí hay un test que lo hace explícito en vez de
dejarlo sólo en la documentación.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from starradar.config import Settings
from starradar.github import GitHubClient
from starradar.models import utcnow

pytestmark = pytest.mark.live

TOKEN = os.environ.get("STARRADAR_TOKEN", "")

requires_token = pytest.mark.skipif(
    not TOKEN,
    reason="sin STARRADAR_TOKEN: test de contrato con la API real, se salta",
)


@pytest.fixture(scope="module")
def client() -> GitHubClient:
    s = Settings(token=TOKEN, home="/tmp/starradar-live-tests")
    s.ensure_dirs()
    return GitHubClient(s)


@requires_token
def test_search_returns_repos_with_expected_shape(client: GitHubClient) -> None:
    """El contrato de `search` que starradar da por supuesto."""
    found = client.search(window_days=90, star_floor=50, topics=["llm"], per_topic=10)
    assert found, "la búsqueda debe devolver algo para topic:llm, ventana 90d"
    for name, info in list(found.items())[:5]:
        assert name.count("/") == 1, f"nombre mal normalizado: {name}"
        assert isinstance(info.get("stars", 0), int)
        assert info.get("created_at"), "sin created_at no se puede medir la edad"


@requires_token
def test_enrich_returns_watchers(client: GitHubClient) -> None:
    """Watchers es el campo que hace posible el filtro de autenticidad.

    Si esto falla, el ranking entero pierde su base: documentado en
    docs/github-api.md §4.
    """
    repos = client.enrich(["openai/openai-python", "astral-sh/ruff"])
    assert repos, "GraphQL debe devolver detalle de repos conocidos"
    for name, r in repos.items():
        assert r.watchers >= 0, f"{name} sin watchers"
        assert r.stars > 0
        assert r.watch_ratio is not None, f"{name}: watch_ratio None (stars={r.stars})"


@requires_token
def test_search_and_enrich_agree_on_stars(client: GitHubClient) -> None:
    """Search y GraphQL no deben discrepar: si lo hacen, el score es ruido.

    Una discrepancia pequeña (±2) es normal por el instante de lectura. Una
    grande significaría que estamos mezclando dos fuentes distintas.
    """
    found = client.search(window_days=120, star_floor=100, topics=["agent"], per_topic=5)
    names = list(found)[:5]
    if not names:
        pytest.skip("sin resultados en esta ventana")
    enriched = client.enrich(names)
    for name in names:
        if name not in enriched:
            continue
        delta = abs(enriched[name].stars - found[name]["stars"])
        assert delta <= max(5, found[name]["stars"] * 0.02), (
            f"{name}: search dice {found[name]['stars']}★, graphql {enriched[name].stars}★"
        )


@requires_token
def test_repo_detail_uses_etag_cache(client: GitHubClient) -> None:
    """La segunda llamada al mismo repo debe salir por caché (304)."""
    name = "dnniz/starradar" if _exists(client, "dnniz/starradar") else "openai/openai-python"
    client.stats.clear()
    client.repo_detail(name)
    first = client.stats.get("not_modified", 0)
    client.repo_detail(name)
    second = client.stats.get("not_modified", 0)
    assert first == 0, "la primera llamada no debería usar caché"
    assert second == 1, f"la segunda debe dar 304; not_modified={second}"


def _exists(client: GitHubClient, name: str) -> bool:
    try:
        return client.repo_detail(name) is not None
    except Exception:
        return False


@requires_token
def test_discovered_repos_are_recently_created(client: GitHubClient) -> None:
    """La ventana de descubrimiento debe respetarse: si no, entra ruido viejo."""
    now = utcnow()
    found = client.search(window_days=30, star_floor=20, topics=["cli"], per_topic=10)
    for name, info in found.items():
        created = info.get("created_at")
        if not created:
            continue
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age = (now - created_at).total_seconds() / 86400
        assert age <= 35, f"{name} tiene {age:.0f} días y la ventana era de 30"
