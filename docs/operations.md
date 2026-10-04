# Operación diaria

## Rutina

```bash
starradar scan          # el ciclo; ~10 llamadas de red
starradar moves         # qué ha subido desde el último snapshot
starradar explain r/x   # cuando algo del ranking no encaja
```

Cadencia recomendada: **una vez al día**. Con más frecuencia, los snapshots se
pisan (la clave primaria es `(repo, at)`) y la velocidad medida no gana nada.

## Los primeros días

| Día | Qué esperar |
| --- | ----------- |
| 1 | Todo marcado como `estimada`. Los scores ya son válidos, pero la velocidad es una cota inferior. |
| 2 | El histórico empieza a ser real. `tracked_days` sube. |
| 7+ | La velocidad medida es fiable. Aquí es donde el radar empieza a merecer el nombre. |

Si el digest del día 1 te parece raro, es correcto: se está mezclando "total de
estrellas" con "estrellas por día" y el segundo necesita tiempo.

## Calibrar

```bash
starradar calibrate
```

Imprime la distribución actual de watchers/estrellas y cuántos repos caen por
debajo del suelo. Tres lecturas posibles:

- **La mayoría por debajo del suelo**: el suelo está alto para tu distribución.
  Bájalo en `config.py` y anota el motivo en `docs/scoring.md`.
- **Casi ninguno por debajo**: puede que el suelo esté bajo, o que sólo estés
  mirando repos buenos. Normal si los topics son específicos.
- **Un repo legítimo marcado como sospechoso**: mira `starradar explain`. Si el
  caso es legítimo, es un falso positivo del ratio y hay que documentarlo.

## Cuando el ranking va mal

1. **`scan` devuelve 0 repos.** Casi siempre es la búsqueda: comprueba el token
   y prueba `--topics llm` con una ventana amplia. Recordar que `sort=stars`
   topa en 1000 resultados (ver `github-api.md` §3).
2. **Todo sale "sin verificar".** O el suelo está alto para lo que hay ahora, o
   la autenticidad no se está midiendo: mira `stats.n_verified` frente a
   `n_discovered`. Si enriched es 0, el GraphQL está fallando.
3. **Los scores no cambian entre días.** Es normal si sólo hay un snapshot: se
   está reordenando la misma distribución. Necesitas ≥2 días.
4. **Un repo con 30k★ aparece en "sin verificar".** Correcto: un repo enorme
   con pocos watchers sí es sospechoso. `explain` enseña las cifras.

## Copias de seguridad

Todo el estado vive en un SQLite: `$STARRADAR_HOME/starradar.db` (por defecto
`~/.starradar/starradar.db`). Es autocontenido; copiarlo es la copia de
seguridad.

```bash
sqlite3 ~/.starradar/starradar.db ".backup /ruta/starradar-$(date +%F).db"
```

Perderlo no es grave (se reconstruye con un `scan`), pero se pierde el
historial, y el historial es la parte difícil de recuperar.

## Variables

| Variable | Por defecto | Para qué |
| -------- | ----------- | -------- |
| `STARRADAR_TOKEN` | (vacío) | Token de lectura de GitHub. Sin él, sólo modo offline. |
| `STARRADAR_HOME` | `~/.starradar` | Dónde vive el SQLite. |
| `STARRADAR_LIVE` | `1` | `0` = modo offline: recalcula sin red. |
| `NO_COLOR` | (vacío) | Fuerza salida sin ANSI. Ya se fuerza si no es un TTY. |
