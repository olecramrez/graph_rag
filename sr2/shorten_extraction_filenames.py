import argparse
import json
from pathlib import Path
from typing import Dict, List

from anm_normativos_downloader import (
    collect_tracking_rows,
    safe_filename,
    unique_path,
    write_rows_csv,
    write_rows_jsonl,
)


def read_jsonl(path: Path) -> List[Dict[str, str]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def rename_if_needed(path_text: str, new_stem: str, suffix: str, max_len: int) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.exists():
        return path_text

    target_name = safe_filename(new_stem, max_len=max_len) + suffix
    target = path.with_name(target_name)
    if path.resolve() == target.resolve():
        return str(path)
    if target.exists():
        target = unique_path(target)
    path.rename(target)
    return str(target)


def build_stem(row: Dict[str, str]) -> str:
    tipo = str(row.get("tipo_norma") or "").strip()
    numero = str(row.get("numero_norma") or "").strip()
    ano = str(row.get("ano_norma") or "").strip()
    titulo = str(row.get("titulo") or row.get("doc_id_curto") or row.get("doc_id") or "").strip()
    if tipo and numero and ano:
        return f"{tipo} {numero}-{ano} {titulo}"
    return titulo


def main() -> int:
    parser = argparse.ArgumentParser(description="Encurta nomes de PDF/HTML de uma extracao existente.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-len", type=int, default=55)
    args = parser.parse_args()

    metadata_jsonl = args.output_dir / "metadados.jsonl"
    rows = read_jsonl(metadata_jsonl)
    if not rows:
        raise SystemExit(f"Nenhum metadados.jsonl encontrado em {metadata_jsonl}")

    for row in rows:
        stem = build_stem(row)
        pdf_path = rename_if_needed(row.get("pdf_path", ""), stem, ".pdf", args.max_len)
        html_path = rename_if_needed(row.get("html_path", ""), stem, ".html", args.max_len)

        if pdf_path:
            pdf = Path(pdf_path)
            row["pdf_path"] = str(pdf)
            row["doc_id"] = pdf.name
            row["doc_id_curto"] = pdf.stem
        if html_path:
            row["html_path"] = html_path

    write_rows_csv(args.output_dir / "metadados.csv", rows)
    write_rows_jsonl(args.output_dir / "metadados.jsonl", rows)

    tracking = collect_tracking_rows(rows)
    write_rows_csv(args.output_dir / "rastreamento_dispositivos.csv", tracking)
    write_rows_jsonl(args.output_dir / "rastreamento_dispositivos.jsonl", tracking)

    print(f"Arquivos atualizados: {len(rows)}")
    print(f"Limite de nome base: {args.max_len}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
