from pathlib import Path
from typing import Optional

from src.config import get_chunks_path, get_persistent_data_dir
from tools.enrich_chunks_metadata import DEFAULT_METADATA_FIELDS, enrich_chunks
from tools.detect_revocation_impacts import (
    detect_revocation_impacts,
    write_revocation_impact_report,
)


METADATA_FILENAMES = ("index.csv", "metadata.csv", "metadados.csv")


def resolve_metadata_csv(base_name: str, metadata_csv: Optional[str] = None) -> Optional[Path]:
    if metadata_csv:
        path = Path(metadata_csv)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise FileNotFoundError(f"Arquivo de metadados nao encontrado: {path}")
        return path

    data_dir = get_persistent_data_dir(base_name)
    for filename in METADATA_FILENAMES:
        candidate = data_dir / filename
        if candidate.exists():
            return candidate

    return None


def enrich_chunks_metadata_if_available(
    base_name: str,
    metadata_csv: Optional[str] = None,
) -> bool:
    metadata_path = resolve_metadata_csv(base_name, metadata_csv)
    if metadata_path is None:
        print(
            "[METADADOS] Nenhum index.csv/metadata.csv/metadados.csv encontrado; "
            "chunks novos ficarao sem metadados normativos enriquecidos."
        )
        return False

    chunks_path = get_chunks_path(base_name)
    changed_path = get_persistent_data_dir(base_name) / "chunks.metadata.changed.jsonl"

    print(f"[METADADOS] Enriquecendo chunks com: {metadata_path}")
    enrich_chunks(
        chunks_path=chunks_path,
        metadata_csv=metadata_path,
        output_path=chunks_path,
        changed_output_path=changed_path,
        metadata_fields=DEFAULT_METADATA_FIELDS,
        create_backup=True,
    )
    print("[METADADOS] Enriquecimento concluido.")
    return True


def write_revocation_impact_report_for_base(base_name: str) -> int:
    chunks_path = get_chunks_path(base_name)
    if not chunks_path.exists():
        return 0

    report_path = get_persistent_data_dir(base_name) / "revocation_impacts.csv"
    rows = detect_revocation_impacts(chunks_path)
    write_revocation_impact_report(rows, report_path)
    print(f"[REVOGACAO] Impactos potenciais detectados: {len(rows)}")
    print(f"[REVOGACAO] Relatorio: {report_path}")
    return len(rows)
