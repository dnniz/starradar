# Scoring: qué es "hot" y cómo se demuestra

Este documento es la defensa de cada número del sistema. Si cambias un umbral,
cámbialo aquí y anota por qué. Un score sin evidencia medida es una opinión con
formato de métrica, y es exactamente el tipo de cosa que este proyecto existe
para filtrar.

Todas las cifras de abajo son **medidas en la API de GitHub el 2026-10-04**
con el token de lectura del autor. Reproducibles con `starradar calibrate`.

---

## 1. El hallazgo que cambió el diseño

La intuición habitual —y la que circulaba en el pipeline de idea-lab antes de
este proyecto— es que **el ratio forks/estrellas detecta estrellas compradas**.
Se midió, y **es falso**:

| Repo                                | ★      | forks | f/★    |
| ----------------------------------- | ------ | ----- | ------ |
| `denoland/deno`                     | 105,8k | 6,2k  | 0.0589 |
| (mediana de 10 establecidos)        |        |       | 0.2050 |
| (pool "nuevo" sospechoso)           |        |       | 0.3050 |

`deno` tiene el **ratio forks/estrellas más bajo** de todo el grupo de
establecidos, y sus forks son absolutamente reales. El pool sospechoso, en
cambio, marcaba *más alto* que la mediana de los proyectos buenos. El ratio
forks/estrellas confunde "mucho interés" con "estrellas falsas", y castiga justo
a los proyectos técnicos más populares. **Descartado como detector.**

### El sustituto: watchers/estrellas

Los *watchers* (subscribers) no se compran: cada uno es una suscripción real a
notificaciones, cuesta un clic y deja rastro. Si 5.000★ producen 20 watchers,
algo no va con esas estrellas.

| Pool                          | mediana w/★ | rango                |
| ----------------------------- | ----------- | -------------------- |
| 10 proyectos establecidos     | 0.0240      | 0.0075 – 0.0337      |
| 24 repos del pool "nuevo"     | 0.0278      | 0.0042 – 0.0723      |

Las distribuciones **se solapan**, lo cual es buena noticia: significa que la
mayoría de los repos nuevos son legítimos. También significa que w/★ solo no
basta, y por eso se combina con velocidad y momento (sección 4).

Umbrales derivados de esa medición (`src/starradar/config.py`):

| Constante             | Valor  | Lectura                                                        |
| --------------------- | ------ | -------------------------------------------------------------- |
| `AUTHENTICITY_FLOOR`  | 0.0075 | Por debajo: "no demostrado" (≈ el mínimo de los establecidos).   |
| `AUTHENTICITY_STRONG` | 0.015  | Por encima: "orgánico confirmado" (≈ 0.6x la mediana).          |

El suelo es deliberadamente generoso: **no pretende acusar de fraude**, sino
separar "crecimiento no demostrado" de "crecimiento normal". El lenguaje del
código y de la UI es "no demostrado" / "orgánico confirmado", nunca "falso" o
"fraude", porque no hay evidencia para esas palabras.

### Los tres estados de la autenticidad

La autenticidad tiene **tres** salidas, no dos. El tercer estado es el que
costó un bug de producción entero ([ADR 0003](adr/0003-bugs-del-scan-real.md)):

| Estado                             | `w/★` | Componente | Verificado | Por qué |
| ---------------------------------- | ----- | ---------- | ---------- | ------- |
| Orgánico confirmado                | ≥ 0.0075 | 45–100 | sí | señal positiva medida |
| Por debajo del suelo                | 0 < r < 0.0075 | 0–45 | no | sospechoso, con la cifra a la vista |
| **Sin dato de watchers**            | — (0) | **50 neutro** | **no** | **"no lo sé" ≠ "sospechoso"** |

Un `watchers == 0` con estrellas > 0 devuelve un componente **neutro**, no
cero. La razón: si el enriquecimiento falló, el ratio sale 0.0 y caería en la
banda de "por debajo del suelo", que *acusa*. Acusar de compra de estrellas a
algo que sólo no sabemos es peor que no ranking: en la primera pasada real
marcó así a 539 de 567 repos y dejó el producto inservible.

