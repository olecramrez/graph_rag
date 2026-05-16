# =====================================================
# RAG MULTI-BASE PORTAVEL
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectName = Split-Path $projectPath -Leaf
$venvPath = Join-Path $env:USERPROFILE "${projectName}_venv\Scripts\python.exe"

Write-Host ""
Write-Host "======================================="
Write-Host "  RAG Multi-Base Portavel"
Write-Host "======================================="
Write-Host ""
Write-Host "Projeto: $projectPath"
Write-Host "Python:  $venvPath"
Write-Host ""

if (!(Test-Path $venvPath)) {
    Write-Host "Ambiente virtual nao encontrado."
    Write-Host "Execute primeiro: python setup_user.py"
    pause
    exit
}

Set-Location $projectPath

Write-Host "Iniciando Streamlit..."
Write-Host ""

& $venvPath -m streamlit run app.py

pause
