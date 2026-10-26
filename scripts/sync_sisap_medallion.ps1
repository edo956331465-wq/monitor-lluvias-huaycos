$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$projects = Split-Path -Parent (Split-Path -Parent $repo)
$prices = Join-Path $projects "Precios Mayoristas"
$python = Join-Path $prices ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "No se encontró el Python del proyecto Precios Mayoristas: $python"
}

& $python (Join-Path $repo "src\export_sisap_medallion.py") --precios-root $prices
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location $repo
try {
    git add data/gold/sisap_dashboard.json
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Output "SISAP Gold no cambió; no se requiere publicación."
        exit 0
    }
    $day = Get-Date -Format "yyyy-MM-dd"
    git commit -m "Actualizar SISAP Gold $day"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    git push origin main
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
