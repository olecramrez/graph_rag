import json
import shutil
import errno
import time
import os
from typing import Callable, Optional

import faiss
import numpy as np

from .lia_client import get_embedding_batch
from .config import (
    get_chunks_path,
    get_faiss_path,
    get_faiss_fallback_path,
    get_faiss_temp_path,
    get_idmap_path,
)


def _configure_index_search(index):
    if isinstance(index, faiss.IndexIDMap):
        base = index.index
    else:
        base = index

    if hasattr(base, "hnsw"):
        base.hnsw.efSearch = 64


def _create_hnsw_index(dim=1536):
    M = 32
    base = faiss.IndexHNSWFlat(dim, M)
    base.hnsw.efConstruction = 200
    base.hnsw.efSearch = 64
    return faiss.IndexIDMap(base)


def _is_no_space_error(exc: OSError) -> bool:
    return (
        getattr(exc, "errno", None) == errno.ENOSPC
        or getattr(exc, "winerror", None) == 112
    )


def _same_path(a, b):
    a_s = os.path.normcase(os.path.normpath(str(a)))
    b_s = os.path.normcase(os.path.normpath(str(b)))

    if a_s == b_s:
        return True

    # Em Windows, Z:\... e \\servidor\share\... podem apontar para o mesmo
    # arquivo; samefile evita falso negativo por alias de caminho.
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _network_fallback_enabled():
    return (os.getenv("RAG_DISABLE_FAISS_NETWORK_FALLBACK", "0") != "1")


def _get_active_faiss_work_path(base_name):
    primary = get_faiss_temp_path(base_name)
    fallback = get_faiss_fallback_path(base_name)
    use_fallback = _network_fallback_enabled()

    if primary.exists():
        return primary
    if use_fallback and fallback.exists():
        return fallback
    return primary


def _publish_faiss_checkpoint_to_network(base_name):
    source_path = _get_active_faiss_work_path(base_name)
    network_path = get_faiss_path(base_name)

    if not source_path.exists():
        return False

    network_path.parent.mkdir(parents=True, exist_ok=True)

    if _same_path(source_path, network_path):
        return True

    temp_path = network_path.with_name(network_path.name + ".tmp")

    try:
        shutil.copy2(source_path, temp_path)
        os.replace(temp_path, network_path)
        return True
    except Exception as e:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

        print(f"[WARN] Falha ao publicar checkpoint FAISS na rede: {e}")
        return False


def ensure_local_faiss_workfile(base_name):
    local_path = get_faiss_temp_path(base_name)
    fallback_path = get_faiss_fallback_path(base_name)
    network_path = get_faiss_path(base_name)
    use_fallback = _network_fallback_enabled()

    if local_path.exists():
        return local_path
    if use_fallback and fallback_path.exists():
        return fallback_path

    local_path.parent.mkdir(parents=True, exist_ok=True)
    if use_fallback:
        fallback_path.parent.mkdir(parents=True, exist_ok=True)

    if network_path.exists():
        if _same_path(network_path, local_path):
            return local_path

        try:
            shutil.copy2(network_path, local_path)
            print("[FAISS] Copia inicial da rede para workspace local concluida.")
            return local_path
        except OSError as e:
            if not _is_no_space_error(e) or not use_fallback:
                raise

            print("[WARN] Sem espaco no workspace local para FAISS; usando fallback na rede.")
            if _same_path(network_path, fallback_path):
                return fallback_path

            shutil.copy2(network_path, fallback_path)
            return fallback_path

    return local_path


# =====================================================
# LOAD FAISS
# =====================================================

def load_faiss(base_name, dim=1536, for_indexing=False):
    if for_indexing:
        faiss_path = ensure_local_faiss_workfile(base_name)
    else:
        faiss_path = get_faiss_path(base_name)

    if faiss_path.exists():
        index = faiss.read_index(str(faiss_path))
        _configure_index_search(index)
        return index

    return _create_hnsw_index(dim)


# =====================================================
# SAVE FAISS
# =====================================================

def save_faiss(index, base_name, for_indexing=False):
    use_fallback = _network_fallback_enabled()

    if for_indexing:
        primary_path = get_faiss_temp_path(base_name)
        fallback_path = get_faiss_fallback_path(base_name)
        faiss_path = _get_active_faiss_work_path(base_name)
    else:
        faiss_path = get_faiss_path(base_name)

    faiss_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        faiss.write_index(index, str(faiss_path))
    except OSError as e:
        if (
            not for_indexing
            or not _is_no_space_error(e)
            or not use_fallback
        ):
            raise

        if _same_path(faiss_path, fallback_path):
            raise

        print("[WARN] Sem espaco no workspace local durante checkpoint; gravando FAISS no fallback da rede.")
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(fallback_path))

        # Remove arquivo parcial no caminho primario, se houver.
        if primary_path.exists() and not _same_path(primary_path, fallback_path):
            try:
                primary_path.unlink()
            except Exception:
                pass


