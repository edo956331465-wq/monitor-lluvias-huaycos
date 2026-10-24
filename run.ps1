# Lanzador del pipeline. Busca un Python que realmente funcione, no solo que exista:
# Windows trae un alias "python" que abre la Microsoft Store y falla en silencio.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$candidates = @()
foreach ($name in @("python", "py")) {
    if (Get-Command $name -ErrorAction SilentlyContinue) { $candidates += $name }
}
$candidates += (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")

$exe = $null
foreach ($c in $candidates) {
    try {
        $ver = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -match '^3\.(1[1-9]|[2-9]\d)$') {
            $exe = $c
            Write-Host "Python $ver -> $c"
            break
        }
    } catch { }
}

if (-not $exe) {
    throw "No hay Python 3.11+ funcionando. Instalalo o agrega 'python' al PATH."
}

& $exe "run_all.py" @args
exit $LASTEXITCODE
