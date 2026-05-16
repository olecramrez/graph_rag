import os
import subprocess
import sys
from pathlib import Path


# =====================================================
# CONFIG
# =====================================================

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_NAME = PROJECT_ROOT.name

BASE_RAG_DIR = PROJECT_ROOT.parent / "base_rag"

BASE_DATA_DIR = BASE_RAG_DIR / "data"
BASE_DOCS_DIR = BASE_RAG_DIR / "documentos"
BASE_USERS_DIR = BASE_RAG_DIR / "users"

USER_HOME = Path.home()
USER_NAME = os.getenv("USERNAME") or os.getenv("USER") or USER_HOME.name
USER_PROJECT_DIR = USER_HOME / PROJECT_NAME
USER_DATA_DIR = USER_PROJECT_DIR / "data"
USER_ENV_PATH = USER_PROJECT_DIR / ".env"
VENV_DIR = USER_HOME / f"{PROJECT_NAME}_venv"

REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
RUN_PS1 = PROJECT_ROOT / "run.ps1"
SETUP_PS1 = PROJECT_ROOT / "setup.ps1"

ENV_DEFAULTS = {
    "LIA_API_KEY": "",
    "LIA_ENDPOINT": "https://lia-api.cgu.gov.br/api/resources/DiretoGpt",
    "LIA_API_VERSION": "2024-10-21",
    "LIA_LLM_MODEL": "gpt-5.4",
    "LIA_EMBED_MODEL": "text-embedding-3-small",
    "RAG_ENABLE_TEMPORAL_QUERY_LLM": "1",
    "RAG_TEMPORAL_QUERY_MODEL": "o3-mini",
    "RAG_QUERY_GENERATION_PROVIDER": "lia_gpt53",
    "LIA_QUERY_GENERATION_MODEL": "gpt-5.3-chat",
    "LIA_QUERY_GENERATION_API_VERSION": "2025-04-01-preview",
    "LIA_QUERY_GENERATION_ALLOW_ONLY_ENTRAID": "false",
    "RAG_RERANK_PROVIDER": "lia_cohere",
    "LIA_RERANK_URL": "https://lia-api.cgu.gov.br/api/tools/rerank?allow_only_entraid=false",
    "LIA_RERANK_MODEL": "Cohere-rerank-v4.0-pro",
    "LIA_RERANK_MAX_TOKENS_PER_DOC": "4096",
    "LIA_RERANK_TIMEOUT": "120",
}


# =====================================================
# HELPERS
# =====================================================

def run_command(command):
    result = subprocess.run(command)
    if result.returncode != 0:
        print(f"\nErro ao executar: {command}")
        sys.exit(1)


def get_venv_python():
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def sync_env_file(path, updates):
    lines = []

    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    existing_values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        existing_values[key.strip().lstrip("\ufeff")] = value.strip()

    effective_updates = dict(updates)
    if existing_values.get("LIA_API_KEY"):
        effective_updates["LIA_API_KEY"] = existing_values["LIA_API_KEY"]

    updated = []
    found = set()
    changed = []

    for line in lines:
        stripped = line.strip()

        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip().lstrip("\ufeff")

            if key in effective_updates:
                old_value = line.split("=", 1)[1].strip()
                new_value = effective_updates[key]
                updated.append(f"{key}={new_value}")
                found.add(key)

                if old_value != new_value and key not in changed:
                    changed.append(key)

                continue

        updated.append(line)

    missing = [key for key in effective_updates if key not in found]
    if missing:
        if updated and updated[-1].strip():
            updated.append("")

        for key in missing:
            updated.append(f"{key}={effective_updates[key]}")
            changed.append(key)

    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    return changed


def ensure_user_env():
    USER_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    auto_shared_root = BASE_RAG_DIR.as_posix()
    env_values = {**ENV_DEFAULTS, "RAG_SHARED_ROOT": auto_shared_root}

    if USER_ENV_PATH.exists():
        changed = sync_env_file(USER_ENV_PATH, env_values)
        print(f".env ja existe: {USER_ENV_PATH}")
        print(f"RAG_SHARED_ROOT atualizado para: {auto_shared_root}")
        if changed:
            print("Chaves atualizadas no .env: " + ", ".join(changed))
        else:
            print("Todas as configuracoes gerenciadas ja estavam atualizadas.")
        return

    USER_ENV_PATH.write_text(
        "\n".join(f"{key}={value}" for key, value in env_values.items()) + "\n",
        encoding="utf-8",
    )
    print(f".env criado: {USER_ENV_PATH}")
    print(f"RAG_SHARED_ROOT definido para: {auto_shared_root}")


def ensure_run_script():
    RUN_PS1.write_text(
        """# =====================================================
# RAG - EXECUCAO LOCAL (VENV DO USUARIO)
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectName = Split-Path $projectPath -Leaf
$venvPath = Join-Path $env:USERPROFILE "${projectName}_venv\\Scripts\\python.exe"

Write-Host ""
Write-Host "======================================="
Write-Host "        RAG - EXECUCAO LOCAL"
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
& $venvPath -m streamlit run app.py
pause
""",
        encoding="utf-8",
    )
    print(f"Script criado/atualizado: {RUN_PS1}")


