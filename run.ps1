# One-command local run on Windows PowerShell:   .\run.ps1        (http://localhost:8000)
#   $env:FACEID_PASSWORD = "secret"; $env:PORT = "9000"; .\run.ps1
#   $env:FACEID_OPERATOR_NAME = "Altair"   # name shown on the login loader (default ALTAIR)
# If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if ($env:PYTHON) { $env:PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
if (-not (Test-Path ".venv")) { & $py -m venv .venv }
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPy -m pip install -q --upgrade pip | Out-Null
& $venvPy -m pip install -q -r requirements.txt

# Models (same files scripts/download_models.sh fetches)
New-Item -ItemType Directory -Force -Path models | Out-Null
if (-not ((Test-Path "models\det_10g.onnx") -and (Test-Path "models\w600k_r50.onnx"))) {
  Write-Host "Downloading face models (buffalo_l, ~280 MB)..."
  $zip = Join-Path $env:TEMP "buffalo_l.zip"
  Invoke-WebRequest -Uri "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip" -OutFile $zip
  $tmp = Join-Path $env:TEMP "buffalo_l"
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  Get-ChildItem $tmp -Recurse -Include det_10g.onnx, w600k_r50.onnx | Copy-Item -Destination models -Force
}

if ($null -eq $env:FACEID_PASSWORD) {
  $env:FACEID_PASSWORD = "faceid"
  Write-Warning "FACEID_PASSWORD not set; using default password 'faceid'. Set `$env:FACEID_PASSWORD to change it (empty string disables login)."
}
if (-not $env:FACEID_SECRET) {
  New-Item -ItemType Directory -Force -Path data | Out-Null
  if (-not (Test-Path "data\.secret")) { & $venvPy -c "import secrets;print(secrets.token_hex(32))" | Set-Content -NoNewline "data\.secret" }
  $env:FACEID_SECRET = (Get-Content "data\.secret" -Raw).Trim()
}
$port = if ($env:PORT) { $env:PORT } else { "8000" }
$hostAddr = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
Write-Host "FaceID running on http://localhost:$port  (camera works on localhost or https only)"
& $venvPy -m uvicorn app.main:app --host $hostAddr --port $port
