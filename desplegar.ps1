# Despliega todo en orden desde Windows. Cada stack lee los outputs del anterior (${cf:...}).
# Los 5 repos de MS deben estar AL LADO de este repo (.\clonar.ps1).
#   $env:CRM_DB_PASSWORD = '...'                # mínimo 8 caracteres, sin / @ " ni espacios
#   .\desplegar.ps1                             # todo
#   .\desplegar.ps1 -Email gerente@correo.com   # + suscribe ese email a los avisos
#   .\desplegar.ps1 -Solo ms3-leads             # un solo servicio (compartido, rds o msN-x)
param(
    [string]$Stage = 'dev',
    [string]$Email = '',
    [string]$Solo = ''
)
$ErrorActionPreference = 'Stop'
if (-not $env:CRM_DB_PASSWORD) { throw 'Define primero $env:CRM_DB_PASSWORD (la clave del RDS)' }

$infra = $PSScriptRoot
$raiz = Split-Path $infra -Parent

function Carpeta($servicio) {
    if ($servicio -in 'compartido', 'rds') { return Join-Path $infra $servicio }
    return Join-Path $raiz "crm-grupo1-$servicio"
}

function Salida($stack, $clave) {
    return aws cloudformation describe-stacks --stack-name $stack --query "Stacks[0].Outputs[?OutputKey=='$clave'].OutputValue" --output text
}

& "$infra/sincronizar-comun.ps1"

$orden = 'compartido', 'rds', 'ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades'
if ($Solo) { $orden = @($Solo) }

foreach ($servicio in $orden) {
    $dir = Carpeta $servicio
    if (-not (Test-Path (Join-Path $dir 'serverless.yml'))) { throw "No encuentro $dir\serverless.yml (¿corriste .\clonar.ps1?)" }
    Write-Host "`n=== $servicio ===" -ForegroundColor Cyan
    # compartido y rds usan el Serverless instalado en el repo infra; cada MS, el suyo.
    $base = if ($servicio -in 'compartido', 'rds') { $infra } else { $dir }
    if (-not (Test-Path (Join-Path $base 'node_modules'))) {
        Push-Location $base; try { npm install --no-fund --no-audit } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "Falló npm install en $base" }
    }
    $argumentos = @('deploy', '--stage', $Stage)
    if ($servicio -eq 'compartido' -and $Email) { $argumentos += "--param=emailNotificaciones=$Email" }
    Push-Location $dir
    try {
        & (Join-Path $base 'node_modules/.bin/serverless.cmd') @argumentos
        if ($LASTEXITCODE -ne 0) { throw "Falló el despliegue de $servicio" }
    } finally {
        Pop-Location
    }
    if ($servicio -eq 'rds') {
        Write-Host "`n=== Bases del RDS (clientes_db, ventas_db) ===" -ForegroundColor Cyan
        python -c 'import pg8000' 2>$null
        if ($LASTEXITCODE -ne 0) { python -m pip install -q -r (Join-Path $infra 'requirements-dev.txt') }
        python (Join-Path $infra 'rds/crear_bases.py') --host (Salida "crm-rds-$Stage" 'DbHost')
        if ($LASTEXITCODE -ne 0) { throw 'Falló la creación de las bases del RDS' }
    }
}

if (-not $Solo) {
    Write-Host "`nURLs de las APIs (ponlas en el config del frontend):" -ForegroundColor Green
    foreach ($s in 'ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades') {
        Write-Host "  $s -> $(Salida "crm-$s-$Stage" 'HttpApiUrl')"
    }
}