def ensure_setup_script():
    SETUP_PS1.write_text(
        """# =====================================================
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
""",
        encoding="utf-8",
    )
    print(f"Script criado/atualizado: {SETUP_PS1}")


def create_project_shortcuts():
    if sys.platform != "win32":
        print("Atalho automatico suportado apenas no Windows.")
        return

    target_path = "powershell.exe"

    def q(text):
        return str(text).replace("'", "''")

    shortcuts = [
        {
            "path": PROJECT_ROOT / "Abrir_RAG.lnk",
            "arguments": f'-NoProfile -ExecutionPolicy Bypass -File "{RUN_PS1}"',
        },
        {
            "path": PROJECT_ROOT / "Instalar_RAG.lnk",
            "arguments": f'-NoProfile -ExecutionPolicy Bypass -File "{SETUP_PS1}"',
        },
    ]

    for shortcut in shortcuts:
        shortcut_path = shortcut["path"]
        arguments = shortcut["arguments"]

        ps_script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$shortcutPath = '{q(shortcut_path)}'; "
            "if (Test-Path -LiteralPath $shortcutPath) { "
            "Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue "
            "}; "
            "$ws = New-Object -ComObject WScript.Shell; "
            "$sc = $ws.CreateShortcut($shortcutPath); "
            f"$sc.TargetPath = '{q(target_path)}'; "
            f"$sc.Arguments = '{q(arguments)}'; "
            f"$sc.WorkingDirectory = '{q(PROJECT_ROOT)}'; "
            "$sc.IconLocation = '%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe,0'; "
            "$sc.Save();"
        )

        result = subprocess.run([
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_script,
        ])

        if result.returncode == 0:
            print(f"Atalho criado: {shortcut_path}")
            continue

        # Em pastas de rede, a escrita de .lnk pode falhar por bloqueio/permissao.
        # Nao interrompe o setup; o usuario ainda pode executar setup.ps1/run.ps1.
        print(f"[WARN] Nao foi possivel criar atalho: {shortcut_path}")
        print("[WARN] Continue usando os scripts .ps1 diretamente nesta pasta.")


def install_or_update_requirements(python_path):
    if not REQUIREMENTS_FILE.exists():
        print(f"requirements.txt nao encontrado em: {REQUIREMENTS_FILE}")
        sys.exit(1)

    print("\nAtualizando pip...")
    run_command([
        str(python_path),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "--trusted-host",
        "pypi.org",
        "--trusted-host",
        "files.pythonhosted.org",
    ])

    if sys.platform == "win32":
        print("Configurando certificados do Windows...")
        run_command([
            str(python_path),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "python-certifi-win32",
            "--trusted-host",
            "pypi.org",
            "--trusted-host",
            "files.pythonhosted.org",
        ])

    print("Atualizando requirements...")
    run_command([
        str(python_path),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "-r",
        str(REQUIREMENTS_FILE),
        "--trusted-host",
        "pypi.org",
        "--trusted-host",
        "files.pythonhosted.org",
    ])


# =====================================================
# MAIN
# =====================================================

def main():
    print("\n=======================================")
    print("        SETUP DO USUARIO (RAG)")
    print("=======================================\n")

    # Estrutura local do usuario.
    USER_PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (USER_DATA_DIR / "raw_docs").mkdir(parents=True, exist_ok=True)

    # Estrutura compartilhada: so usa se a pasta base_rag ja existir.
    if BASE_RAG_DIR.exists():
        BASE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        BASE_DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (BASE_USERS_DIR / USER_NAME / "logs").mkdir(parents=True, exist_ok=True)
        (BASE_USERS_DIR / USER_NAME / "exports").mkdir(parents=True, exist_ok=True)
    else:
        print(f"[INFO] base_rag nao encontrada em: {BASE_RAG_DIR}")
        print("[INFO] Crie a pasta manualmente para habilitar as bases compartilhadas.")

    print(f"Pasta local do usuario: {USER_PROJECT_DIR}")
    print(f"Base compartilhada: {BASE_RAG_DIR}")

    if not VENV_DIR.exists():
        print("Criando ambiente virtual...")
        run_command([sys.executable, "-m", "venv", str(VENV_DIR)])
    else:
        print(f"Venv ja existe: {VENV_DIR}")

    python_path = get_venv_python()
    install_or_update_requirements(python_path)
    ensure_user_env()
    ensure_run_script()
    ensure_setup_script()
    create_project_shortcuts()

    print("\n=======================================")
    print("SETUP CONCLUIDO")
    print("=======================================\n")
    print("Use os atalhos da pasta do projeto: Abrir_RAG.lnk e Instalar_RAG.lnk.")


if __name__ == "__main__":
    main()
