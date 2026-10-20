$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) {
    py -3.14 -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
$env:OZON_ENABLE_TEST_TOOLS = "1"
Write-Host "Starting local API at http://127.0.0.1:8765 (leave this window open)."
python -m app.main
