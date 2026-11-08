#!/usr/bin/env bash
# Despliega todo en orden. Cada stack lee los outputs del anterior (${cf:...}).
#   export CRM_DB_PASSWORD='...'                 # mínimo 8 caracteres, sin / @ " ni espacios
#   ./desplegar.sh                               # todo
#   ./desplegar.sh --email gerente@correo.com    # + suscribe ese email a los avisos
#   ./desplegar.sh --solo ms3-leads              # un solo servicio (compartido, rds o msN-x)
#   ./desplegar.sh --stage prod
set -euo pipefail

STAGE=dev; EMAIL=''; SOLO=''
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) STAGE="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --solo)  SOLO="$2";  shift 2 ;;
    *) echo "Opción desconocida: $1" >&2; exit 1 ;;
  esac
done
[ -n "${CRM_DB_PASSWORD:-}" ] || { echo 'Define primero CRM_DB_PASSWORD (la clave del RDS)' >&2; exit 1; }

INFRA="$(cd "$(dirname "$0")" && pwd)"
RAIZ="$(dirname "$INFRA")"
PYTHON="${PYTHON:-python3}"

carpeta() {
  case "$1" in
    compartido|rds) echo "$INFRA/$1" ;;
    *) echo "$RAIZ/crm-grupo1-$1" ;;
  esac
}

salida() {  # salida <stack> <OutputKey>
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

crear_bases() {
  echo -e "\n=== Bases del RDS (clientes_db, ventas_db) ==="
  "$PYTHON" -c 'import pg8000' 2>/dev/null || "$PYTHON" -m pip install -q --user -r "$INFRA/requirements-dev.txt"
  "$PYTHON" "$INFRA/rds/crear_bases.py" --host "$(salida "crm-rds-$STAGE" DbHost)"
}

"$INFRA/sincronizar-comun.sh"

ORDEN=(compartido rds ms1-usuarios ms2-clientes ms3-leads ms4-oportunidades ms5-actividades)
[ -n "$SOLO" ] && ORDEN=("$SOLO")

for servicio in "${ORDEN[@]}"; do
  dir="$(carpeta "$servicio")"
  [ -f "$dir/serverless.yml" ] || { echo "No encuentro $dir/serverless.yml (¿corriste ./clonar.sh?)" >&2; exit 1; }
  echo -e "\n=== $servicio ==="
  # compartido y rds usan el Serverless instalado en el repo infra; cada MS, el suyo.
  base="$dir"
  if [ "$servicio" = compartido ] || [ "$servicio" = rds ]; then base="$INFRA"; fi
  if [ ! -d "$base/node_modules" ]; then (cd "$base" && npm install --no-fund --no-audit); fi
  args=(deploy --stage "$STAGE")
  if [ "$servicio" = compartido ] && [ -n "$EMAIL" ]; then args+=("--param=emailNotificaciones=$EMAIL"); fi
  (cd "$dir" && "$base/node_modules/.bin/serverless" "${args[@]}")
  if [ "$servicio" = rds ]; then crear_bases; fi
done

if [ -z "$SOLO" ]; then
  echo -e "\nURLs de las APIs (ponlas en el config del frontend):"
  for s in ms1-usuarios ms2-clientes ms3-leads ms4-oportunidades ms5-actividades; do
    echo "  $s -> $(salida "crm-$s-$STAGE" HttpApiUrl)"
  done
fi
