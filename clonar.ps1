# Clona (o actualiza) los 5 repos de microservicios AL LADO de este repo.
# Uso: .\clonar.ps1
$ErrorActionPreference = 'Stop'
$raiz = Split-Path $PSScriptRoot -Parent
$org = 'https://github.com/enriquetorres-cell'
foreach ($ms in 'ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades', 'frontend') {
    $repo = "crm-grupo1-$ms"
    $dir = Join-Path $raiz $repo
    if (Test-Path (Join-Path $dir '.git')) {
        Write-Host "== ${repo}: git pull"
        git -C $dir pull --ff-only
    } else {
        Write-Host "== ${repo}: git clone"
        git -C $raiz clone "$org/$repo.git"
    }
    if ($LASTEXITCODE -ne 0) { throw "Falló git en $repo" }
}
