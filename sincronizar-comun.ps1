# Copia comun/comun.py a los 5 MS y comun/db.py a los 2 MS con PostgreSQL (repos hermanos).
# Correr después de editar algo en comun/ (desplegar.ps1 lo llama solo).
$ErrorActionPreference = 'Stop'
$infra = $PSScriptRoot
$raiz = Split-Path $infra -Parent
foreach ($ms in 'ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades') {
    $src = Join-Path $raiz "crm-grupo1-$ms/src"
    if (-not (Test-Path $src)) { throw "Falta ${src}: corre .\clonar.ps1" }
    Copy-Item (Join-Path $infra 'comun/comun.py') $src -Force
    if (-not (Test-Path (Join-Path $src '__init__.py'))) { New-Item -ItemType File (Join-Path $src '__init__.py') | Out-Null }
}
foreach ($ms in 'ms2-clientes', 'ms4-oportunidades') {
    Copy-Item (Join-Path $infra 'comun/db.py') (Join-Path $raiz "crm-grupo1-$ms/src") -Force
}
Write-Host 'comun.py y db.py sincronizados'
