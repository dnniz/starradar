"""Cliente de GitHub: REST para descubrir, GraphQL para enriquecer.

Decisiones que vienen de medición, no de suposición (ver
docs/architecture.md y la skill `unattended-scheduled-jobs`):

* La **búsqueda no devuelve ETag**, así que sus respuestas no se cachean. El
  core REST **sí** cachea con ETag/304 y por tanto no gasta cuota. Por eso las
  llamadas individuales van por REST con caché, y la búsqueda se usa sólo para
  *descubrir* nombres.
* La búsqueda **no soporta OR**: ``topic:ai OR topic:llm`` devuelve 422. Se
  lanza una query por topic y se hace UNION + dedupe aquí.
* ``sort=stars`` **trunca a 1000 resultados**, así que se añade un suelo de
  estrellas para que un repo nuevo no desaparezca bajo los incumbents.
* ``subscribers_count`` no viene en búsqueda; watchers sí llegan por GraphQL.
* El campo GraphQL correcto es ``nameWithOwner`` (``fullName`` no existe).

Todo es síncrono y con reintentos acotados: las llamadas son de red y una
ejecución colgada es peor que una ejecución con menos datos.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .models import Repo, parse_dt, utcnow

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"


class GitHubError(RuntimeError):
    """Error de la API de GitHub, con el estado HTTP si lo hubo."""


# --------------------------------------------------------------------------
# Normalización
# --------------------------------------------------------------------------


def _search_item_to_repo(item: dict[str, Any]) -> Repo:
    """Convierte un item de /search/repositories a :class:`Repo`.

    Trampa de la API, documentada aquí para que nadie la reintroduzca:
    ``watchers_count`` **NO es el número de watchers**, es un alias histórico
    de ``stargazers_count``. Medido el 2026-10-04 sobre
    ``jev-chat/jev-chat-jarvis``: ``watchers_count`` = 7319 = ``stargazers_count``,
    mientras el número real de watchers es ``subscribers_count`` = 10.
    Leer ``watchers_count`` hace que w/★ = 1.0 para todo y arruina el filtro de
    autenticidad. El campo correcto en REST es ``subscribers_count``; en
    GraphQL es ``watchers { totalCount }``.

    La búsqueda no trae ninguno de los dos, así que aquí se deja 0 y lo rellena
    el enriquecimiento. Un 0 significa "desconocido", nunca "no tiene watchers".
    """
    owner = item.get("owner") or {}
    return Repo(
        full_name=item.get("full_name", ""),
        stars=int(item.get("stargazers_count") or 0),
        forks=int(item.get("forks_count") or 0),
        watchers=int(item.get("subscribers_count") or 0),
        open_issues=int(item.get("open_issues_count") or 0),
        description=item.get("description"),
        language=item.get("language"),
        license=(item.get("license") or {}).get("spdx_id"),
        homepage=item.get("homepage") or None,
        topics=tuple(item.get("topics") or ()),
        created_at=parse_dt(item.get("created_at")),
        pushed_at=parse_dt(item.get("pushed_at")),
        is_fork=bool(item.get("fork")),
        is_archived=bool(item.get("archived")),
        owner_type=owner.get("type"),
    )


# La query GraphQL que funciona: nameWithOwner (no fullName), sin
# mentionableUsers (provoca RESOURCE_LIMITS_EXCEEDED), con watchers para el
# ratio de autenticidad.
_GQL_REPO_FIELDS = """
  nameWithOwner
  stargazerCount
  forkCount
  createdAt
  pushedAt
  isArchived
  isFork
  description
  homepageUrl
  openGraphqlIssues: issues(states: OPEN) { totalCount }
  openGraphqlPulls: pullRequests(states: OPEN) { totalCount }
  watchers { totalCount }
  licenseInfo { spdxId }
  primaryLanguage { name }
  repositoryTopics(first: 12) { nodes { topic { name } } }
