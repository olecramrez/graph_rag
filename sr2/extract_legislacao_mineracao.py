import argparse
import hashlib
import html
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Set
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from anm_normativos_downloader import (
    ActRef,
    AnmLegisDownloader,
    DEFAULT_USER_AGENT,
    apply_pdf_publication_fallback,
    collect_tracking_rows,
    decode_response,
    extract_metadata,
    extract_pdf_text,
    extract_public_act_refs,
    parse_any_date,
    safe_filename,
    sha256_bytes,
    strip_tags,
    write_rows_csv,
    write_rows_jsonl,
)


THEMATIC_ROOTS = [
    (
        "legislacao_consolidada_mineracao_ano",
        "Legislacao Consolidada de Mineracao",
        "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=recuperarTematicasTitulo&cod_menu=11114&cod_modulo=405",
    ),
    (
        "codigo_mineracao_guia_minerador",
        "Codigo de Mineracao e Guia do Minerador",
        "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=recuperarTematicasCollapse&cod_menu=8988&cod_modulo=405",
    ),
    (
        "normas_reguladoras_mineracao",
        "Normas Reguladoras de Mineracao",
        "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=recuperarTematicasCollapse&cod_menu=6710&cod_modulo=351",
    ),
    (
        "legislacao_estruturante_anm",
        "Legislacao Estruturante da ANM",
        "https://anmlegis.datalegis.net/action/ActionDatalegis.php?acao=recuperarTematicasCollapse&cod_menu=6588&cod_modulo=351",
    ),
]

TARGET_MENUS = {"11114", "8988", "6710", "6588"}
CONSTITUTION_HTML_URL = "https://www.planalto.gov.br/ccivil_03/constituicao/ConstituicaoCompilado.htm"
CONSTITUTION_PDF_URL = "https://www.planalto.gov.br/ccivil_03/Constituicao/DOUconstituicao88.pdf"


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


def query_value(url: str, key: str) -> str:
    query = parse_qs(urlparse(html.unescape(url)).query, keep_blank_values=True)
    values = query.get(key) or [""]
    return values[0]


def is_target_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if host and "anmlegis.datalegis.net" not in host:
        return False
    cod_menu = query_value(url, "cod_menu")
    return not cod_menu or cod_menu in TARGET_MENUS


def extract_thematic_links(page_html: str, page_url: str) -> List[str]:
    links: Dict[str, str] = {}
    for raw_href in re.findall(r'href=["\']([^"\']+)["\']', page_html, flags=re.I):
        if not re.search(r"(recuperarTematicas|abrirVinculos)", raw_href, flags=re.I):
            continue
        url = urljoin(page_url, html.unescape(raw_href))
        if is_target_url(url):
            links[url] = url
    for raw_url in re.findall(r"(?:/action/)?TematicaAction\.php\?acao=abrirVinculos[^'\"<>\s]+", page_html, flags=re.I):
        url = urljoin(page_url, "/" + raw_url.lstrip("/"))
        if is_target_url(url):
            links[url] = url

    cod_menu = query_value(page_url, "cod_menu")
    cod_modulo = query_value(page_url, "cod_modulo")
    if cod_menu in TARGET_MENUS and cod_modulo:
        for code in re.findall(r'\bcodigo=["\'](\d+)["\']', page_html, flags=re.I):
            params = {
                "acao": "montarEstruturaTitulos",
                "cotematica": code,
                "cod_menu": cod_menu,
                "cod_modulo": cod_modulo,
                "notematica": "",
                "nivel": "1",
                "infiltro": "",
            }
            links["https://anmlegis.datalegis.net/action/TematicaAction.php?" + urlencode(params)] = ""
    return list(links)


def ref_row(ref: ActRef, collection_slug: str, collection_title: str) -> Dict[str, str]:
    return {
        "collection_slug": collection_slug,
        "collection_title": collection_title,
        "ref_key": ref.key,
        "tipo_norma": ref.tipo,
        "numero_norma": ref.numero,
        "ano_norma": ref.ano,
        "seq_ato": ref.sequencia,
        "orgao": ref.orgao,
        "source_url": ref.public_url(),
        "source_list_url": ref.list_url,
    }


