import subprocess
import sys
from pathlib import Path
import os

# =====================================================
# CONFIGURACOES DO PROJETO
# =====================================================

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_NAME = PROJECT_ROOT.name

SHARED_BASE_DIR = PROJECT_ROOT.parent / "base_rag"

NETWORK_DATA_DIR = SHARED_BASE_DIR / "data"
NETWORK_DOCS_DIR = SHARED_BASE_DIR / "documentos"
NETWORK_USERS_DIR = SHARED_BASE_DIR / "users"

USER_HOME = Path.home()
USER_NAME = os.getenv("USERNAME") or os.getenv("USER") or USER_HOME.name
USER_PROJECT_DIR = USER_HOME / PROJECT_NAME
DATA_DIR = USER_PROJECT_DIR / "data"
VENV_DIR = USER_HOME / f"{PROJECT_NAME}_venv"

USER_ENV = USER_PROJECT_DIR / ".env"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
RUN_PS1 = PROJECT_ROOT / "run_rag.ps1"

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
# EXECUTAR COMANDO
# =====================================================

def run_command(command):

    result = subprocess.run(command)

    if result.returncode != 0:
        print("\nErro ao executar:", command)
        sys.exit(1)


def upsert_env_lines(lines, updates):
    updated = []
    found = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                current_value = line.split("=", 1)[1]
                value = current_value if key == "LIA_API_KEY" and current_value.strip() else updates[key]
                updated.append(f"{key}={value}")
                found.add(key)
                continue
        updated.append(line)

    missing = [key for key in updates if key not in found]
    if missing:
        if updated and updated[-1].strip():
            updated.append("")
        for key in missing:
            updated.append(f"{key}={updates[key]}")

    return updated


# =====================================================
# INICIO
# =====================================================

print("\n=======================================")
print("   SETUP DO RAG MULTI-BASE PORTAVEL")
print("=======================================\n")


# =====================================================
# 1) PASTA LOCAL DO USUARIO
# =====================================================

USER_PROJECT_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "raw_docs").mkdir(parents=True, exist_ok=True)

print(f"Pasta local criada: {USER_PROJECT_DIR}")
print(f"Pasta de dados local: {DATA_DIR}")


# =====================================================
# 2) PASTAS DA BASE COMPARTILHADA NA REDE
# =====================================================

if SHARED_BASE_DIR.exists():
    NETWORK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    NETWORK_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (NETWORK_USERS_DIR / USER_NAME / "logs").mkdir(parents=True, exist_ok=True)
    (NETWORK_USERS_DIR / USER_NAME / "exports").mkdir(parents=True, exist_ok=True)

    print(f"Base compartilhada: {SHARED_BASE_DIR}")
    print(f"Dados de indice: {NETWORK_DATA_DIR}")
    print(f"Documentos por base: {NETWORK_DOCS_DIR}")
    print(f"Pasta de usuario na rede: {NETWORK_USERS_DIR / USER_NAME}")
else:
    print(f"[INFO] base_rag nao encontrada em: {SHARED_BASE_DIR}")
    print("[INFO] Crie a pasta manualmente para habilitar as bases compartilhadas.")


# =====================================================
# 3) AMBIENTE VIRTUAL
# =====================================================

if not VENV_DIR.exists():

    print("Criando ambiente virtual...")

    run_command([
        sys.executable,
        "-m",
        "venv",
        str(VENV_DIR)
    ])

else:
    print("Ambiente virtual ja existe.")


# =====================================================
# 4) CAMINHO DO PYTHON DO VENV
# =====================================================

if sys.platform == "win32":
    python_path = VENV_DIR / "Scripts" / "python.exe"
else:
    python_path = VENV_DIR / "bin" / "python"


# =====================================================
# 5) ATUALIZAR PIP
# =====================================================

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
    "files.pythonhosted.org"
])


# =====================================================
# 6) CORRIGIR CERTIFICADOS DO PYTHON
# =====================================================

print("Configurando certificados do Windows...")

run_command([
    str(python_path),
    "-m",
    "pip",
    "install",
    "python-certifi-win32",
    "--trusted-host",
    "pypi.org",
    "--trusted-host",
    "files.pythonhosted.org"
])


# =====================================================
# 7) INSTALAR DEPENDENCIAS
# =====================================================

if not REQUIREMENTS_FILE.exists():
    print("requirements.txt nao encontrado.")
    sys.exit(1)

print("\nInstalando dependencias...")

run_command([
    str(python_path),
    "-m",
    "pip",
    "install",
    "-r",
    str(REQUIREMENTS_FILE),
    "--trusted-host",
    "pypi.org",
    "--trusted-host",
    "files.pythonhosted.org"
])

print("Dependencias instaladas com sucesso.")


# =====================================================
# 8) CRIAR .ENV DO USUARIO
# =====================================================

LOCAL_SHARED_ROOT = SHARED_BASE_DIR.as_posix()

if not USER_ENV.exists():

    env_values = dict(ENV_DEFAULTS)
    env_values["RAG_SHARED_ROOT"] = LOCAL_SHARED_ROOT
    USER_ENV.write_text(
        "\n".join(f"{key}={value}" for key, value in env_values.items()) + "\n",
        encoding="utf-8"
    )

    print("\n.env criado automaticamente.")
    print(f"RAG_SHARED_ROOT definido para: {LOCAL_SHARED_ROOT}")
    print("IMPORTANTE: Edite o arquivo abaixo e insira sua LIA_API_KEY:\n")
    print(USER_ENV)

else:
    lines = USER_ENV.read_text(encoding="utf-8").splitlines()
    env_values = dict(ENV_DEFAULTS)
    env_values["RAG_SHARED_ROOT"] = LOCAL_SHARED_ROOT
    updated = upsert_env_lines(lines, env_values)

    print(".env ja existe.")
    print(f"RAG_SHARED_ROOT atualizado para: {LOCAL_SHARED_ROOT}")
    print("Configuracoes de rerank LIA/Cohere conferidas no .env.")

    USER_ENV.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


# =====================================================
# 9) CRIAR SCRIPT DE EXECUCAO
# =====================================================

if not RUN_PS1.exists():

    RUN_PS1.write_text(
f"""# =====================================================
# RAG MULTI-BASE PORTAVEL
# =====================================================

$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectName = Split-Path $projectPath -Leaf
$venvPath = Join-Path $env:USERPROFILE "${{projectName}}_venv\\Scripts\\python.exe"

Write-Host ""
Write-Host "======================================="
Write-Host "  RAG Multi-Base Portavel"
Write-Host "======================================="
Write-Host ""
Write-Host "Projeto: $projectPath"
Write-Host "Python:  $venvPath"
Write-Host ""

if (!(Test-Path $venvPath)) {{
    Write-Host "Ambiente virtual nao encontrado."
    Write-Host "Execute primeiro: python setup_user.py"
    pause
    exit
}}

Set-Location $projectPath

Write-Host "Iniciando Streamlit..."
Write-Host ""

& $venvPath -m streamlit run app.py

pause
""",
        encoding="utf-8"
    )

    print("run_rag.ps1 criado automaticamente.")

else:
    print("run_rag.ps1 ja existe.")


# =====================================================
# FINAL
# =====================================================

print("\n=======================================")
print("SETUP CONCLUIDO")
print("=======================================\n")

print("Para iniciar:")
print("powershell -ExecutionPolicy Bypass -File .\\run_rag.ps1\n")