def finalize_faiss(base_name):
    local_path = get_faiss_temp_path(base_name)
    fallback_path = get_faiss_fallback_path(base_name)
    network_path = get_faiss_path(base_name)
    use_fallback = _network_fallback_enabled()

    source_path = None
    if local_path.exists():
        source_path = local_path
    elif use_fallback and fallback_path.exists():
        source_path = fallback_path

    if source_path is None:
        return False

    network_path.parent.mkdir(parents=True, exist_ok=True)

    if _same_path(source_path, network_path):
        print("[FAISS] faiss.index final ja esta na rede.")
        return True

    # Publicacao segura: copia para temporario e replace atomico.
    temp_path = network_path.with_name(network_path.name + ".final.tmp")
    try:
        shutil.copy2(source_path, temp_path)
        os.replace(temp_path, network_path)
    except Exception as e:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        print(f"[WARN] Falha ao publicar faiss.index final na rede: {e}")
        return False

    # Remove a origem apenas apos publicar com sucesso.
    try:
        if source_path.exists() and not _same_path(source_path, network_path):
            source_path.unlink()
    except Exception as e:
        print(f"[WARN] FAISS publicado, mas nao foi possivel limpar origem local: {e}")

    print("[FAISS] faiss.index final publicado na rede.")

    # Limpa eventual arquivo de fallback remanescente.
    if fallback_path.exists() and not _same_path(fallback_path, network_path):
        try:
            fallback_path.unlink()
        except Exception:
            pass

    return True


# =====================================================
# LOAD IDMAP
# =====================================================

def load_idmap(base_name):
    idmap_path = get_idmap_path(base_name)

    if not idmap_path.exists():
        return {}

    return json.loads(idmap_path.read_text(encoding="utf-8"))


# =====================================================
# SAVE IDMAP
# =====================================================

