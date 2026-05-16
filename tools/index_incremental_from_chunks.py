import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bm25_store import build_bm25
from src.config import get_chunks_path, get_persistent_data_dir
from src.positional_index import build_positional_index
from src.vector_store import finalize_faiss, index_new_chunks, remove_vectors_by_doc


def _load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_json(path: Path, payload: Dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _build_doc_registry_from_chunks(chunks_path: Path) -> Dict[str, Dict]:
    """
    Gera um fingerprint por documento com base em (chunk_id + hash(text)).
    Ordena por chunk_id para evitar falso positivo por mudanca de ordem no arquivo.
    """
    by_doc: Dict[str, List[Tuple[str, str]]] = {}

    with chunks_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            doc_name = chunk.get("doc_name") or chunk.get("doc")
            chunk_id = chunk.get("chunk_id")
            text = chunk.get("text") or ""

            if not doc_name or not chunk_id:
                continue

            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            by_doc.setdefault(doc_name, []).append((chunk_id, text_hash))

    registry: Dict[str, Dict] = {}

    for doc_name, items in by_doc.items():
        items.sort(key=lambda x: x[0])
        doc_hasher = hashlib.sha256()

        for chunk_id, text_hash in items:
            doc_hasher.update(chunk_id.encode("utf-8"))
            doc_hasher.update(b"\x1f")
            doc_hasher.update(text_hash.encode("utf-8"))
            doc_hasher.update(b"\n")

        registry[doc_name] = {
            "hash": doc_hasher.hexdigest(),
            "chunk_count": len(items),
        }

    return registry


def _diff_doc_registries(
    previous: Dict[str, Dict], current: Dict[str, Dict]
) -> Tuple[List[str], List[str], List[str]]:
    prev_docs = set(previous.keys())
    curr_docs = set(current.keys())

    added = sorted(curr_docs - prev_docs)
    removed = sorted(prev_docs - curr_docs)
    modified = sorted(
        doc for doc in (curr_docs & prev_docs)
        if (previous.get(doc) or {}).get("hash") != (current.get(doc) or {}).get("hash")
    )

    return added, modified, removed


def run(
    base_name: str,
    checkpoint_batches: int,
    batch_retries: int,
    retry_wait_seconds: int,
    skip_vector: bool,
    skip_lexical: bool,
):
    data_dir = get_persistent_data_dir(base_name)
    chunks_path = get_chunks_path(base_name)
    chunks_registry_path = data_dir / "chunks_registry.json"

    if not chunks_path.exists():
        print(f"[ERRO] chunks.jsonl nao encontrado: {chunks_path}")
        return 1

    previous_registry = _load_json(chunks_registry_path)
    current_registry = _build_doc_registry_from_chunks(chunks_path)
    added_docs, modified_docs, removed_docs = _diff_doc_registries(
        previous_registry,
        current_registry,
    )

    changed_docs = sorted(set(added_docs + modified_docs))

    print(f"[REGISTRY CHUNKS] adicionado(s): {len(added_docs)}")
    print(f"[REGISTRY CHUNKS] modificado(s): {len(modified_docs)}")
    print(f"[REGISTRY CHUNKS] removido(s): {len(removed_docs)}")

    if not changed_docs and not removed_docs:
        print("[OK] Nenhuma alteracao detectada no chunks.jsonl.")
        # Mantem registry atualizado mesmo sem mudancas (ex.: primeiro run).
        _save_json(chunks_registry_path, current_registry)
        return 0

    if not skip_vector:
        docs_for_vector_removal = sorted(set(changed_docs + removed_docs))
        if docs_for_vector_removal:
            print(
                f"[VETORES] Removendo vetores de {len(docs_for_vector_removal)} documento(s) alterado(s)/removido(s)."
            )
            remove_vectors_by_doc(
                docs_for_vector_removal,
                base_name,
                for_indexing=True,
            )

        print("[VETORES] Reindexando chunks pendentes...")
        index_new_chunks(
            base_name=base_name,
            checkpoint_every_batches=max(1, int(checkpoint_batches)),
            max_retries_per_batch=max(0, int(batch_retries)),
            retry_sleep_seconds=max(0, int(retry_wait_seconds)),
            publish_faiss_network_checkpoint=True,
        )
        finalize_faiss(base_name)
    else:
        print("[VETORES] Ignorado (--skip-vector).")

    if not skip_lexical:
        print("[LEXICAL] Reconstruindo BM25...")
        build_bm25(base_name)
        print("[LEXICAL] Reconstruindo indice posicional...")
        build_positional_index(base_name)
    else:
        print("[LEXICAL] Ignorado (--skip-lexical).")

    _save_json(chunks_registry_path, current_registry)
    print(f"[OK] Registry de chunks atualizado: {chunks_registry_path}")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Indexacao incremental a partir do chunks.jsonl. "
            "Detecta alteracoes por documento via chunks_registry.json."
        )
    )
    parser.add_argument("--base", required=True, help="Nome da base.")
    parser.add_argument(
        "--checkpoint-batches",
        type=int,
        default=100,
        help="Checkpoint do FAISS a cada N batches (padrao: 100).",
    )
    parser.add_argument(
        "--batch-retries",
        type=int,
        default=5,
        help="Tentativas por batch FAISS em caso de erro (padrao: 5).",
    )
    parser.add_argument(
        "--retry-wait-seconds",
        type=int,
        default=30,
        help="Espera entre tentativas de batch FAISS (padrao: 30s).",
    )
    parser.add_argument(
        "--skip-vector",
        action="store_true",
        help="Nao executa indexacao vetorial.",
    )
    parser.add_argument(
        "--skip-lexical",
        action="store_true",
        help="Nao reconstrui BM25/indice posicional.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    code = run(
        base_name=args.base,
        checkpoint_batches=args.checkpoint_batches,
        batch_retries=args.batch_retries,
        retry_wait_seconds=args.retry_wait_seconds,
        skip_vector=bool(args.skip_vector),
        skip_lexical=bool(args.skip_lexical),
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()