def discover_thematic_refs(downloader: AnmLegisDownloader, max_pages: int) -> Dict[str, Dict[str, object]]:
    refs: Dict[str, Dict[str, object]] = {}
    visited: Set[str] = set()
    queue: List[tuple[str, str, str]] = [(slug, title, url) for slug, title, url in THEMATIC_ROOTS]

    while queue and len(visited) < max_pages:
        slug, title, url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            page_html = downloader.get_text(url)
        except Exception as exc:
            print(f"  aviso: falha ao abrir pagina tematica: {url} ({exc})")
            continue

        for ref in extract_public_act_refs(page_html, url, title):
            if ref.key in refs:
                current = refs[ref.key]
                slugs = str(current["collection_slug"]).split(";")
                titles = str(current["collection_title"]).split(";")
                if slug not in slugs:
                    slugs.append(slug)
                    titles.append(title)
                    current["collection_slug"] = ";".join(slugs)
                    current["collection_title"] = ";".join(titles)
            else:
                refs[ref.key] = {"ref": ref, "collection_slug": slug, "collection_title": title}

        for link in extract_thematic_links(page_html, url):
            if link not in visited:
                queue.append((slug, title, link))

        time.sleep(downloader.sleep_seconds)

    print(f"Paginas tematicas visitadas: {len(visited)}")
    return refs


