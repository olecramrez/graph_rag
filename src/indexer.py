import hashlib
import json
from pathlib import Path
from datetime import datetime

from .config import (
    get_registry_path,
    DOCUMENTOS_ROOT
)


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_registry(base_name):
    registry_path = get_registry_path(base_name)
    if not registry_path.exists():
        return {}
    return json.loads(registry_path.read_text(encoding="utf-8"))


def save_registry(registry: dict, base_name):
    registry_path = get_registry_path(base_name)
    registry_path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# =====================================================
# AGORA SCAN OLHA A PASTA DOCUMENTOS (REDE)
# =====================================================

def scan_documents(base_name):

    SOURCE_DIR = DOCUMENTOS_ROOT / base_name
    registry = load_registry(base_name)

    if not SOURCE_DIR.exists():
        return [], [], list(registry.keys())

    current_files = {
        f.name: f for f in SOURCE_DIR.glob("*.pdf") if f.is_file()
    }

    new_files = []
    modified_files = []
    removed_files = []

    # Detectar novos ou modificados
    for name, path in current_files.items():
        current_hash = file_hash(path)

        if name not in registry:
            new_files.append(name)
        elif registry[name]["hash"] != current_hash:
            modified_files.append(name)

    # Detectar removidos
    for name in registry.keys():
        if name not in current_files:
            removed_files.append(name)

    return new_files, modified_files, removed_files


# =====================================================
# REGISTRY AGORA USA HASH DA PASTA DOCUMENTOS
# =====================================================

def update_registry(processed_files, base_name):

    SOURCE_DIR = DOCUMENTOS_ROOT / base_name
    registry = load_registry(base_name)

    for name in processed_files:
        path = SOURCE_DIR / name
        if path.exists():
            registry[name] = {
                "hash": file_hash(path),
                "last_processed": datetime.now().isoformat()
            }

    save_registry(registry, base_name)


def remove_from_registry(removed_files, base_name):

    registry = load_registry(base_name)

    for name in removed_files:
        registry.pop(name, None)

    save_registry(registry, base_name)