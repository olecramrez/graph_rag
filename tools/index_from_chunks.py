import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bm25_store import build_bm25
from src.positional_index import build_positional_index
from src.vector_store import index_new_chunks, finalize_faiss


def run(
    base_name: str,
    do_vector: bool,
    checkpoint_batches: int,
    batch_retries: int,
    retry_wait_seconds: int,
):
    print(f"=== BASE: {base_name} ===")

    if do_vector:
        try:
            print("=== INDEXACAO VETORIAL (FAISS) ===")
            index_new_chunks(
                base_name=base_name,
                checkpoint_every_batches=max(1, int(checkpoint_batches)),
                max_retries_per_batch=max(0, int(batch_retries)),
                retry_sleep_seconds=max(0, int(retry_wait_seconds)),
                publish_faiss_network_checkpoint=True,
            )
            finalize_faiss(base_name)
        except Exception as exc:
            print(f"[WARN] Falha na indexacao vetorial: {exc}")
            print("[WARN] Seguindo com indexacao lexical (BM25 + posicional).")
    else:
        print("=== INDEXACAO VETORIAL: ignorada (--skip-vector) ===")

    print("=== BM25 ===")
    build_bm25(base_name)

    print("=== INDICE POSICIONAL ===")
    build_positional_index(base_name)

    print("=== OK ===")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Indexa uma base a partir do chunks.jsonl existente, sem ler PDFs. "
            "Pode rodar com ou sem indexacao vetorial."
        )
    )
    parser.add_argument("--base", required=True, help="Nome da base.")
    parser.add_argument(
        "--skip-vector",
        action="store_true",
        help="Nao executa indexacao vetorial (FAISS/LIA).",
    )
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
    return parser.parse_args()


def main():
    args = parse_args()
    run(
        base_name=args.base,
        do_vector=not args.skip_vector,
        checkpoint_batches=args.checkpoint_batches,
        batch_retries=args.batch_retries,
        retry_wait_seconds=args.retry_wait_seconds,
    )


if __name__ == "__main__":
    main()
