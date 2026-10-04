#!/usr/bin/env bash
# Publica starradar por SSH y deja el remoto en esa URL para futuros pushes.
#
# SSH evita el problema del PAT fine-grained: su "Repository access" es una
# lista explícita y se queda obsoleta en cuanto creas un repo. Una clave de
# despliegue no caduca y da acceso a los repos donde la registres.
set -euo pipefail

REPO_DIR="${STARRADAR_DIR:-/home/hermes/.hermes/home/workspaces/web/starradar}"
KEY="$HOME/.ssh/id_ed25519_hermes"
SSH_URL="git@github.com:dnniz/starradar.git"

cd "$REPO_DIR"

# Identidad: sin esto los commits salen como root@contenedor.
git config user.name "Hermes (web-dev)"
git config user.email "hermes-web-dev@users.noreply.github.com"

# Usar la clave de este perfil sin tocar la config global de git.
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ "${1:-}" = "--remote-only" ]; then
  git remote set-url origin "$SSH_URL"
  echo "remoto: $(git remote get-url origin)"
  exit 0
fi

git remote set-url origin "$SSH_URL"
echo "→ empujando a $SSH_URL"
git push -u origin HEAD:main
echo "→ publicado: https://github.com/dnniz/starradar"
