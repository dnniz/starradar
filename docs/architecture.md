# Arquitectura

## El problema de partida

El pipeline de idea-lab tenía una fuente de descubrimiento de repos que
devolvía **cero resultados**. Se parcheó con una búsqueda por topics suelta.
Eso deja tres fallos estructurales:

1. **Nadie sabe por qué un repo aparece.** Sin trazabilidad, no hay forma de
   detectar una fuente muerta hasta que el digest sale vacío.
2. **No hay histórico.** Impossible saber si algo está *subiendo*; sólo qué
   tiene más estrellas ahora mismo.
3. **Las cifras no se revalidan.** El propio fichero de señales de Atlas
   registra en su campo `not_verified` que las estrellas, forks y días
   *"no se revalidaron contra GitHub en esta sesión"*.

starradar es la respuesta a los tres: cada dato viene de la API con su fuente,
cada ciclo deja un snapshot, y la capa verificada exige haber medido el repo.

---

## Forma del sistema

```
      ┌──────────────┐
      │   GitHub API │  search (descubrir) · graphql (enriquecer) · rest (detalle)
      └──────┬───────┘
             │  github.py — ETag en core REST, lotes de 100 en GraphQL,
             │               retry con backoff, todo log a stderr
      ┌──────▼───────┐
      │  pipeline.py │  descubrir → enriquecer → guardar → puntuar → digerir
      └───┬──────┬───┘
          │      │
   ┌──────▼──┐ ┌─▼──────────────┐
   │ store.py│ │  scoring.py    │  puro, sin red ni disco
   │ SQLite  │ │  velocity      │
   │ repos + │ │  authenticity  │──► dos capas: verified | unverified
   │ snaps   │ │  momentum      │
   └────┬────┘ └─┬──────────────┘
        │        │
   ┌────▼────────▼─────┐      ┌──────────────┐
   │  cuatro salidas   │      │  scoring.py  │
   │ CLI (humana)      │      │  thresholds  │
   │ MCP (agentes)     │      │  en config   │
   │ dashboard (web)   │      └──────────────┘
   │ atlas.py (señales)│
   └───────────────────┘
```

**Una sola fuente de verdad** para la lógica: `scoring.py` no sabe nada de
GitHub, y `github.py` no sabe nada de scoring. Eso permite testear el scoring
entero sin token, y cambiar los umbrales sin tocar la red.

---

## Decisiones y alternativas descartadas

### Scoring puro, sin I/O

*Alternativa descartada*: puntuar dentro del cliente de GitHub, con la respuesta
a mano. Más rápido de escribir, pero hace que los errores de scoring sólo se
detecten con un token real, en producción, con datos que cambian. Separado, el
scoring se testea entero sin red: 31 tests que corren en milisegundos.

### SQLite, no ficheros JSON

*Alternativa descartada*: un `digests.json` y ya. Se descartó al necesitar
(1) deduplicar por `owner/repo`, (2) guardar un snapshot por repo y día,
(3) consultar "qué ha subido desde el último snapshot". Eso es un `INSERT OR
REPLACE` con índice. JSON obligaría a reescribir el fichero entero en cada
ciclo y a cargar todo para responder una pregunta.

*Alternativa descartada*: Postgres o Redis. Un radar personal de ~500 repos
no justifica un servidor. SQLite en WAL con `busy_timeout` aguanta el caso real
(ejecutar por cron mientras se mira el dashboard).

### Enriquecer con GraphQL, no con REST por repo

*Alternativa descartada*: `GET /repos/{o}/{r}` por cada repo. Son 1 llamada por
repo; para 200 repos son 200 llamadas. GraphQL mete 100 repos por llamada, y de
paso trae watchers, licencia y topics en la misma respuesta. La búsqueda
devuelve justo lo que falta (los watchers), y eso no es casualidad: por eso el
paso existe.

### Cero dependencias obligatorias

Sólo `httpx`. El dashboard es `http.server`, el servidor MCP es JSON-RPC a
mano, el render de la CLI es ANSI directo.

*Alternativa descartada*: FastAPI + uvicorn + rich + typer + el SDK de `mcp`.
Serían cuatro dependencias para un programa que se instala en una máquina y
arranca. La contrapartida es escribir el JSON-RPC a mano (~200 líneas, y está
testeado), y aceptar que el dashboard no escala a millones de usuarios — cosa
que no va a ocurrir en un radar local.

*Consecuencia asumida*: si en el futuro se quiere un dashboard multiusuario,
habrá que añadir FastAPI. El JSON del digest ya está preparado para eso
(`digest.to_dict()`), así que el cambio no toca la lógica.

### Dos capas, no un score único

*Alternativa descartada*: un único 0-100 con la autenticidad como penalización.
Se descartó porque mezcla dos preguntas distintas ("¿esto crece?" y "¿Estas
estrellas son reales?") en un número, y un repo bien penaltyado sigue pudiendo
entrar arriba. Con dos capas, "sin verificar" **nunca** se confunde con una
señal, pero sigue visible: no se pierde información ni se finge precisión.

*Consecuencia*: el consumidor (agente, digest Atlas) tiene que elegir una capa.
Es explícito a propósito. `digest_to_signals()` sólo lee `verified`.

### Hand-rolled JSON-RPC en vez del SDK `mcp`

*Alternativa descartada*: el SDK oficial. Aporta validación de esquemas y
transporte, a cambio de una dependencia y de un modelo de concurrencia (asyncio)
que no aporta nada aquí: un servidor de cinco herramientas de sólo lectura que
vive lo que vive el proceso.

*Consecuencia asumida*: si el protocolo MCP añade requisitos que no se
implementan, el módulo a sustituir es `_jsonrpc.py` entero; los handlers y los
esquemas no cambian. `mcp_server.py` es deliberadamente una capa fina sobre él.

---

## Estados y su tratamiento

Un radar tiene cuatro estados, y confundirlos es como se pierden usuarios:

| Estado | Qué hace el sistema |
| ------ | ------------------ |
| **Vacío** (sin digest) | La CLI dice qué ejecutar; el MCP devuelve `{"empty": true, "message": ...}`; el dashboard muestra el mismo mensaje. Nunca una lista vacía sin explicación. |
| **Cargando** | El MCP responde cuando termina (es un proceso de vida corta); la CLI muestra el progreso en stdout. |
| **Error** | Se imprime el error real y se **sigue con los datos guardados** en vez de abortar. Un fallo de GitHub no debe dejar al usuario sin nada. |
| **Éxito** | Digest con las dos capas y la cuota consumida, para que se vea el coste. |

Un quinto estado que sí importa: **velocidad estimada** vs **medida**. No es un
estado de UI, es de datos, y se propaga hasta la señal de Atlas.

---

## Rendimiento

- La búsqueda son ~8 llamadas (una por topic); el enriquecimiento, 1 por cada
  100 repos. Un `scan` completo son ~10 llamadas de red.
- El ETag/304 en el core REST hace que las consultas repetidas no gasten cuota.
- El dashboard lee el último digest ya serializado de SQLite: sin red, sin
  recomputar scores. Responde en milisegundos.
- `starradar top` (vía MCP) **no toca la red** por diseño: es la ruta para
  agentes, y un agente que espera 3 s por una lista de 20 filas es un agente
  que deja de usarla.
