import os
import argparse

# Alternativa fixa no C:: ignora override de workspace FAISS por ambiente.
os.environ.pop("RAG_FAISS_WORK_ROOT", None)
os.environ["RAG_DISABLE_FAISS_NETWORK_FALLBACK"] = "1"

from src.config import (
    get_available_bases,
    get_default_base,
    get_documents_dir,
    get_persistent_data_dir,
    get_faiss_temp_path,
)
from src.indexer import (
    scan_documents,
    update_registry,
    remove_from_registry,
)
from src.processor import (
    process_document_with_checkpoint,
    clear_chunk_checkpoint_for_docs,
    remove_chunks,
)
from src.metadata_enrichment import (
    enrich_chunks_metadata_if_available,
    write_revocation_impact_report_for_base,
)
from src.vector_store import (
    index_new_chunks,
    remove_vectors_by_doc,
    finalize_faiss,
    reset_vector_index,
)
from src.bm25_store import build_bm25
from src.positional_index import build_positional_index


# =====================================================
# EXECUTA INDEXACAO DE UMA BASE
# =====================================================

def run_index_for_base(
    base_name,
    checkpoint_every_batches=100,
    batch_retries=5,
    retry_wait_seconds=30,
    checkpoint_faiss_network=True,
    metadata_csv=None,
    rebuild_index=False,
):
    source_dir = get_documents_dir(base_name)
    persistent_data_dir = get_persistent_data_dir(base_name)
    local_faiss_path = get_faiss_temp_path(base_name)

    print(f"\n===== BASE ATIVA: {base_name} =====")
    print(f"PDFs (rede): {source_dir}")
    print(f"Dados persistentes (rede): {persistent_data_dir}")
    print(f"FAISS temporario (local): {local_faiss_path}")

    new_files, modified_files, removed_files = scan_documents(base_name)
    to_process = new_files + modified_files

    if to_process:
        print(f"\n[DOCS] {len(to_process)} arquivo(s) para processar.")
        # Remove vetores antigos antes do reprocessamento (novos/modificados).
        remove_vectors_by_doc(to_process, base_name, for_indexing=True)
    if removed_files:
        print(f"[DOCS] {len(removed_files)} arquivo(s) removido(s).")

    total_docs = len(to_process)
    for doc_idx, fname in enumerate(to_process, start=1):
        doc_pct = (doc_idx / total_docs * 100) if total_docs else 100.0
        print(f"[PROGRESS] documento {doc_idx}/{total_docs} ({doc_pct:.1f}%) | {fname}")
        process_document_with_checkpoint(
            fname,
            base_name,
            checkpoint_every_pages=100,
            progress_every_pages=0,
        )

        # Persistencia incremental de progresso por documento:
        # em caso de falha, evita reprocessamento desnecessario.
        update_registry([fname], base_name)

    if removed_files:
        remove_chunks(removed_files, base_name)
        remove_vectors_by_doc(removed_files, base_name, for_indexing=True)
        clear_chunk_checkpoint_for_docs(removed_files, base_name)
        remove_from_registry(removed_files, base_name)

    enrich_chunks_metadata_if_available(base_name, metadata_csv=metadata_csv)
    write_revocation_impact_report_for_base(base_name)

    if rebuild_index:
        reset_vector_index(base_name)

    # Sempre tenta indexar pendencias de chunks para permitir retomada
    # apos falhas sem depender da lista to_process da execucao atual.
    index_new_chunks(
        base_name,
        checkpoint_every_batches=checkpoint_every_batches,
        max_retries_per_batch=batch_retries,
        retry_sleep_seconds=retry_wait_seconds,
        publish_faiss_network_checkpoint=checkpoint_faiss_network,
    )

    print("\nReconstruindo BM25...")
    build_bm25(base_name)

    print("\nConstruindo indice lexical avancado...")
    build_positional_index(base_name)

    moved = finalize_faiss(base_name)
    if not moved:
        print("[WARN] Nenhum faiss.index local para publicar.")

    print("\n[OK] Indexacao concluida.")
    print()


# =====================================================
# MAIN
# =====================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=str, help="Nome da base")
    parser.add_argument("--all", action="store_true", help="Indexar todas as bases")
    parser.add_argument(
        "--checkpoint-batches",
        type=int,
        default=100,
        help="Checkpoint do FAISS a cada N batches (padrao: 100)",
    )
    parser.add_argument(
        "--batch-retries",
        type=int,
        default=5,
        help="Tentativas por batch FAISS em caso de erro (padrao: 5)",
    )
    parser.add_argument(
        "--retry-wait-seconds",
        type=int,
        default=30,
        help="Espera entre tentativas de batch FAISS, em segundos (padrao: 30)",
    )
    parser.add_argument(
        "--checkpoint-faiss-network",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Publica checkpoint do FAISS na rede a cada checkpoint "
            "(padrao: ligado)"
        ),
    )
    parser.add_argument(
        "--metadata-csv",
        help=(
            "Caminho opcional para index.csv/metadata.csv de metadados. "
            "Se omitido, procura esses arquivos na pasta data da base."
        ),
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help=(
            "Recria FAISS/id_map do zero a partir do chunks.jsonl atual. "
            "Use apos enriquecimento de metadados ou troca ampla de chunks."
        ),
    )
    args = parser.parse_args()

    if args.all:
        bases = get_available_bases()

        if not bases:
            print("[WARN] Nenhuma base encontrada.")
            return

        print("\n===== INDEXACAO DE TODAS AS BASES =====")

        for base_name in bases:
            run_index_for_base(
                base_name,
                checkpoint_every_batches=max(1, args.checkpoint_batches),
                batch_retries=max(0, args.batch_retries),
                retry_wait_seconds=max(0, args.retry_wait_seconds),
                checkpoint_faiss_network=bool(args.checkpoint_faiss_network),
                metadata_csv=args.metadata_csv,
                rebuild_index=bool(args.rebuild_index),
            )

        print("\n===== TODAS AS BASES INDEXADAS =====\n")
        return

    base_name = args.base or get_default_base()
    run_index_for_base(
        base_name,
        checkpoint_every_batches=max(1, args.checkpoint_batches),
        batch_retries=max(0, args.batch_retries),
        retry_wait_seconds=max(0, args.retry_wait_seconds),
        checkpoint_faiss_network=bool(args.checkpoint_faiss_network),
        metadata_csv=args.metadata_csv,
        rebuild_index=bool(args.rebuild_index),
    )


if __name__ == "__main__":
    main()
