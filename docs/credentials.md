# Credenciales de GitHub: cómo trabaja starradar sin intervención

Este documento explica por qué este repositorio se publica solo, y qué
credencial usa para hacerlo. Es la referencia para cambiar de token sin
desmontar nada.

## El problema que motivó el diseño

El 2026-10-04, la primera publicación falló con `403 Resource not accessible
by personal access token`. La causa no era el permiso del token, sino su
**ámbito**:

- El token disponible era un **PAT fine-grained** (`github_pat_…`).
- En un PAT fine-grained, *Repository access* es una **lista explícita de
  repositorios**. `dnniz/starradar` se creó **después** de fijar esa lista, así
  que quedaba fuera.
- Lectura sí funcionaba (los repos de la lista se leían bien), por eso el
  fallo no era evidente: el mismo token leía y no escribía.

Un fine-grained **nunca puede auto-ampliar su ámbito por API**; hay que
editarlo en la interfaz de GitHub. Para una herramienta pensada para crear
repositorios nuevos eso es una trampa recurrente, no un incidente aislado.

## La decisión

Se usa un **PAT clásico** (`ghp_…`) con alcance `repo`. En un PAT clásico el
ámbito no es una lista de repos: es una capacidad sobre tu cuenta. Un repo
creado hace un minuto es escribible sin tocar nada.

Comprobado el 2026-10-04: `PUT` + `DELETE` de un fichero en `dnniz/starradar`
devolvieron 201/200/200, y la limpieza se verificó con un 404 posterior.

## Dónde vive el token

Un solo sitio, el `.env` del perfil de Hermes:

```
GH_TOKEN=ghp_…          # PAT clásico; lo usan gh, git y el MCP
GITHUB_TOKEN=ghp_…      # mismo valor, para herramientas que leen este nombre
GH_TOKEN_RO=github_pat… # fine-grained conservado como credencial de sólo lectura
```

Nunca en el repositorio, nunca en `~/.git-credentials`, nunca en un fichero
de credenciales de git. Si el PAT cambia, se edita **una línea** y todo lo
demás sigue funcionando.

## Cómo empuja git sin que nadie teclee

Hay un credential helper en `~/.hermes/bin/git-credential-env`, registrado
como `credential.helper` global:

```
git config --global credential.helper /home/hermes/.hermes/bin/git-credential-env
```

El helper **no guarda nada**: en cada `get` lee el token del `.env` y lo
devuelve a git. Consecuencias:

- No existe `~/.git-credentials`, así que no hay una segunda copia del secreto
  en disco que mantener sincronizada.
- Rotar el token en el `.env` surte efecto en el siguiente `git push`, sin
  limpiar cachés ni reconfigurar nada.
- `store` y `erase` no hacen nada, a propósito: este diseño no tiene estado
  que invalidar.

Publicar es entonces `scripts/publish.sh`, que además **se niega a publicar
con el árbol sucio**, para que un push no automático nunca suba un estado a
medio hacer.

## Superficie del MCP de GitHub

En el `config.yaml` del perfil, el servidor `github` quedó con:

```yaml
args:
  - stdio
  - --toolsets=context,pull_requests,repos,git,issues,gists,actions,projects,labels
env:
  GITHUB_PERSONAL_ACCESS_TOKEN: ghp_…
  GITHUB_TOOLSETS: context,pull_requests,repos,git,issues,gists,actions,projects,labels
```

Dos detalles que costaron tiempo descubrir y que conviene no volver a perder:

1. **`--toolsets` va en `args`, no sólo en `GITHUB_TOOLSETS`.** El binario
   ignora la variable de entorno: con ella puesta registra
   `unrecognized toolsets ignored` y arranca sin las herramientas. Sólo el
   flag en la línea de comandos las activa.
2. Los nombres de las herramientas no se deducen del toolset. `issues` no da
   `create_issue`: da `issue_write`. La lista real se obtuvo con
   `github-mcp-server generate-docs` (escribe en el `README.md` del directorio
   actual, entre los marcadores `AUTOMATED TOOLSETS` y `AUTOMATED TOOLS`).

Con esos toolsets el servidor pasa de 13 a 56 herramientas, e incluye
`create_repository`, `push_files`, `create_branch`, `create_or_update_file`,
`issue_write` y `create_gist`.

## Límite conocido: `push_files` no sube historia

`push_files` crea commits sobre la rama por API. **No reproduce un historial
git local.** Publicar este proyecto con él produciría un remoto cuyo historial
no corresponde al local, y el siguiente `git push` normal fallaría con
`rejected (non-fast-forward)`.

Por eso la vía de publicación es `git push` de verdad, y el MCP se usa para lo
que sí conviene por API: crear repos, abrir issues, comentar en PRs.

## Alternativa considerada: clave SSH

Se llegó a generar una clave de despliegue (`~/.ssh/id_ed25519_hermes`) antes
de tener el PAT clásico. Se descartó porque el PAT clásico ya cubre el caso y
evita un segundo mecanismo de credenciales que mantener.

Si algún día el PAT clásico deja de servir, la clave está ahí y sólo hay que
registrarla como deploy key con escritura:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBz1o+DjNv1h6ZVhaXg9szdNg4ZRBqcQ1VDvjkjaDB7M hermes-web-dev@starradar
```

Huella: `SHA256:aeOw7MSzcxTKVz9b7jjul10jesrEga1fwP05+nLFn5k`
