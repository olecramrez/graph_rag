# =====================================================
# APP2 - INDEXACAO (STREAMLIT)
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectName = Split-Path $projectPath -Leaf
$venvPath = Join-Path $env:USERPROFILE "${projectName}_venv\Scripts\python.exe"

Write-Host ""
Write-Host "======================================="
Write-Host "      APP2 - INDEXACAO RAG"
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
& $venvPath -m streamlit run app2.py
pause