def download_constitution(output_dir: Path, session: requests.Session) -> Dict[str, str]:
    html_dir = output_dir / "html"
    pdf_dir = output_dir / "pdf"
    html_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    html_response = session.get(CONSTITUTION_HTML_URL, timeout=90)
    html_response.raise_for_status()
    html_text = decode_response(html_response)
    pdf_response = session.get(CONSTITUTION_PDF_URL, timeout=90)
    pdf_response.raise_for_status()
    if not pdf_response.content.startswith(b"%PDF"):
        raise RuntimeError("Resposta da Constituicao nao parece PDF.")

    html_path = html_dir / "Constituicao_Republica_Federativa_Brasil_1988.html"
    pdf_path = pdf_dir / "Constituicao_Republica_Federativa_Brasil_1988.pdf"
    html_path.write_text(html_text, encoding="utf-8")
    pdf_path.write_bytes(pdf_response.content)

    plain = strip_tags(html_text)
    title = "CONSTITUICAO DA REPUBLICA FEDERATIVA DO BRASIL DE 1988"
    promulgation = parse_any_date("5 de outubro de 1988") or "1988-10-05"
    return {
        "doc_id": pdf_path.name,
        "doc_id_curto": pdf_path.stem,
        "classe_documental": "normativo",
        "tipo_documento": "CF",
        "tipo_norma": "CF",
        "numero_norma": "1988",
        "ano_norma": "1988",
        "seq_ato": "000",
        "orgao": "ASSEMBLEIA NACIONAL CONSTITUINTE",
        "titulo": title,
        "ementa": "Constituicao da Republica Federativa do Brasil.",
        "data_assinatura": promulgation,
        "data_publicacao": promulgation,
        "data_inicio_vigencia": promulgation,
        "data_fim_vigencia": "",
        "status_normativo": "vigente",
        "tipo_revogacao": "",
        "revogado_por": "",
        "revogado_por_data": "",
        "vacatio_dias": "0",
        "vigencia_regra": "entrada_imediata",
        "vigencia_trecho": "Promulgada em 5 de outubro de 1988.",
        "data_publicacao_fonte": "planalto_pdf",
        "tem_revogacao_parcial_dispositivo": "false",
        "source_url": CONSTITUTION_HTML_URL,
        "source_list_url": CONSTITUTION_PDF_URL,
        "source_sha1": hashlib.sha1(html_text.encode("utf-8", errors="replace")).hexdigest(),
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "pdf_url": CONSTITUTION_PDF_URL,
        "pdf_sha256": sha256_bytes(pdf_response.content),
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "texto_preview": plain[:500],
        "rastreamento_dispositivos_json": "[]",
        "quantidade_eventos_rastreamento": "0",
        "collection_slug": "constituicao",
        "collection_title": "Constituicao Federal",
        "texto_pdf_preview": extract_pdf_text(pdf_path, max_pages=2)[:500],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extrai legislacao tematica de mineracao e Constituicao em pasta separada.")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "extracao_legislacao_mineracao")
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--limit", type=int, help="Limite de atos ANM para teste.")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--max-pages", type=int, default=1500)
    parser.add_argument("--skip-constitution", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    downloader = AnmLegisDownloader(out, sleep_seconds=args.sleep, timeout=args.timeout)

    print("[1/3] Descobrindo legislacao tematica no ANMlegis...")
    discovered = discover_thematic_refs(downloader, max_pages=args.max_pages)
    discovered_rows = [
        ref_row(item["ref"], str(item["collection_slug"]), str(item["collection_title"]))
        for item in discovered.values()
    ]
    discovered_rows.sort(key=lambda row: (row["collection_slug"], row["ano_norma"], row["tipo_norma"], row["numero_norma"]))
    write_rows_csv(out / "atos_descobertos.csv", discovered_rows)
    write_rows_jsonl(out / "atos_descobertos.jsonl", discovered_rows)
    print(f"Atos ANMlegis descobertos: {len(discovered_rows)}")

    if args.discover_only:
        return 0

    print("[2/3] Baixando PDFs do ANMlegis e extraindo metadados...")
    metadata_path = out / "metadados.jsonl"
    rows = read_jsonl(metadata_path)
    done = {row.get("source_url") for row in rows if row.get("source_url")}
    errors = read_jsonl(out / "erros.jsonl")
    selected = list(discovered.values())
    selected.sort(key=lambda item: str(item["ref"].key))
    if args.limit:
        selected = selected[: args.limit]

    total = len(selected)
    for index, item in enumerate(selected, start=1):
        ref = item["ref"]
        if ref.public_url() in done:
            continue
        try:
            print(f"[{index}/{total}] {ref.tipo} {ref.numero}/{ref.ano} {ref.orgao}")
            row = downloader.download_one(ref, skip_existing=True)
            row["collection_slug"] = str(item["collection_slug"])
            row["collection_title"] = str(item["collection_title"])
            rows.append(row)
            done.add(row.get("source_url") or ref.public_url())
            append_jsonl(metadata_path, row)
        except Exception as exc:
            err = {
                **ref_row(ref, str(item["collection_slug"]), str(item["collection_title"])),
                "erro": str(exc),
                "erro_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            errors.append(err)
            append_jsonl(out / "erros.jsonl", err)
            print(f"  ERRO: {exc}")
        time.sleep(args.sleep)

    if not args.skip_constitution and CONSTITUTION_HTML_URL not in done:
        print("[3/3] Baixando Constituicao Federal...")
        try:
            row = download_constitution(out, downloader.session)
            rows.append(row)
            append_jsonl(metadata_path, row)
        except Exception as exc:
            err = {
                "collection_slug": "constituicao",
                "collection_title": "Constituicao Federal",
                "source_url": CONSTITUTION_HTML_URL,
                "erro": str(exc),
                "erro_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            errors.append(err)
            append_jsonl(out / "erros.jsonl", err)
            print(f"  ERRO Constituicao: {exc}")

    write_rows_csv(out / "metadados.csv", rows)
    tracking_rows = collect_tracking_rows(rows)
    write_rows_csv(out / "rastreamento_dispositivos.csv", tracking_rows)
    write_rows_jsonl(out / "rastreamento_dispositivos.jsonl", tracking_rows)
    write_rows_csv(out / "erros.csv", errors)
    print(f"Concluido. Metadados: {len(rows)} | Rastreamento: {len(tracking_rows)} | Erros: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
