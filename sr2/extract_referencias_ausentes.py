import argparse
import csv
import json
import time
from pathlib import Path
from typing import Dict, List

from anm_normativos_downloader import (
    ActRef,
    AnmLegisDownloader,
    act_ref_from_url,
    collect_tracking_rows,
    write_rows_csv,
    write_rows_jsonl,
)


SKIP_TIPOS_DEFAULT = {"EMC", "ECR"}


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    rows = []
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


def append_jsonl(path: Path, row: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def first_url(value: str) -> str:
    return (value or "").split(" | ")[0].strip()


def ref_row(ref: ActRef, source_report_row: Dict[str, str]) -> Dict[str, str]:
    return {
        "collection_slug": "referencias_ausentes_sem_emendas",
        "ref_key": ref.key,
        "tipo_norma": ref.tipo,
        "numero_norma": ref.numero,
        "ano_norma": ref.ano,
        "seq_ato": ref.sequencia,
        "orgao": ref.orgao,
        "source_url": ref.public_url(),
        "source_list_url": source_report_row.get("urls_referencia", ""),
        "qtd_eventos_origem": source_report_row.get("qtd_eventos", ""),
        "textos_referencia_origem": source_report_row.get("textos_referencia", ""),
    }


def load_missing_refs(report_path: Path, skip_tipos: set[str]) -> List[tuple[ActRef, Dict[str, str]]]:
    refs: Dict[str, tuple[ActRef, Dict[str, str]]] = {}
    for row in read_csv(report_path):
        tipo = (row.get("tipo_norma") or "").upper()
        if not tipo or tipo in skip_tipos:
            continue
        url = first_url(row.get("urls_referencia", ""))
        ref = act_ref_from_url(url, list_url=url, list_title=row.get("textos_referencia", ""))
        if not ref:
            continue
        refs[ref.key] = (ref, row)
    return sorted(
        refs.values(),
        key=lambda item: (-(int(item[1].get("qtd_eventos") or 0)), item[0].tipo, item[0].ano, item[0].numero),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Baixa referencias ausentes, ignorando emendas constitucionais.")
    parser.add_argument("--report", type=Path, default=Path(__file__).parent / "relatorio_referencias_ausentes.csv")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "extracao_referencias_ausentes")
    parser.add_argument("--skip-tipo", action="append", default=sorted(SKIP_TIPOS_DEFAULT))
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--discover-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    skip_tipos = {item.upper() for item in (args.skip_tipo or [])}
    selected = load_missing_refs(args.report, skip_tipos)
    if args.limit:
        selected = selected[: args.limit]

    discovered_rows = [ref_row(ref, source_row) for ref, source_row in selected]
    write_rows_csv(out / "atos_descobertos.csv", discovered_rows)
    write_rows_jsonl(out / "atos_descobertos.jsonl", discovered_rows)
    print(f"Referencias selecionadas: {len(selected)} | tipos ignorados: {', '.join(sorted(skip_tipos))}")

    if args.discover_only:
        return 0

    downloader = AnmLegisDownloader(out, sleep_seconds=args.sleep, timeout=args.timeout)
    metadata_path = out / "metadados.jsonl"
    rows = read_jsonl(metadata_path)
    done = {row.get("source_url") for row in rows if row.get("source_url")}
    errors = read_jsonl(out / "erros.jsonl")

    total = len(selected)
    for index, (ref, source_row) in enumerate(selected, start=1):
        if ref.public_url() in done:
            continue
        try:
            print(f"[{index}/{total}] {ref.tipo} {ref.numero}/{ref.ano} {ref.orgao}")
            row = downloader.download_one(ref, skip_existing=True)
            row["collection_slug"] = "referencias_ausentes_sem_emendas"
            row["qtd_eventos_origem"] = source_row.get("qtd_eventos", "")
            row["textos_referencia_origem"] = source_row.get("textos_referencia", "")
            rows.append(row)
            done.add(row.get("source_url") or ref.public_url())
            append_jsonl(metadata_path, row)
        except Exception as exc:
            err = {
                **ref_row(ref, source_row),
                "erro": str(exc),
                "erro_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            errors.append(err)
            append_jsonl(out / "erros.jsonl", err)
            print(f"  ERRO: {exc}")
        time.sleep(args.sleep)

    write_rows_csv(out / "metadados.csv", rows)
    tracking_rows = collect_tracking_rows(rows)
    write_rows_csv(out / "rastreamento_dispositivos.csv", tracking_rows)
    write_rows_jsonl(out / "rastreamento_dispositivos.jsonl", tracking_rows)
    write_rows_csv(out / "erros.csv", errors)
    print(f"Concluido. Metadados: {len(rows)} | Rastreamento: {len(tracking_rows)} | Erros: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
