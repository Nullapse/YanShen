[CmdletBinding()]
param(
    [switch]$SkipPythonTests,
    [ValidateRange(1, 16)]
    [int]$PythonTestWorkers = 4
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
}

$testTempRoot = Join-Path $projectRoot ".test-tmp\check"
New-Item -ItemType Directory -Force -Path $testTempRoot | Out-Null
$env:TEMP = $testTempRoot
$env:TMP = $testTempRoot

Push-Location $projectRoot
try {
    & $pythonExecutable -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed." }

    npm run check
    if ($LASTEXITCODE -ne 0) { throw "Frontend checks failed." }

    if (-not $SkipPythonTests) {
        & $pythonExecutable scripts\run_python_tests.py --workers $PythonTestWorkers
        if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }
    }
}
finally {
    Pop-Location
}
