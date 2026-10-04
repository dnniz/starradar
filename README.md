# starradar

> Radar de repos de GitHub **nuevos** cuyas estrellas están subiendo de verdad.

No responde a "¿qué repos tienen más estrellas?". Responde a la pregunta que
importa: **¿qué acaba de aparecer y está ganando atención real, con números que
he medido yo?**

---

## El problema que resuelve

Buscar repos por estrellas tiene dos trampas, y ambas se pagan caras si no las
tratas:

**1. Las granjas de estrellas.** Hay repos que compran o inyectan estrellas para
aparecer en cualquier "top de la semana". Un radar ingenuo los pone primeros, y
si un agente los lee se mete en un proyecto que no existe.

**2. "Popular" no es "nuevo".** Ordenar por estrellas absolutas devuelve
proyectos con años de trayectoria. Lo interesante es lo que está *subiendo* ahora,
y eso sólo se sabe mirando la **pendiente**, no el total.

starradar resuelve las dos: mide velocidad contra snapshots históricos, y filtra
por autenticidad con un ratio validado empíricamente (`docs/scoring.md`).

---

## Qué lo hace distinto

- **Dos capas, nunca mezcladas.** El ranking verificado y los "sin verificar"
  viven en secciones separadas. Un repo sin crecimiento demostrado nunca se
  presenta como señal, pero tampoco se esconde: se muestra con el motivo.
- **Explicable por repo.** `starradar explain owner/repo` dice por qué puntúa
  así, con las cifras. Un número sin explicación no es auditable.
- **Sin stars/día inventados.** El primer día la velocidad es una *estimación*
  marcada como tal; a partir del segundo snapshot es *medida*. El digest dice
  siempre cuál de las dos estás viendo (`velocity_source`).
- **Cero dependencias obligatorias.** Sólo `httpx`. El dashboard es
  `http.server` de la librería estándar. Arranca en milisegundos.

---

## Instalación

```bash
git clone https://github.com/dnniz/starradar.git
cd starradar
pip install -e .          # o: pip install -e ".[dev]" para desarrollar
```

## Configuración

Sólo hace falta un token de GitHub con permiso de lectura de repos públicos
(un PAT fine-grained sin scopes funciona para el API público):

```bash
export STARRADAR_TOKEN=ghp_xxx    # opcional: sin token, sólo funciona offline
export STARRADAR_HOME=~/.starradar   # dónde vive el SQLite (por defecto)
export STARRADAR_LIVE=0              # 0 = modo offline, recalcula sin red
```

## Uso en 30 segundos

```bash
starradar scan                    # descubre, puntúa y guarda un ciclo
starradar explain facebook/react  # por qué puntúa así, con cifras
starradar moves --hours 24        # qué más ha crecido desde el último snapshot
starradar serve                   # dashboard web en http://127.0.0.1:8787
starradar signals --out atlas/    # emite señales Atlas desde el digest
```

La primera ejecución marca la velocidad como `estimada`. Ejecuta `scan` en días
distintos y a partir del segundo día la velocidad es real.

---

## Las tres capas de confianza

| Capa           | Qué significa                                                                 |
| -------------- | ----------------------------------------------------------------------------- |
| verificado     | w/★ sobre el suelo **y** score suficiente: crecimiento demostrado.             |
| sin verificar  | pasa autenticidad pero el score aún es bajo, o está por debajo del suelo.      |
| (oculto)       | forks, archivados, sin licencia: ruido, no se listan.                          |

El detalle completo, con la justificación empírica de cada umbral, está en
**[`docs/scoring.md`](scoring.md)**.

## Documentación

| Documento | Qué contiene |
| --------- | ------------ |
| [`docs/architecture.md`](architecture.md) | Decisiones de diseño, alternativas descartadas y por qué. |
| [`docs/scoring.md`](scoring.md) | El modelo de puntuación y la evidencia medida que lo sustenta. |
| [`docs/github-api.md`](github-api.md) | Hechos verificados sobre la API de GitHub (sus trampas). |
| [`docs/integration.md`](integration.md) | Hermes, MCP, cron, Atlas: cómo encaja con el resto. |
| [`docs/operations.md`](operations.md) | Operación diaria, calibración, qué hacer si el ranking va mal. |
| [`docs/adr/`](adr/) | Decisiones con contexto, alternativas y consecuencias. |

---

## Licencia

MIT. Ver [LICENSE](LICENSE).
