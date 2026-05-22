# =====================================================
# APP4 - CRIAR SQLITE DA BASE (STREAMLIT)
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectName = Split-Path $projectPath -Leaf
$venvPath = Join-Path $env:USERPROFILE "${projectName}_venv\Scripts\python.exe"

Write-Host ""
Write-Host "======================================="
Write-Host "      APP4 - CRIAR SQLITE DA BASE"
Write-Host "======================================="
Write-Host "Projeto: $projectPath"
Write-Host "Python:  $venvPath"
Write-Host ""

if (!(Test-Path $venvPath)) {
    Write-Host "Ambiente virtual nao encontrado."
    Write-Host "Execute primeiro: setup.ps1"
    pause
    exit 1
}

Set-Location $projectPath

$port = 8504
while (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Porta $port em uso. Tentando $($port + 1)..."
    $port += 1
}

$url = "http://localhost:$port"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host "URL:     $url"
Write-Host ""

Start-Process $url
& $venvPath -m streamlit run app4.py --server.port $port --server.headless true
pause
