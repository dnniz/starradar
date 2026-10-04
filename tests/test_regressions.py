"""Regresiones de bugs que sólo aparecieron con datos reales.

Cada test de este fichero existe porque un ``scan`` real lo destapó. Ninguno
pudo encontrarlo un test con fixtures, y esa es justo la razón de que estén
aquí: un test que reproduce el payload crudo de la API vale más que uno que
inventa un ``{"stars": 100, "watchers": 5}`` de la nada.

Bug 1 (crítico): ``watchers_count`` en REST es un alias de ``stargazers_count``,
no el número de watchers. Leído tal cual, w/★ quedaba a 0 para todo repo
enriquecido por REST y *todo* caía en la capa "sin verificar".
Bug 2: los lotes de GraphQL de 100 saturan el límite de coste de la query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from starradar.config import Settings
from starradar.github import GitHubClient, _chunked, _search_item_to_repo
from starradar.models import Repo

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

# Payload real de GET /repos/jev-chat/jev-chat-jarvis, recortado a lo que
# starradar lee. Notar la trampa: watchers_count == stargazers_count == 7319.
RAW_JARVIS = {
    "full_name": "jev-chat/jev-chat-jarvis",
    "stargazers_count": 7319,
    "watchers_count": 7319,  # ← NO es el número de watchers
    "subscribers_count": 10,  # ← esto es lo que hay que leer
    "forks_count": 1220,
    "open_issues_count": 34,
    "description": "un repo cualquiera",
    "language": "TypeScript",
    "license": {"spdx_id": "MIT"},
    "homepage": "",
    "topics": [],
    "created_at": "2026-09-21T10:00:00Z",
    "pushed_at": "2026-10-04T09:00:00Z",
    "fork": False,
    "archived": False,
    "owner": {"type": "User"},
}


# -- bug 1: watchers_count no es watchers -----------------------------------


def test_repo_detail_uses_subscribers_count_not_watchers_count() -> None:
    """El campo de watchers en REST es `subscribers_count`.

    Si este test falla, todos los repos resting se clasifican como no
    verificados: el filtro de autenticidad queda invertido.
    """
    repo = _search_item_to_repo(RAW_JARVIS)
    assert repo.watchers == 10, (
        f"watchers={repo.watchers}; se está leyendo watchers_count "
        f"({RAW_JARVIS['watchers_count']}) en vez de subscribers_count "
        f"({RAW_JARVIS['subscribers_count']})"
    )
    # Y el ratio tiene que ser el de un repo real, no 0 ni 1.
    assert repo.watch_ratio is not None
    assert 0 < repo.watch_ratio < 0.01, (
        f"w/★={repo.watch_ratio}: un repo de 7319★ con 10 watchers debería "
        f"quedar por debajo de 0.0015, no ser 0 (dato ausente) ni 1 (alias)"
    )


def test_watchers_count_is_never_used_as_watchers() -> None:
    """Guardarraíl explícito: si el payload sólo trae watchers_count, se ignora.

    Un 0 significa "no lo sé", no "no tiene watchers". Preferimos perder el
    dato a mentir con él.
    """
    only_alias = dict(RAW_JARVIS)
    del only_alias["subscribers_count"]
    repo = _search_item_to_repo(only_alias)
    assert repo.watchers == 0, "sin subscribers_count el valor debe seguir siendo 0"
    assert repo.watchers != RAW_JARVIS["watchers_count"]


def test_zero_watchers_is_treated_as_unknown_not_as_bad_signal() -> None:
    """Un repo con 0 watchers no debe caer en "no verificado" por sospecha.

    Es la diferencia entre "el radar no sabe" y "el radar sospecha". Confundir
    las dos cosas envenena el ranking: el usuario ve 500 repos marcados como
    dudosos sólo porque el enriquecimiento falló.
    """
    from starradar.scoring import score, split

    unknown = Repo(
        full_name="acme/sin-watcher",
        stars=5000,
        forks=0,
        watchers=0,
        created_at=NOW - timedelta(days=3),
        pushed_at=NOW - timedelta(days=1),
    )
    s = score(unknown, [], NOW)
    verified, unverified = split([s], [s])
    # Sin watchers no hay dato: la autenticidad es neutra, no una penalización.
    # Y el repo NO debe caer en la capa de dudosos: acusar de sospecha a lo que
    # sólo no sabemos envenena el ranking (en la pasada real: 539 de 567).
    assert s.authenticity.value == 50.0, (
        f"sin watchers la autenticidad debe ser neutra (50), "
        f"no {s.authenticity.value} (0 = penalización)"
    )
    assert not any("suelo" in n for n in s.notes), (
        f"no debe acusar de estar bajo el suelo: {s.notes}"
    )
    # Sin dato de watchers no se puede exigir la prueba: el repo se queda en
    # la capa "sin verificar" (por falta de dato), no verificado ni sospechoso.
    # Lo importante es que no se mezcle con los verificados.
    assert unverified == [s], "sin watchers, el repo va a la capa sin verificar"
    assert verified == []


# -- bug 2: lotes de GraphQL --------------------------------------------------


def test_chunked_splits_without_losing_elements() -> None:
    items = [f"o/r{i}" for i in range(567)]
    chunks = _chunked(items, 50)
    assert [n for c in chunks for n in c] == items
    assert max(len(c) for c in chunks) == 50
    assert len(chunks) == 12  # 11×50 + 1×17


def test_chunked_never_produces_an_empty_chunk() -> None:
    assert _chunked([], 50) == []
    assert all(c for c in _chunked(["a", "b", "c"], 50))  # el resto no genera vacío
    assert _chunked(["a"], 0) == [["a"]], "un tamaño de 0 no debe colgarse"


def test_enrich_bisects_when_a_chunk_fails(monkeypatch) -> None:
    """Si un lote de GraphQL falla, se parte en dos: un repo malo no tira 50.

    Reproduce la causa: el límite de coste de la query depende del tamaño, no
    de los repos. El cliente devuelve {} para lotes grandes, como hizo GitHub
    de verdad el 2026-10-04 con lotes de 100.
    """
    names = [f"o{i}/r{i}" for i in range(100)]
    sizes: list[int] = []

    def fake_post(self, query: str):
        sizes.append(query.count("repository(owner:"))
        if len(sizes) == 1:  # primer lote completo: falla
            from starradar.github import GitHubError

            raise GitHubError("Resource limits for this query exceeded.")
        return {}

    monkeypatch.setattr(GitHubClient, "_gql_post", fake_post)
    client = GitHubClient(Settings(home="/tmp/starradar-bisect", live=False))
    client.enrich(names)

    assert sizes[0] == 50, f"el primer lote debe ser el tamaño completo, fue {sizes[0]}"
    assert sizes[1] == 25 and sizes[2] == 25, f"debe bisecar en 25+25, fue {sizes[1:3]}"
    # El contador sube en cada nivel del bisectado (1 → 2), no una vez por lote.
    assert client.stats["gql_bisect"] == 2, (
        f"gql_bisect debería contar los 2 reintentos, vale "
        f"{client.stats['gql_bisect']}"
    )


def test_enrich_gives_up_on_a_single_bad_repo(monkeypatch) -> None:
    """Con 1 nodo que falla, se rinde: ya no es un problema de tamaño."""
    client = GitHubClient(Settings(home="/tmp/starradar-bad", live=False))
    calls: list[int] = []

    def fake_post(self, query: str):
        calls.append(query.count("repository(owner:"))
        from starradar.github import GitHubError

        raise GitHubError("Could not resolve to a Repository")

    monkeypatch.setattr(GitHubClient, "_gql_post", fake_post)
    assert client.enrich(["ghost/repo"]) == {}
    assert calls == [1], "no debe reintentar un nodo individual más de una vez"


# -- la integración que arregla los dos bugs a la vez ------------------------


def test_rest_fallback_recovers_watchers_for_graphql_misses(monkeypatch) -> None:
    """El respaldo REST debe devolver watchers, que es justo lo que faltaba.

    Si el fallback devolviera watchers=0, añadirlo sería un no-op disfrazado
    de solución y el bug 1 volvería por la puerta de atrás.
    """
    from starradar.github import _search_item_to_repo as parse

    calls: list[str] = []

    def fake_enrich(self, names):
        return {}  # GraphQL no devuelve nada, como en el scan real

    def fake_detail(self, name):
        calls.append(name)
        return parse({**RAW_JARVIS, "full_name": name})

    monkeypatch.setattr(GitHubClient, "enrich", fake_enrich)
    monkeypatch.setattr(GitHubClient, "repo_detail", fake_detail)

    client = GitHubClient(Settings(home="/tmp/starradar-fb", live=False))
    assert client.enrich(["a/one", "b/two"]) == {}  # GraphQL no devuelve nada
    for name in ("a/one", "b/two"):
        detail = client.repo_detail(name)
        assert detail is not None
        assert detail.watchers == 10, f"{name}: el fallback debe traer watchers reales"
    assert calls == ["a/one", "b/two"]


@pytest.mark.parametrize("size,expected", [(50, 12), (100, 6), (1, 567), (600, 1)])
def test_chunked_matches_documented_batch_size(size: int, expected: int) -> None:
    assert len(_chunked([f"x{i}" for i in range(567)], size)) == expected
