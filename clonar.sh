#!/usr/bin/env bash
# Clona (o actualiza) los 5 repos de microservicios AL LADO de este repo:
#   carpeta/
#   ├── crm-grupo1-infra/      <- estás aquí
#   ├── crm-grupo1-ms1-usuarios/
#   └── ...
# Uso: ./clonar.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ORG=https://github.com/enriquetorres-cell
for ms in ms1-usuarios ms2-clientes ms3-leads ms4-oportunidades ms5-actividades; do
  repo="crm-grupo1-$ms"
  if [ -d "$repo/.git" ]; then
    echo "== $repo: git pull"
    git -C "$repo" pull --ff-only
  else
    echo "== $repo: git clone"
    git clone "$ORG/$repo.git"
  fi
done
