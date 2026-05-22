from pathlib import Path
import os
from dotenv import load_dotenv

# =====================================================
# CAMINHOS DO PROJETO
# =====================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = PROJECT_ROOT.name

# =====================================================
# CARREGAMENTO PORTAVEL DO .env
# =====================================================

USER_ROOT = Path.home() / PROJECT_NAME
USER_ENV = USER_ROOT / ".env"
PROJECT_ENV = PROJECT_ROOT / ".env"

if USER_ENV.exists():
    load_dotenv(USER_ENV, override=True)
elif PROJECT_ENV.exists():
    load_dotenv(PROJECT_ENV, override=True)
else:
    load_dotenv(override=True)

# Base compartilhada.
# Prioridade:
# 1) RAG_SHARED_ROOT (quando definido)
# 2) base_rag dentro da pasta do projeto
# 3) base_rag ao lado da pasta do projeto
_inside_project = PROJECT_ROOT / "base_rag"
_shared_root_env = (os.getenv("RAG_SHARED_ROOT") or "").strip()
_sibling_project = PROJECT_ROOT.parent / "base_rag"

def _resolve_env_root(raw_value):
    env_root = Path(raw_value)
    if not env_root.is_absolute():
        env_root = PROJECT_ROOT / env_root
    return env_root


def _count_bases(root_path):
    docs_dir = root_path / "documentos"
    if not docs_dir.exists():
        return 0

    try:
        return sum(1 for p in docs_dir.iterdir() if p.is_dir())
    except OSError:
        return 0


def _first_existing(paths):
    for p in paths:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


_explicit_shared_root = _resolve_env_root(_shared_root_env) if _shared_root_env else None

candidate_roots = [_sibling_project, _inside_project]
if _explicit_shared_root is not None:
    candidate_roots.append(_explicit_shared_root)

seen = set()
unique_candidates = []
for candidate in candidate_roots:
    key = str(candidate).lower()
    if key not in seen:
        seen.add(key)
        unique_candidates.append(candidate)

SHARED_BASE_ROOT = None

# Quando informado explicitamente via ambiente, prioriza esse caminho
# (se acessivel) em vez de inferir por heuristica.
if _explicit_shared_root is not None and _explicit_shared_root.exists():
    SHARED_BASE_ROOT = _explicit_shared_root
else:
    for candidate in unique_candidates:
        if _count_bases(candidate) > 0:
            SHARED_BASE_ROOT = candidate
            break

    if SHARED_BASE_ROOT is None:
        SHARED_BASE_ROOT = _first_existing(unique_candidates)

    if SHARED_BASE_ROOT is None:
        SHARED_BASE_ROOT = _sibling_project

NETWORK_DATA_ROOT = SHARED_BASE_ROOT / "data"
DOCUMENTOS_ROOT = SHARED_BASE_ROOT / "documentos"
NETWORK_USERS_ROOT = SHARED_BASE_ROOT / "users"

