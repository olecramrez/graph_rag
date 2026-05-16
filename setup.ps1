# =====================================================
# SETUP DO USUARIO - RAG
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$setUserPath = Join-Path $projectPath "set_user.py"

Write-Host ""
Write-Host "======================================="
Write-Host "        SETUP DO USUARIO (RAG)"
Write-Host "======================================="
Write-Host "Projeto: $projectPath"
Write-Host ""

if (!(Test-Path $setUserPath)) {
    Write-Host "Arquivo nao encontrado: $setUserPath"
    pause
    exit 1
}

$pythonCmd = $null

if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCmd = @("py", "-3")
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCmd = @("python")
}
else {
    Write-Host "Python nao encontrado no computador."
    Write-Host "Instale o Python e tente novamente."
    pause
    exit 1
}

Set-Location $projectPath

if ($pythonCmd.Length -eq 2) {
    & $pythonCmd[0] $pythonCmd[1] $setUserPath
} else {
    & $pythonCmd[0] $setUserPath
}

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Setup falhou com codigo: $LASTEXITCODE"
    pause
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Setup concluido."
pause
