$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$launcher = Join-Path $PSScriptRoot 'scripts\run_bijisatu.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error 'Project Python was not found. Complete the virtual-environment setup first.'
    exit 2
}

& $python $launcher @args
exit $LASTEXITCODE

