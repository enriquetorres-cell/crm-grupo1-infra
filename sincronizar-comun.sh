#!/usr/bin/env bash
# Copia comun/comun.py a los 5 MS y comun/db.py a los 2 MS con PostgreSQL (repos hermanos).
# Correr después de editar algo en comun/ (desplegar.sh lo llama solo).
set -euo pipefail
INFRA="$(cd "$(dirname "$0")" && pwd)"
RAIZ="$(dirname "$INFRA")"

for ms in ms1-usuarios ms2-clientes ms3-leads ms4-oportunidades ms5-actividades; do
  src="$RAIZ/crm-grupo1-$ms/src"
  [ -d "$src" ] || { echo "Falta $src: corre ./clonar.sh" >&2; exit 1; }
  cp "$INFRA/comun/comun.py" "$src/"
  [ -f "$src/__init__.py" ] || touch "$src/__init__.py"
done
for ms in ms2-clientes ms4-oportunidades; do
  cp "$INFRA/comun/db.py" "$RAIZ/crm-grupo1-$ms/src/"
done
echo 'comun.py y db.py sincronizados'