def save_idmap(idmap, base_name):
    idmap_path = get_idmap_path(base_name)
    idmap_path.parent.mkdir(parents=True, exist_ok=True)
    idmap_path.write_text(
        json.dumps(idmap, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def reset_vector_index(base_name, dim=1536):
    index = _create_hnsw_index(dim)
    save_faiss(index, base_name, for_indexing=True)
    save_idmap({}, base_name)
    print("[FAISS] Indice vetorial reiniciado para reindexacao completa.")


def _run_checkpoint(
    index,
    idmap,
    base_name,
    callback: Optional[Callable[[], None]],
    publish_faiss_network_checkpoint=False,
):
    save_faiss(index, base_name, for_indexing=True)
    save_idmap(idmap, base_name)

    if publish_faiss_network_checkpoint:
        _publish_faiss_checkpoint_to_network(base_name)

    if callback is not None:
        callback()


# =====================================================
# INDEXAR NOVOS CHUNKS
# =====================================================

def index_new_chunks(
    base_name,
    batch_size=25,
    checkpoint_every_batches=100,
    max_retries_per_batch=5,
    retry_sleep_seconds=30,
    publish_faiss_network_checkpoint=True,
    checkpoint_callback: Optional[Callable[[], None]] = None,
):
    chunks_path = get_chunks_path(base_name)

    if not chunks_path.exists():
        print("[WARN] chunks.jsonl nao encontrado.")
        return

    work_path = ensure_local_faiss_workfile(base_name)
    faiss_work_exists = work_path.exists()
    index = load_faiss(base_name, for_indexing=True)
    idmap = load_idmap(base_name)

    # Auto-recuperacao: se o FAISS sumiu, mas o id_map ficou, recria do zero.
    if not faiss_work_exists and idmap:
        print(
            "[WARN] id_map encontrado sem FAISS correspondente. "
            "Reiniciando id_map para reconstruir vetores."
        )
        idmap = {}
        save_idmap(idmap, base_name)

    # Auto-recuperacao: se FAISS existe com vetores e o id_map sumiu,
    # reinicia o FAISS para reconstruir de forma consistente.
    if getattr(index, "ntotal", 0) > 0 and not idmap:
        print(
            "[WARN] FAISS encontrado sem id_map correspondente. "
            "Reiniciando FAISS para reconstruir vetores."
        )
        index = _create_hnsw_index()
        save_faiss(index, base_name, for_indexing=True)

    next_faiss_id = max(idmap.values(), default=0)

    new_chunks = []

    with open(chunks_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            chunk_id = chunk.get("chunk_id")

            if chunk_id and chunk_id not in idmap:
                new_chunks.append(chunk)

    if not new_chunks:
        print("[OK] Nenhum novo chunk para indexar.")
        return

    total_batches = (len(new_chunks) + batch_size - 1) // batch_size
    print(
        f"{len(new_chunks)} novos chunks para indexar "
        f"({total_batches} batches)."
    )

    batches_since_checkpoint = 0

    for i in range(0, len(new_chunks), batch_size):
        batch = new_chunks[i:i + batch_size]
        batch_no = i // batch_size + 1
        retries = 0

        while True:
            pending_batch = [
                chunk for chunk in batch
                if chunk.get("chunk_id") not in idmap
            ]

            if not pending_batch:
                pct = (batch_no / total_batches * 100) if total_batches else 100.0
                print(f"[PROGRESS] batch {batch_no}/{total_batches} ({pct:.1f}%)")
                break

            try:
                texts = [c["text"] for c in pending_batch]
                embeddings = get_embedding_batch(texts)

                vectors = np.array(embeddings, dtype="float32")
                ids = np.arange(
                    next_faiss_id + 1,
                    next_faiss_id + 1 + len(pending_batch),
                    dtype="int64",
                )

                index.add_with_ids(vectors, ids)

                for chunk, fid in zip(pending_batch, ids.tolist()):
                    idmap[chunk["chunk_id"]] = int(fid)

                if len(ids):
                    next_faiss_id = int(ids[-1])

                pct = (batch_no / total_batches * 100) if total_batches else 100.0
                print(f"[PROGRESS] batch {batch_no}/{total_batches} ({pct:.1f}%)")
                break

            except KeyboardInterrupt:
                raise
            except Exception as e:
                retries += 1

                if retries > max_retries_per_batch:
                    raise RuntimeError(
                        f"Falha no batch {batch_no}/{total_batches} apos "
                        f"{max_retries_per_batch} tentativas."
                    ) from e

                # Preserva estado consistente antes de aguardar retry.
                _run_checkpoint(
                    index,
                    idmap,
                    base_name,
                    checkpoint_callback,
                    publish_faiss_network_checkpoint=publish_faiss_network_checkpoint,
                )

                wait_s = max(0, int(retry_sleep_seconds))
                print(
                    f"[RETRY FAISS] batch {batch_no}/{total_batches} falhou "
                    f"(tentativa {retries}/{max_retries_per_batch}). "
                    f"Aguardando {wait_s}s..."
                )

                if wait_s > 0:
                    time.sleep(wait_s)

        batches_since_checkpoint += 1
        if batches_since_checkpoint >= checkpoint_every_batches:
            _run_checkpoint(
                index,
                idmap,
                base_name,
                checkpoint_callback,
                publish_faiss_network_checkpoint=publish_faiss_network_checkpoint,
            )
            print(
                f"[CHECKPOINT FAISS] Estado persistido em "
                f"{batch_no}/{total_batches} batches ({pct:.1f}%)."
            )
            batches_since_checkpoint = 0

    _run_checkpoint(
        index,
        idmap,
        base_name,
        checkpoint_callback,
        publish_faiss_network_checkpoint=publish_faiss_network_checkpoint,
    )
    print("[CHECKPOINT] Persistencia final concluida.")


# =====================================================
# REMOVER VETORES POR DOCUMENTO
# =====================================================

def remove_vectors_by_doc(removed_docs, base_name, for_indexing=False):
    if not removed_docs:
        return

    if for_indexing:
        faiss_path = ensure_local_faiss_workfile(base_name)
    else:
        faiss_path = get_faiss_path(base_name)

    if not faiss_path.exists():
        return

    index = load_faiss(base_name, for_indexing=for_indexing)
    idmap = load_idmap(base_name)

    removed_docs = tuple(removed_docs)
    to_remove = [
        fid for chunk_id, fid in idmap.items()
        if chunk_id.startswith(removed_docs)
    ]

    if not to_remove:
        print("[OK] Nenhum vetor para remover.")
        return

    index.remove_ids(np.array(to_remove, dtype="int64"))

    idmap = {
        cid: fid for cid, fid in idmap.items()
        if fid not in to_remove
    }

    save_faiss(index, base_name, for_indexing=for_indexing)
    save_idmap(idmap, base_name)

    print("[OK] Vetores removidos.")
