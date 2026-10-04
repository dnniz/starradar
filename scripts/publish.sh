#!/usr/bin/env bash
# Publica starradar (o el repo en el que se ejecute) con la credencial del
# perfil. No depende de SSH ni de ~/.git-credentials: el credential helper
# global (git-credential-env) lee el PAT del .env del perfil en cada uso, así
# que rotar el token en el .env basta y no queda ningún secreto duplicado.
#
# Uso:
#   ./scripts/publish.sh              # publica la rama actual
#   ./scripts/publish.sh --remote-only # sólo fija el remoto
set -euo pipefail

REPO_DIR="${STARRADAR_DIR:-/home/hermes/.hermes/home/workspaces/web/starradar}"
REMOTE="https://github.com/dnniz/starradar.git"

cd "$REPO_DIR"

# Nunca debe abrir un prompt: si falla la credencial, que falle ya.
export GIT_TERMINAL_PROMPT=0

if [ "${1:-}" = "--remote-only" ]; then
  git remote set-url origin "$REMOTE"
  echo "remoto: $(git remote get-url origin)"
  exit 0
fi

git remote set-url origin "$REMOTE"

if [ -n "$(git status --porcelain)" ]; then
  echo "→ hay cambios sin commitear:"
  git status --short
  echo "  (commit o stash antes de publicar)"
  exit 1
fi

echo "→ empujando $(git rev-parse --abbrev-ref HEAD) a $REMOTE"
git push -u origin HEAD:main
echo "→ publicado: https://github.com/dnniz/starradar"
