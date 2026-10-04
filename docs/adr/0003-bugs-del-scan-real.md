# ADR 0003 — Bugs encontrados por el primer scan real, y qué hicieron

- **Estado**: aceptado
- **Fecha**: 2026-10-04
- **Contexto**: primera ejecución completa de `starradar scan` contra la API de
  GitHub, después de ~95 tests verdes con fixtures sintéticos.

## Por qué existe este documento

Los tests con fixtures pasaron 100% y el producto estaba roto. Ninguno de los
tres bugs de abajo era hipotético: los tres ocurrieron en producción, en la
primera pasada, sobre 567 repos reales. Se documentan porque el patrón —*un
test que inventa el payload no valida el contrato con la API*— es la lección
que nos llevamos, y porque cualquiera que retome el proyecto va a tropezar con
los mismos campos de GitHub.

## Bug 1 (crítico): `watchers_count` no es el número de watchers

**Síntoma**: 539 de 567 repos caían en la capa "sin verificar" con
`w/★=0.0000 por debajo del suelo`. El filtro de autenticidad, que es la pieza
central del producto, estaba invertido: marcaba como sospechoso a casi todo.

**Causa**: en la API de GitHub, `watchers_count` es un **alias de
`stargazers_count`** (legacy, conserva el nombre viejo del campo). El campo
que cuenta watchers de verdad es `subscribers_count`. Medido sobre
`jev-chat/jev-chat-jarvis`:

```
stargazers_count   : 7319
watchers_count     : 7319   ← idéntico: es el mismo número con otro nombre
subscribers_count  : 10     ← los watchers reales
```

El parser leía `watchers_count` con un `.get(..., 0)`, así que con datos de
búsqueda (donde sólo viene `watchers_count`) obtenía 7319, no 10.

**Arreglo**: leer `subscribers_count`; si no viene, dejar 0 y marcarlo como
"sin dato", nunca como un valor inventado. El parser REST quedó con la
trampa documentada en su docstring y con un test que reproduce el payload
crudo real (`test_repo_detail_uses_subscribers_count_not_watchers_count`).

**Efecto medido**: de 67/567 enriquecidos a 567/567, y de 28 a 197 repos
verificados en la siguiente pasada.

## Bug 2: los lotes de GraphQL de 100 saturan el límite de coste

**Síntoma**: sólo 67 de 567 repos volvieron enriquecidos; los otros 500 se
quedaron sin watchers, y por tanto sin señal de autenticidad.

**Causa**: el coste de una query de GraphQL depende del tamaño del batch, y con
100 repos GitHub devuelve `Resource limits for this query exceeded` sin más
detalle. El cliente devolvía `{}` para el lote entero y no decía nada.

**Arreglo**: lotes de 50 (`Settings.graphql_batch`) y, si un lote falla,
bisectado recursivo hasta aislar el repo culpable. Con un solo nodo malo se
rinde y lo reporta; con 500 buenos, un fallo ya no tira el trabajo de medio
ranking.

## Bug 3: confundir "sin dato" con "dato malo"

**Síntoma**: un repo con 5.000★ y watchers desconocidos salía con
`w/★=0.0000, por debajo del suelo` — es decir, acusado de compra de estrellas.

**Causa**: el scoring trataba `ratio is None` (repo sin estrellas) como
"desconocido", pero no `watchers == 0` con estrellas > 0. Para el scoring,
0 watchers y 0 estrellas son el mismo número, y ambos caían en la banda de
"no demostrado".

**Arreglo**: tres estados, no dos.

| Estado | Autenticidad | ¿Verificado? | Razón |
|---|---|---|---|
| ratio conocido, ≥ suelo | 45–100 | sí | señal positiva |
| ratio conocido, < suelo | 0–45 | no | por debajo del suelo, suspicion explícita |
| **sin watchers (0)** | **50 neutro** | **no** | **"no lo sé", no "sospechoso"** |

El tercer estado es el que faltaba, y es el que importa: un 0 de watchers
cuando las estrellas son 0 es un dato real ("nadie mira este repo"), pero un 0
cuando las estrellas son miles casi siempre es un fallo de enriquecimiento.
El scoring no puede distinguirlos, así que no debe acusar en ninguno de los dos
casos.

**Consecuencia real, y es un coste aceptable**: en la pasada del 2026-10-04,
**290 de 567 repos** tienen `subscribers_count: 0` legítimo (verificado contra
la API: `Edwardxlai/easyread` ★731 sub=0, `qiz029/dscode` ★1016 sub=0). Son
repos de una tarde, sin comunidad formada. Al no haber señal de watchers, caen
en la capa "sin verificar" — correctamente, porque **no hay nada que
reconocer**. La alternativa (inventar un ratio) sería mentir en la dirección
contraria: hacer pasar por sospechoso algo que simplemente es nuevo.

Es un coste de la honestidad del modelo, y es el motivo por el que la segunda
capa existe.

## Lección general

95 tests con fixtures pasan y no dicen nada sobre si el contrato con la API es
correcto. Lo que sí lo dice:

1. `tests/test_live_github.py` — tests marcados `live` que hablan con GitHub de
   verdad, con aserciones sobre *qué campos existen*, no sobre *qué devolvemos*.
2. `tests/test_regressions.py` — los tres bugs de este documento, cada uno
   anclado al payload crudo que los provocó.
3. El scan real como criterio de aceptación antes de publicar.

La regla que nos queda: **un número que no hemos visto salir de la API no tiene
default**. Ni el ratio de watchers, ni la velocidad, ni el score. Si no lo
hemos medido, se declara estimado; si no se puede estimar, se declara
desconocido.
