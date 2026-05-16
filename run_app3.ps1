# =====================================================
# APP3 - INDEXACAO SEM ENRIQUECIMENTO (STREAMLIT)
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectName = Split-Path $projectPath -Leaf
$venvPath = Join-Path $env:USERPROFILE "${projectName}_venv\Scripts\python.exe"

Write-Host ""
Write-Host "======================================="
Write-Host "      APP3 - INDEXACAO RAG"
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
$port = 8502
$url = "http://localhost:$port"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host "URL:     $url"
Write-Host ""

Start-Process $url
& $venvPath -m streamlit run app3.py --server.port $port
pause
