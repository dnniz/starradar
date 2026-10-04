# Hechos verificados sobre la API de GitHub (2026-10-04)

Medidos con un PAT fine-grained del usuario `dnniz` (5000 req/h core,
30 req/min search). **No es documentación: es comportamiento medido**, y
varios puntos contradicen lo que se asume normalmente. Todo lo que starradar
hace de forma distinta sale de aquí.

Reproducible con los scripts de `spike/` del repo.

---

## 1. La búsqueda no tiene ETag: 304 no existe ahí

`/search/repositories` **no devuelve cabecera `ETag`**. Una petición condicional
con `If-None-Match` devuelve 200 con el cuerpo entero otra vez. **Conclusión:
no se puede cachear la búsqueda por ETag, y cada llamada gasta cuota de
search.**

En cambio, el **core REST sí cachea**: `GET /repos/{owner}/{repo}` con
`If-None-Match` devuelve **304** y no cuenta contra el límite de 5000/h.

Por eso el diseño es: *descubrir* con búsqueda (barato en llamadas, caro en
cuota, no cacheable) y *enriquecer* con GraphQL en lotes (una llamada para 100
repos). El ETag se reserva para `repo_detail()`, la ruta de consulta puntual.

## 2. La búsqueda NO soporta operadores `OR` entre filtros

`topic:ai OR topic:llm` devuelve **HTTP 422**. Tampoco `OR` entre lenguajes.
starradar lo resuelve **en cliente**: una query por topic y una unión con
deduplicación por `owner/repo`. Más llamadas, pero correcto.

## 3. La búsqueda tiene el tope de 1000 resultados

Con `sort=stars` (o `sort=updated`), la API devuelve como máximo 1000 ítems
aunque `total_count` diga millions. El ranking "top nuevo por estrellas" está
topado: para un topic enorme, lo interesante (crecimiento reciente) puede caer
fuera de esa ventana.

**Mitigación**: starradar usa una ventana de creación reciente
(`created:>=`) y varios topics, que reduce el espacio de búsqueda muy por debajo
del tope. Aun así, es un techo conocido: no hay paginación que lo rompa.

## 4. La búsqueda NO trae `subscribers_count`

El campo de watchers **no viene en los resultados de búsqueda**. Sólo aparece
en el detalle del repo (REST) o en GraphQL. Como el score de autenticidad se
basa en watchers/estrellas, **sin enriquecer la búsqueda no se puede puntuar**.
Ése es el motivo de existir del paso de enriquecimiento: sin él, el ranking sería
una lista de estrellas sin verificar.

## 5. La búsqueda GraphQL tiene campos que la matan

Medido, campo a campo:

| Campo añadido a la búsqueda         | Resultado      |
| ----------------------------------- | -------------- |
| `stargazerCount` solo                 | funciona       |
| `forkCount` solo                      | funciona       |
| `stargazerCount` + `forkCount`       | **0 resultados** |
| `issues { totalCount }`              | **0 resultados** |

Añadir dos contadores juntos, o la conexión `issues`, hace que la búsqueda
devuelva cero sin error explícito. Es una trampa con síntoma engañoso: un
`total_count: 0` silencioso parece "no hay repos", no "mi query está mal".

**Solución en starradar**: la búsqueda pide campos mínimos (`nameWithOwner`,
`url`) y todo el detalle se pide en la llamada GraphQL separada de enriquecimiento.
Las dos consultas nunca mezclan búsqueda con contadores pesados.

## 6. `fullName` no existe en GraphQL; es `nameWithOwner`

En el tipo `Repository` de GraphQL el campo es `nameWithOwner`. Pedir
`fullName` devuelve error de esquema. Detalle menor que costó una iteración de
spike.

---

## Límites de cuota (con token fine-grained)

| Recurso                    | Límite        |
| -------------------------- | ------------- |
| core REST                  | 5000 req/h    |
| search REST                | 30 req/min    |
| GraphQL                    | 5000 puntos/h |

El coste real de un `starradar scan` típico (8 topics, ventana 45 días):

- ~8 llamadas de búsqueda (8 de 30/min: sin problema).
- 1 llamada GraphQL por cada 100 repos.
- El resto, core REST con ETag (casi gratis gracias al 304).

Muy por debajo de cualquier límite. El cuello de botella sería la *cadencia*,
no la cuota: por eso el digest es diario y no horario.

## Autenticación

- PAT fine-grained **sin scopes** sirve para todo esto (sólo lectura de repos
  públicos). No hace falta `public_repo` ni nada de escritura.
- Sin token, `starradar` funciona en modo **offline**: recalcula el scoring y el
  digest con lo que ya está en la base, sin tocar la red. Útil para probar y
  para los estados vacío/error.