El coste de ser honesto: en la pasada del 2026-10-04, 290 de 567 repos tienen
`subscribers_count: 0` **legítimo** (verificado contra la API). Son repos de
una tarde, sin comunidad todavía. Sin señal de watchers caen en la segunda
capa, que es exactamente donde deben estar: no hay nada que reconocer.

---

## 2. Velocidad (peso 0.45)

`estrellas por día`, que es lo que convierte un total en una oportunidad.

**Medida contra snapshots.** Si hay historial, se usa el delta entre el primer
y el último snapshot de la ventana. Si no hay historial (primer día), se estima
como `estrellas / edad_días`.

Esa estimación **subestima** los repos que nacieron con estrellas (importados, o
creados dentro de un entorno ya popular). Por eso el digest siempre declara
`velocity_source`:

- `medida (snapshots)` → el número es real.
- `estimada (sin histórico)` → el número es una cota inferior, y la UI lo avisa.

No se mezcla nunca una con otra sin decirlo. Es la diferencia entre un radar y
una bola de cristal.

La escala es `log1p` (≈17★/d→20, ≈100★/d→45, ≈1000★/d→69) comprimida contra
`VELOCITY_CAP`: linealmente, 10× más velocidad no puede hacer que un repo
duplique su score y rebase el ranking entero.

---

## 3. Momento (peso 0.15)

Un repo que ganó 2.000★ el mes pasado y lleva seis semanas sin push no es una
oportunidad: es un cadáver con métricas. Momentum puntúa:

- **Recencia del push** (curva suave: 1 día ≈ tope, 60 días ≈ 0).
- **Issues abiertas como proxy de uso**: muchas issues y ninguna contribución
  sugiere un proyecto sin manos. Pocas issues en un repo nuevo es normal y no
  penaliza.
- **Sin licencia y forks archivados** se filtran antes de puntuar, no aquí.

---

## 4. Composición

```
total = 0.45 · velocidad + 0.35 · autenticidad + 0.15 · momento
```

El peso de velocidad es el más alto a propósito: es la señal más difícil de
falsificar, porque requiere historial real que un comprador de estrellas no
puede fabricar hacia atrás.

**Dos capas de salida**, porque una puntuación única mezcla cosas que no deben
mezclarse:

- `verificado` = `autenticidad ≥ 0` (w/★ sobre el suelo) **y** `total ≥
  VERIFIED_MIN`.
- `sin verificar` = todo lo demás que aún no se ha filtrado del radar.

Con `VERIFIED_MIN = 45`, un repo de 40 puntos no sale como verificado aunque
autentique bien: score bajo significa "todavía no hay señal suficiente". Se
muestra en la segunda capa, con el motivo, no se esconde.

**Veredicto** (sólo presentación, no cambia el orden):

| Rango  | Veredicto   | Lectura                                        |
| ------ | ----------- | ---------------------------------------------- |
| ≥ 70   | destacado   |crecimiento fuerte y orgánico                   |
| ≥ 55   | sólido      |merece atención                                 |
| ≥ 45   | temprano    |señal incipiente, vigílalo                       |
| < 45   | sin verificar| no hay evidencia suficiente todavía            |

---

## 5. Qué NO hace este scoring

Documentar los límites es tan importante como documentar lo que funciona.

- **No detecta estrellas compradas en repos ya establecidos.** El suelo está
  calibrado para repos *nuevos*; aplicarlo a proyectos maduros mezcla
  distribuciones distintas.
- **No mide retención.** Que alguien startee no significa que vuelva. Sólo se
  ve el push, que es un proxy imperfecto.
- **La primera ejecución no puede medir velocidad.** Es una limitación real, no
  un bug: se resuelve con un snapshot al día, y el sistema lo dice en vez de
  fingir.
- **Los umbrales derivan de una muestra de ~110 repos.** Es suficiente para
  separarlos, no para ser una verdad universal. `starradar calibrate` permite
  recalibrar contra la distribución real cuando la muestra crezca.

---

## 6. Reproducir la medición

```bash
starradar scan --window 45 --floor 40   # rellena la base
starradar calibrate                     # distribución w/★ actual
starradar explain owner/repo            # el desglose de un repo concreto
```

Si `calibrate` muestra que la mayoría de tus repos caen por debajo del suelo,
el suelo está demasiado alto para tu distribución actual y hay que ajustarlo en
`config.py`, documentando el motivo aquí.
