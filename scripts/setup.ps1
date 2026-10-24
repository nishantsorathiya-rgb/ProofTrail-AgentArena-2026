param([Alias('Python312')][string]$PythonExecutable)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    $taskVenvPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $taskVenvPython) {
        & $taskVenvPython -c 'import sys; assert sys.version_info[:2] in {(3, 11), (3, 12)}, "Existing .venv must use Python 3.11 or 3.12"'
        if ($LASTEXITCODE -ne 0) { throw 'Existing environment must use Python 3.11 or 3.12.' }
    } elseif ($PythonExecutable) {
        & $PythonExecutable -c 'import sys; assert sys.version_info[:2] in {(3, 11), (3, 12)}, "Python 3.11 or 3.12 required"'
        if ($LASTEXITCODE -ne 0) { throw 'Python version validation failed.' }
        & $PythonExecutable -m venv .venv
    } else {
        & py -3.12 -c 'import sys; assert sys.version_info[:2] in {(3, 11), (3, 12)}, "Python 3.11 or 3.12 required"'
        if ($LASTEXITCODE -ne 0) { throw 'Pass -PythonExecutable with installed Python 3.11 or 3.12.' }
        & py -3.12 -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    & .\.venv\Scripts\python.exe --version
    & .\.venv\Scripts\python.exe -m pip install -c requirements.lock -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & .\.venv\Scripts\python.exe -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
} finally {
    Pop-Location
}
