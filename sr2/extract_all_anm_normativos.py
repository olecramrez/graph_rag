import argparse
import csv
import json
import time
from pathlib import Path
from typing import Dict, List

from anm_normativos_downloader import (
    AnmLegisDownloader,
    ActRef,
    collect_tracking_rows,
    write_rows_csv,
    write_rows_jsonl,
)


NORMATIVE_MENUS = [
    ("resolucoes", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=6675&cod_modulo=351"),
    ("portarias", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=6676&cod_modulo=351"),
    ("instrucoes_normativas", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=6677&cod_modulo=351"),
    ("deliberacoes", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=8375&cod_modulo=351"),
    ("decisoes_circuito_deliberativo", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=11109&cod_modulo=351"),
    ("sumulas", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=11110&cod_modulo=351"),
    ("orientacoes_normativas", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=6678&cod_modulo=351"),
    ("circulares", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=6680&cod_modulo=351"),
    ("oficios_circulares", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=7690&cod_modulo=351"),
    ("ordens_servico", "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=abrirResenhaAnoData&cod_menu=8041&cod_modulo=351"),
]


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


def ref_row(ref: ActRef, menu_slug: str) -> Dict[str, str]:
    return {
        "menu_slug": menu_slug,
        "ref_key": ref.key,
        "tipo_norma": ref.tipo,
        "numero_norma": ref.numero,
        "ano_norma": ref.ano,
        "seq_ato": ref.sequencia,
        "orgao": ref.orgao,
        "source_url": ref.public_url(),
        "source_list_url": ref.list_url,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extrai todos os tipos normativos principais da ANMlegis.")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "extracao_anm_completa")
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--limit", type=int, help="Limite total para teste.")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--flush-every", type=int, default=25)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    downloader = AnmLegisDownloader(out, sleep_seconds=args.sleep, timeout=args.timeout)

    discovered: Dict[str, Dict[str, str]] = {}
    refs_by_key: Dict[str, ActRef] = {}

    print("[1/2] Descobrindo atos...")
    for menu_slug, menu_url in NORMATIVE_MENUS:
        refs = downloader.discover_refs([menu_url])
        print(f"  {menu_slug}: {len(refs)}")
        for ref in refs:
            row = ref_row(ref, menu_slug)
            discovered[row["ref_key"]] = row
            refs_by_key[row["ref_key"]] = ref

    discovered_rows = list(discovered.values())
    discovered_rows.sort(key=lambda row: (row["menu_slug"], row["ano_norma"], row["numero_norma"], row["seq_ato"]))
    write_rows_csv(out / "atos_descobertos.csv", discovered_rows)
    write_rows_jsonl(out / "atos_descobertos.jsonl", discovered_rows)
    print(f"Atos descobertos: {len(discovered_rows)}")

    if args.discover_only:
        return 0

    metadata_path = out / "metadados.jsonl"
    existing_rows = read_jsonl(metadata_path)
    done = {row.get("source_url") for row in existing_rows if row.get("source_url")}
    rows = list(existing_rows)
    errors: List[Dict[str, str]] = read_jsonl(out / "erros.jsonl")

    selected = [refs_by_key[row["ref_key"]] for row in discovered_rows]
    if args.limit:
        selected = selected[: args.limit]

    print("[2/2] Baixando PDFs e extraindo metadados...")
    print(f"Ja processados: {len(done)}")
    total = len(selected)
    processed_now = 0

    for index, ref in enumerate(selected, start=1):
        if ref.public_url() in done:
            continue
        label = f"{ref.tipo} {ref.numero}/{ref.ano} {ref.orgao}"
        try:
            print(f"[{index}/{total}] {label}")
            row = downloader.download_one(ref, skip_existing=True)
            menu_slug = discovered.get(ref.key, {}).get("menu_slug", "")
            row["menu_slug"] = menu_slug
            rows.append(row)
            done.add(row.get("source_url") or ref.public_url())
            append_jsonl(metadata_path, row)
            processed_now += 1
        except Exception as exc:
            err = {
                **ref_row(ref, discovered.get(ref.key, {}).get("menu_slug", "")),
                "erro": str(exc),
                "erro_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            errors.append(err)
            append_jsonl(out / "erros.jsonl", err)
            print(f"  ERRO: {exc}")

        if processed_now and processed_now % args.flush_every == 0:
            write_rows_csv(out / "metadados.csv", rows)
            tracking = collect_tracking_rows(rows)
            write_rows_csv(out / "rastreamento_dispositivos.csv", tracking)
            write_rows_jsonl(out / "rastreamento_dispositivos.jsonl", tracking)
            write_rows_csv(out / "erros.csv", errors)
            print(f"  checkpoint: {len(rows)} metadados, {len(errors)} erros")

    write_rows_csv(out / "metadados.csv", rows)
    tracking_rows = collect_tracking_rows(rows)
    write_rows_csv(out / "rastreamento_dispositivos.csv", tracking_rows)
    write_rows_jsonl(out / "rastreamento_dispositivos.jsonl", tracking_rows)
    write_rows_csv(out / "erros.csv", errors)

    print(f"Concluido. Metadados: {len(rows)} | Rastreamento: {len(tracking_rows)} | Erros: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