"""


@dataclass(slots=True)
class RateLimit:
    """Estado de cuota, para poder avisar antes de agotarla."""

    core_remaining: int = 0
    search_remaining: int = 0
    core_reset: datetime | None = None

    @property
    def search_exhausted(self) -> bool:
        return self.search_remaining <= 2

    def summary(self) -> str:
        return f"core {self.core_remaining} · búsqueda {self.search_remaining}"


class GitHubClient:
    """Cliente REST + GraphQL con ETag en disco y reintentos."""

    def __init__(self, settings: Settings, token: str | None = None) -> None:
        self.s = settings
        self.token = token or settings.token
        if not self.token and settings.live:
            raise GitHubError(
                "Falta el token. Define STARRADAR_TOKEN, GH_TOKEN o GITHUB_TOKEN."
            )
        self._cache: dict[str, str] = {}  # url -> etag
        self._cache_dir = settings.cache_dir
        self.rate = RateLimit()
        self.stats = {
            "rest": 0,
            "rest_304": 0,
            "search": 0,
            "graphql": 0,
            "gql_bisect": 0,
            "errors": 0,
        }

    # -- HTTP ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.s.user_agent,
        }

    def _cache_file(self, url: str) -> Path:
        import hashlib

        return self._cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.etag"

    def _read_etag(self, url: str) -> str | None:
        if url in self._cache:
            return self._cache[url]
        f = self._cache_file(url)
        if f.exists():
            try:
                etag = f.read_text(encoding="utf-8").strip()
                if etag:
                    self._cache[url] = etag
                    return etag
            except OSError:
                pass
        return None

    def _write_etag(self, url: str, etag: str) -> None:
        self._cache[url] = etag
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file(url).write_text(etag, encoding="utf-8")
        except OSError:
            pass  # la caché es una optimización, nunca un requisito

    def _request(
        self, url: str, *, use_etag: bool = False, tries: int = 3
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """GET con reintentos acotados y backoff. Devuelve (status, json, headers)."""
        import httpx

        last_err: Exception | None = None
        for attempt in range(tries):
            headers = self._headers()
            if use_etag:
                etag = self._read_etag(url)
                if etag:
                    headers["If-None-Match"] = etag
            try:
                with httpx.Client(timeout=self.s.timeout) as client:
                    resp = client.get(url, headers=headers)
            except Exception as exc:  # red, DNS, timeout
                last_err = exc
                if attempt < tries - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise GitHubError(f"fallo de red en {url}: {exc}") from exc

            self._update_rate(resp.headers)

            if resp.status_code == 304:
                self.stats["rest_304"] += 1
                return 304, {}, dict(resp.headers)

            if resp.status_code == 200:
                if use_etag:
                    etag = resp.headers.get("etag")
                    if etag:
                        self._write_etag(url, etag)
                try:
                    return 200, resp.json(), dict(resp.headers)
                except json.JSONDecodeError as exc:
                    raise GitHubError(f"respuesta no-JSON de {url}") from exc

            if resp.status_code in (403, 429):
                # Cuota agotada o secundario: esperar y reintentar una vez.
                if attempt < tries - 1:
                    reset = resp.headers.get("x-ratelimit-reset")
                    delay = 2.0
                    if reset and reset.isdigit():
                        reset_dt = datetime.fromtimestamp(int(reset), tz=UTC)
                        delay = max(2.0, min(20.0, (reset_dt - utcnow()).total_seconds()))
                    time.sleep(delay)
                    continue

            if resp.status_code == 422:
                raise GitHubError(f"422 de {url}: {resp.text[:200]}")

            if resp.status_code == 404:
                return 404, {}, dict(resp.headers)

            if attempt < tries - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            raise GitHubError(f"HTTP {resp.status_code} de {url}: {resp.text[:200]}")

        raise GitHubError(f"agotados los reintentos de {url}: {last_err}")

    def _update_rate(self, headers: Mapping[str, str]) -> None:
        def get(k: str) -> int | None:
            v = headers.get(k) or headers.get(k.title())
            return int(v) if v and v.isdigit() else None

        core = get("x-ratelimit-remaining")
        search = get("x-ratelimit-search-remaining")
        if core is not None and get("x-ratelimit-resource") in (None, "core"):
            self.rate.core_remaining = core
        if search is not None:
            self.rate.search_remaining = search
        reset = headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            self.rate.core_reset = datetime.fromtimestamp(int(reset), tz=UTC)

    # -- Búsqueda (descubrimiento) ---------------------------------------

    def discover(
        self,
        topics: tuple[str, ...] | None = None,
        window_days: int | None = None,
        star_floor: int | None = None,
        languages: tuple[str, ...] = (),
    ) -> list[Repo]:
        """Descubre repos nuevos. UNION + dedupe de una query por topic.

        Sin OR en GitHub: `topic:ai OR topic:llm` → 422. Por eso una query por
        topic. `stars:>=N` es obligatorio para que un repo nuevo con pocas
        estrellas no quede por debajo del tope de 1000 de `sort=stars`.
        """
        topics = topics or self.s.topics
        window = window_days or self.s.window_days
        floor = self.s.star_floor if star_floor is None else star_floor
        since = (utcnow() - timedelta(days=window)).date().isoformat()

        found: dict[str, Repo] = {}
        for topic in topics:
            q = f"topic:{topic} created:>={since} stars:>={floor} archived:false"
            for lang in languages:
                q += f" language:{lang}"
            try:
                items = self._search(q)
            except GitHubError as exc:
                self.stats["errors"] += 1
                print(f"  ! búsqueda '{topic}' falló: {exc}")
                continue
            self.stats["search"] += len(items)
            for it in items:
                repo = _search_item_to_repo(it)
                if repo.full_name:
                    found[repo.full_name] = repo  # dedupe por nombre

        return sorted(found.values(), key=lambda r: r.stars, reverse=True)

    def _search(self, q: str) -> list[dict[str, Any]]:
        """Pagina una búsqueda. Sin ETag: GitHub no lo manda aquí."""
        out: list[dict[str, Any]] = []
        for page in range(1, self.s.pages + 1):
            url = (
                f"{API}/search/repositories?q={_quote(q)}"
                f"&sort=stars&order=desc&per_page={self.s.per_page}&page={page}"
            )
            status, data, _ = self._request(url, use_etag=False, tries=2)
            if status != 200:
                break
            items = data.get("items") or []
            out.extend(items)
            if len(items) < self.s.per_page:
                break
        return out

    # -- GraphQL (enriquecimiento) ----------------------------------------

    def enrich(self, names: list[str]) -> dict[str, Repo]:
        """Rellena watchers/topics/language de hasta 100 repos en una llamada.

        Es la única forma barata de obtener `watchers` (necesario para el
        score de autenticidad) y los topics de verdad, que la búsqueda no
        devuelve. Coste: 1 punto de cuota por llamada.
        """
        out: dict[str, Repo] = {}
        pending = list(names)
        # El límite de coste de GraphQL depende del tamaño de la query, no del
        # número de nodos: con lotes grandes GitHub responde
        # "Resource limits for this query exceeded" y no devuelve datos. Medido
        # el 2026-10-04: lotes de 100 fallaban de forma intermitente, de ahí el
        # bisectado en vez de un tamaño fijo.
        for chunk in _chunked(pending, self.s.graphql_batch):
            got = self._gql_chunk(chunk)
            if len(got) < len(chunk):
                # Bisecar: el culpable es el tamaño, no los repos.
                half = len(chunk) // 2
                if half < 2:
                    # Con 1 nodo sigue fallando: es ese repo, no el lote.
                    self.stats["errors"] += 1
                    continue
                self.stats["gql_bisect"] += 1
                out.update(self._gql_chunk(chunk[:half]))
                out.update(self._gql_chunk(chunk[half:]))
            else:
                out.update(got)
        return out

    def _gql_chunk(self, chunk: list[str]) -> dict[str, Repo]:
        """Un lote de GraphQL. Devuelve {} si falla: el llamador reintenta."""
        query = "query { "
        for i, name in enumerate(chunk):
            query += f'r{i}: repository(owner: "{_owner(name)}", name: "{_repo(name)}") '
            query += "{ " + _GQL_REPO_FIELDS + " } "
        query += "}"

        try:
            resp = self._gql_post(query)
        except GitHubError as exc:
            self.stats["errors"] += 1
            print(f"  ! graphql falló en lote de {len(chunk)}: {exc}")
            return {}
        self.stats["graphql"] += 1

        got: dict[str, Repo] = {}
        for node in (resp or {}).values():
            if not node:
                continue
            full = node.get("nameWithOwner")
            if not full:
                continue
            got[full] = _gql_to_repo(node)
        return got

    def _gql_post(self, query: str) -> dict[str, Any]:
        import httpx

        with httpx.Client(timeout=self.s.timeout) as client:
            r = client.post(
                GRAPHQL,
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"query": query},
            )
        self._update_rate(r.headers)
        if r.status_code != 200:
            raise GitHubError(f"GraphQL HTTP {r.status_code}: {r.text[:200]}")
        payload = r.json()
        if payload.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in payload["errors"][:3])
            raise GitHubError(f"GraphQL errores: {msgs}")
        return payload.get("data") or {}

    # -- REST individual (ETag, sin coste si no cambia) -------------------

    def repo_detail(self, full_name: str) -> Repo | None:
        """Detalle de un repo vía core REST, cacheado con ETag.

        Un 304 significa "sin cambios" y no consume cuota; se devuelve el
        objeto vacío porque con 304 GitHub no manda cuerpo. Para el radar esto
        sólo se usa como fuente de respaldo cuando GraphQL no pudo.
        """
        url = f"{API}/repos/{full_name}"
        status, data, _ = self._request(url, use_etag=True, tries=2)
        self.stats["rest"] += 1
        if status == 404:
            return None
        if status == 304:
            return None  # sin cambios y sin cuerpo: nada que actualizar
        return _search_item_to_repo(data)


def _gql_to_repo(node: dict[str, Any]) -> Repo:
    """Nodo GraphQL → :class:`Repo`, con watchers y topics de verdad."""
    topics = node.get("repositoryTopics") or {}
    topic_names = tuple(
        t["topic"]["name"] for t in (topics.get("nodes") or []) if t and t.get("topic")
    )
    lang = (node.get("primaryLanguage") or {}).get("name")
    lic = (node.get("licenseInfo") or {}).get("spdxId")
    return Repo(
        full_name=node.get("nameWithOwner", ""),
        stars=int(node.get("stargazerCount") or 0),
        forks=int(node.get("forkCount") or 0),
        watchers=int(((node.get("watchers") or {}).get("totalCount")) or 0),
        open_issues=int(((node.get("openGraphqlIssues") or {}).get("totalCount")) or 0),
        description=node.get("description"),
        language=lang,
        license=lic,
        homepage=node.get("homepageUrl") or None,
        topics=topic_names,
        created_at=parse_dt(node.get("createdAt")),
        pushed_at=parse_dt(node.get("pushedAt")),
        is_fork=bool(node.get("isFork")),
        is_archived=bool(node.get("isArchived")),
    )


def _owner(full_name: str) -> str:
    return full_name.split("/", 1)[0] if "/" in full_name else full_name


def _repo(full_name: str) -> str:
    return full_name.split("/", 1)[1] if "/" in full_name else full_name


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def _chunked(items: list[str], size: int) -> list[list[str]]:
    """Parte una lista en trozos de `size` (mínimo 1)."""
    n = max(1, size)
    return [items[i : i + n] for i in range(0, len(items), n)]
