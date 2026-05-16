import json
from pathlib import Path
import os
import errno
import time
import tempfile


# =====================================================
# LOAD JSONL (IGNORA LINHAS INVÁLIDAS)
# =====================================================
def load_valid_jsonl(path: Path):
    if not path.exists():
        return []

    valid = []
    invalid = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                valid.append(json.loads(line))
            except Exception:
                invalid += 1

    if invalid:
        print(f"[WARN] JSONL continha {invalid} linhas invalidas (ignoradas)")

    return valid


# =====================================================
# ESCRITA ATÔMICA (MESMO DISCO)
# =====================================================
def atomic_write_jsonl(path: Path, objects):

    path.parent.mkdir(parents=True, exist_ok=True)

    # cria temp no MESMO diretório → evita WinError 17
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        encoding="utf-8"
    ) as tmp:
        for obj in objects:
            tmp.write(json.dumps(obj, ensure_ascii=False) + "\n")
        tmp_path = tmp.name

    max_attempts = 12
    wait_s = 0.15

    for attempt in range(1, max_attempts + 1):
        try:
            os.replace(tmp_path, path)
            break
        except (PermissionError, OSError) as exc:
            winerror = getattr(exc, "winerror", None)
            err_no = getattr(exc, "errno", None)
            retryable = (
                winerror in {5, 32, 33}
                or err_no in {errno.EACCES, errno.EBUSY}
            )

            if not retryable or attempt == max_attempts:
                raise

            print(
                f"[WARN] Arquivo em uso ao gravar {path.name}. "
                f"Tentativa {attempt}/{max_attempts}; aguardando {wait_s:.2f}s..."
            )
            time.sleep(wait_s)
            wait_s = min(wait_s * 1.7, 2.0)

    # Se o replace falhar, evita arquivo temporario sobrando.
    try:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    except OSError:
        pass


# =====================================================
# APPEND SEGURO
# =====================================================
def safe_append_jsonl(path: Path, new_objects):
    existing = load_valid_jsonl(path)
    existing.extend(new_objects)
    atomic_write_jsonl(path, existing)
