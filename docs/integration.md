# Integración: Hermes, MCP, cron y Atlas

starradar no es sólo un script: es la pieza que hace que el resto del setup de
Hermes deje de adivinar qué repos existen y empiece a tenerlas medidas.

---

## 1. Como servidor MCP (todos los perfiles, de golpe)

Es la vía principal. Un solo servidor MCP registrado en la config de Hermes
queda disponible para **todos los perfiles** (web-dev, y los que se ajouten),
así que no hay que instalar nada por perfil.

En `config.yaml` del perfil:

```yaml
mcp_servers:
  starradar:
    command: starradar
    args: ["mcp"]
    env:
      STARRADAR_HOME: "~/.starradar"
      STARRADAR_TOKEN: "${STARRADAR_GH_TOKEN}"
```

O, si el token se lee del entorno del proceso y se prefiere no escribirlo en la
config, `STARRADAR_TOKEN` se hereda tal cual y la entrada queda sin `env`.

### Herramientas expuestas

| Herramienta | Qué hace | ¿Red? |
| ----------- | -------- | ----- |
| `radar_scan` | Ciclo completo: descubre, puntúa, guarda, devuelve el digest. | sí |
| `radar_top` | Último ranking guardado. **Instantáneo.** | no |
| `radar_explain` | Por qué puntúa así un repo, con cifras. | a veces |
| `radar_moves` | Qué ha más crecido desde el último snapshot. | no |
| `radar_status` | Snapshots, si la velocidad es medida, umbrales. | no |

Todas devuelven JSON en texto, y **los errores vuelven como resultado con
`isError: true`**, no como excepción de socket. Un agente puede leer el error y
reintentar con otros parámetros; una sesión MCP rota no se recupera.

### Por qué `radar_top` existe

Es la ruta que un agente debería usar por defecto: lee SQLite y devuelve en
milisegundos. `radar_scan` es para cuando hace falta descubrir algo nuevo. La
distinción importa porque un agente que espera tres segundos por una lista de
veinte filas deja de usarla en dos días.

---

## 2. Digest diario por cron

```bash
starradar scan --json > /tmp/starradar.json
```

Con la salida JSON, el trabajo del agente es resumir, no recalcular. El prompt
del job debe ser explícito sobre eso: si le pides "analiza", el agente
reimplementará el scoring y perderás el filtro de autenticidad.

Programación sugerida: **una vez al día**. El cuello de botella no es la cuota
(un scan son ~10 llamadas) sino el valor: medir velocidad con menos de 24 h de
separación entre snapshots no añade información real.

La velocidad sólo pasa de `estimada` a `medida` en la **segunda** ejecución. El
primer digest debe decirlo, y `run_scan` lo pone en `stats.velocity_source` para
que no haya forma de olvidarlo.

---

## 3. Señales Atlas

`atlas.py` convierte el digest verificado en el esquema que idea-lab ya consume,
de modo que una señal de starradar entra en el pipeline sin traducción.

```bash
starradar signals --out ~/workspaces/ideas/atlas/
```

Reglas de la conversión:

- **Sólo la capa `verified`.** Un repo sin crecimiento demostrado no genera
  señal. Atlas convierte señales en tesis, y una granja de estrellas
  convertida en tesis es peor que un hueco.
- **`date_inferred: false` siempre.** La fecha es la de la medición, no una
  suposición.
- **`momentum_by_theme` se deja vacío.** starradar mide momentum de *repos*, no
  de temas. No debe inventar ese agrupamiento: es trabajo de Atlas.
- Cada señal lleva un bloque `measured` con las cifras crudas, para que quien
  la lea pueda auditarla sin volver a GitHub.

Esto cierra el hueco que el propio `not_verified` de Atlas declaraba: las
cifras de un repo ahora vienen de una medición, no de un briefing.

---

## 4. Los cuatro estados, y por qué importan

Un radar se usa en modo desatendido, y en desatendido no hay nadie mirando
para interpretar un error. Los cuatro estados tienen que ser distinguibles por
el texto, no sólo por el color:

| Estado | Salida |
| ------ | ------ |
| vacío | "Sin digest todavía. Ejecuta `starradar scan`." + el motivo en `empty` |
| velocidad estimada | Aviso explícito: "la velocidad aún es estimada: mañana habrá snapshots reales" |
| error de GitHub | El error real + se continúa con los datos guardados. No se aborta. |
| éxito | Digest con las dos capas y la cuota consumida |

El de error es el más importante: si GitHub falla a las 3 de la mañana, el
digest de las 3 de la mañana debería seguir siendo útil.

---

## 5. Seguridad

- El token se lee de `STARRADAR_TOKEN` y **nunca** se escribe en la base ni se
  imprime. Los repos guardados son públicos; el token no se persiste.
- El dashboard escucha en `127.0.0.1` por defecto. No expone datos a la red
  local sin que alguien lo pida con `--host`.
- El servidor MCP habla por stdio: no abre ningún puerto.
- `tests/test_guards.py` falla si un patrón `ghp_...` aparece en cualquier
  fichero del repo. Es una red de seguridad barata contra un accidente.