def _safe_mkdir(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Evita quebra quando unidade de rede estiver indisponivel.
        pass


if SHARED_BASE_ROOT.exists():
    _safe_mkdir(NETWORK_DATA_ROOT)
    _safe_mkdir(DOCUMENTOS_ROOT)
    _safe_mkdir(NETWORK_USERS_ROOT)

DEFAULT_SHARED_BASE_ROOT = SHARED_BASE_ROOT

# =====================================================
# DIRETORIOS DE DADOS
# =====================================================

LOCAL_DATA_ROOT = USER_ROOT / "data"
PERSISTENT_DATA_ROOT = NETWORK_DATA_ROOT
_faiss_work_root_env = (os.getenv("RAG_FAISS_WORK_ROOT") or "").strip()
FAISS_WORK_ROOT = Path(_faiss_work_root_env) if _faiss_work_root_env else LOCAL_DATA_ROOT

_safe_mkdir(PERSISTENT_DATA_ROOT)
_safe_mkdir(LOCAL_DATA_ROOT)
_safe_mkdir(FAISS_WORK_ROOT)


def set_shared_base_root(root_path):
    """Troca a raiz base_rag em tempo de execucao, sem alterar o .env."""
    global SHARED_BASE_ROOT
    global NETWORK_DATA_ROOT
    global DOCUMENTOS_ROOT
    global NETWORK_USERS_ROOT
    global PERSISTENT_DATA_ROOT

    root = Path(root_path)
    if not root.is_absolute():
        root = PROJECT_ROOT / root

    SHARED_BASE_ROOT = root
    NETWORK_DATA_ROOT = SHARED_BASE_ROOT / "data"
    DOCUMENTOS_ROOT = SHARED_BASE_ROOT / "documentos"
    NETWORK_USERS_ROOT = SHARED_BASE_ROOT / "users"
    PERSISTENT_DATA_ROOT = NETWORK_DATA_ROOT

    if SHARED_BASE_ROOT.exists():
        _safe_mkdir(NETWORK_DATA_ROOT)
        _safe_mkdir(DOCUMENTOS_ROOT)
        _safe_mkdir(NETWORK_USERS_ROOT)


def get_shared_base_root():
    return SHARED_BASE_ROOT


def get_documentos_root():
    return DOCUMENTOS_ROOT


def reset_shared_base_root():
    set_shared_base_root(DEFAULT_SHARED_BASE_ROOT)


def _current_user():
    return (
        os.getenv("USERNAME")
        or os.getenv("USER")
        or USER_ROOT.name
    )


def get_user_root():
    user_dir = NETWORK_USERS_ROOT / _current_user()
    _safe_mkdir(user_dir)
    return user_dir


def get_user_logs_dir():
    logs = get_user_root() / "logs"
    _safe_mkdir(logs)
    return logs


def get_user_exports_dir():
    exports = get_user_root() / "exports"
    _safe_mkdir(exports)
    return exports


# =====================================================
# BASE PADRAO
# =====================================================

def get_default_base():
    return os.getenv("RAG_BASE", "ibama")


# =====================================================
# BASES DISPONIVEIS
# =====================================================

def get_available_bases():
    if not DOCUMENTOS_ROOT.exists():
        return []
    return [p.name for p in DOCUMENTOS_ROOT.iterdir() if p.is_dir()]


# =====================================================
# DIRETORIOS DA BASE
# =====================================================

def get_documents_dir(base_name=None):
    base = base_name or get_default_base()
    return DOCUMENTOS_ROOT / base


def get_persistent_data_dir(base_name=None):
    base = base_name or get_default_base()
    base_dir = PERSISTENT_DATA_ROOT / base
    _safe_mkdir(base_dir)
    return base_dir


def get_local_work_dir(base_name=None):
    base = base_name or get_default_base()
    work_dir = FAISS_WORK_ROOT / base
    _safe_mkdir(work_dir)
    return work_dir


# Compatibilidade com codigo legado: dados persistidos da base.
def get_data_dir(base_name=None):
    return get_persistent_data_dir(base_name)


# Compatibilidade com codigo legado: "raw_docs" agora aponta
# para os documentos de origem na rede (sem copia local de PDFs).
def get_raw_dir(base_name=None):
    return get_documents_dir(base_name)


# =====================================================
# ARQUIVOS DE INDICE
# =====================================================

def get_chunks_path(base_name=None):
    return get_persistent_data_dir(base_name) / "chunks.jsonl"


def get_registry_path(base_name=None):
    return get_persistent_data_dir(base_name) / "registry.json"


def get_faiss_path(base_name=None):
    return get_persistent_data_dir(base_name) / "faiss.index"


def get_faiss_temp_path(base_name=None):
    return get_local_work_dir(base_name) / "faiss.index"


def get_faiss_fallback_path(base_name=None):
    # Fallback sem uso de C: quando faltar espaco no workspace local.
    return get_persistent_data_dir(base_name) / "faiss.work.index"


def get_idmap_path(base_name=None):
    return get_persistent_data_dir(base_name) / "id_map.json"


def get_bm25_path(base_name=None):
    return get_persistent_data_dir(base_name) / "bm25.pkl"


def get_positional_index_path(base_name=None):
    return get_persistent_data_dir(base_name) / "positional_index.pkl"


def get_chunk_checkpoint_path(base_name=None):
    return get_persistent_data_dir(base_name) / "chunk_checkpoint.json"


def get_cnpj_db_candidates():
    candidates = []

    env_path = (os.getenv("CNPJ_SQLITE_PATH") or "").strip()
    if env_path:
        path = Path(env_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        candidates.append(path)

    cnpj_data_dir = get_persistent_data_dir("cnpj")
    candidates.extend(
        [
            cnpj_data_dir / "cnpj.sqlite",
            cnpj_data_dir / "cnpj_2026_01.sqlite",
            PROJECT_ROOT / "data_cnpj" / "cnpj.sqlite",
        ]
    )

    for root in (cnpj_data_dir, PROJECT_ROOT / "data_cnpj"):
        try:
            candidates.extend(sorted(root.glob("cnpj_*.sqlite"), reverse=True))
        except OSError:
            pass

    unique = []
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def get_cnpj_db_path():
    for path in get_cnpj_db_candidates():
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return get_persistent_data_dir("cnpj") / "cnpj.sqlite"
