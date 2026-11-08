# Despliega todo en orden. Cada stack lee los outputs del anterior (${cf:...}).
#   $env:CRM_DB_PASSWORD = '...'
#   .\desplegar.ps1                         # todo
#   .\desplegar.ps1 -Solo ms3-leads         # un solo servicio
#   .\desplegar.ps1 -Email gerente@correo.com
param(
    [string]$Stage = 'dev',
    [string]$Email = '',
    [string]$Solo = ''
)
$ErrorActionPreference = 'Stop'
if (-not $env:CRM_DB_PASSWORD) { throw 'Define primero $env:CRM_DB_PASSWORD (la clave del RDS)' }

& "$PSScriptRoot/sincronizar-comun.ps1"

$orden = 'infra/compartido', 'infra/rds', 'ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades'
if ($Solo) { $orden = @($Solo) }

foreach ($servicio in $orden) {
    Write-Host "`n=== $servicio ===" -ForegroundColor Cyan
    Push-Location (Join-Path $PSScriptRoot $servicio)
    try {
        $argumentos = @('serverless', 'deploy', '--stage', $Stage)
        if ($servicio -eq 'infra/compartido' -and $Email) { $argumentos += "--param=emailNotificaciones=$Email" }
        npx @argumentos
        if ($LASTEXITCODE -ne 0) { throw "Falló el despliegue de $servicio" }
    } finally {
        Pop-Location
    }
}

if (-not $Solo) {
    Write-Host "`nURLs de las APIs (ponlas en el config del frontend):" -ForegroundColor Green
    foreach ($s in 'ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades') {
        $url = aws cloudformation describe-stacks --stack-name "crm-$s-$Stage" --query "Stacks[0].Outputs[?OutputKey=='HttpApiUrl'].OutputValue" --output text
        Write-Host "  $s -> $url"
    }
}
