import argparse
import csv
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REVOCATION_CLAUSE_RE = re.compile(
    r"\b(?P<verb>revoga(?:m|do|da|dos|das)?|ficam?\s+revogad[oa]s?)\b"
    r"(?P<body>[^.\n;]{0,520})",
    flags=re.IGNORECASE,
)

NORM_REF_RE = re.compile(
    r"\b(?P<tipo>"
    r"instru[cç][aã]o\s+normativa(?:\s+anm)?|"
    r"ordem\s+de\s+servi[cç]o(?:\s+sei)?|"
    r"resolu[cç][aã]o(?:\s+anm)?|"
    r"portaria|decreto(?:-lei)?|lei|"
    r"medida\s+provis[oó]ria|delibera[cç][aã]o|alvar[aá]"
    r")\s*(?:n[º°o.]*)?\s*(?P<numero>\d{1,6}(?:[./-]\d+)?)"
    r"(?P<tail>[^.\n;]{0,180})",
    flags=re.IGNORECASE,
)

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _strip_accents(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_tipo(value: Any) -> str:
    text = _strip_accents(value).lower()
    text = _normalize_spaces(text)
    # Para casar "resolucao" com "resolucao anm", preserva a natureza do ato
    # sem depender do orgao no tipo.
    return re.sub(r"\s+anm\b", "", text).strip()


def _normalize_numero(value: Any) -> str:
    text = _strip_accents(value).lower()
    text = re.sub(r"[^0-9/.-]+", "", text)
    return text.strip("./-")


def _parse_year(value: Any) -> Optional[int]:
    match = YEAR_RE.search(str(value or ""))
    if not match:
        return None
    return int(match.group(1))


def _chunk_doc(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("doc_name") or chunk.get("doc") or "")


def _norm_key_from_chunk(chunk: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
    tipo = _normalize_tipo(chunk.get("tipo_norma"))
    numero = _normalize_numero(chunk.get("numero_norma"))
    ano = _parse_year(chunk.get("ano_norma")) or _parse_year(_chunk_doc(chunk))
    if not tipo or not numero or not ano:
        return None
    return tipo, numero, ano


def _source_label(chunk: Dict[str, Any]) -> str:
    tipo = chunk.get("tipo_norma")
    numero = chunk.get("numero_norma")
    ano = chunk.get("ano_norma")
    if tipo and numero and ano:
        return f"{tipo} nº {numero}/{ano}"
    return _chunk_doc(chunk)


def _load_doc_representatives(chunks_path: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str, int], List[str]]]:
    docs: Dict[str, Dict[str, Any]] = {}
    keys: Dict[Tuple[str, str, int], List[str]] = defaultdict(list)

    with chunks_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            chunk = json.loads(raw)
            doc = _chunk_doc(chunk)
            if not doc:
                continue
            if doc not in docs:
                docs[doc] = chunk
                key = _norm_key_from_chunk(chunk)
                if key:
                    keys[key].append(doc)

    return docs, keys


def _extract_norm_refs(text: str) -> Iterable[Tuple[str, str, int, str, str]]:
    for clause in REVOCATION_CLAUSE_RE.finditer(text or ""):
        body = _normalize_spaces(clause.group("body"))
        if not body:
            continue
        lower_body = _strip_accents(body).lower()
        partial = any(
            marker in lower_body
            for marker in (" art", " artigo", " inciso", " paragrafo", " dispositivo")
        )

        for match in NORM_REF_RE.finditer(body):
            tipo = _normalize_tipo(match.group("tipo"))
            numero = _normalize_numero(match.group("numero"))
            tail = match.group("tail") or ""
            ano = _parse_year(tail)
            if not ano:
                continue
            trecho = _normalize_spaces(match.group(0))
            yield tipo, numero, ano, trecho, "parcial" if partial else "total"


def detect_revocation_impacts(chunks_path: Path) -> List[Dict[str, Any]]:
    docs, key_to_docs = _load_doc_representatives(chunks_path)
    rows: List[Dict[str, Any]] = []
    seen = set()

    with chunks_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            chunk = json.loads(raw)
            source_doc = _chunk_doc(chunk)
            text = str(chunk.get("text") or "")
            if not source_doc or "revog" not in _strip_accents(text).lower():
                continue

            source_start = chunk.get("data_inicio_vigencia")
            source_ref = _source_label(chunk)

            for tipo, numero, ano, trecho, alcance in _extract_norm_refs(text):
                key = (tipo, numero, ano)
                targets = [
                    target_doc for target_doc in key_to_docs.get(key, [])
                    if target_doc != source_doc
                ]
                if not targets:
                    row_key = (source_doc, tipo, numero, ano, trecho, "")
                    if row_key in seen:
                        continue
                    seen.add(row_key)
                    rows.append(
                        {
                            "status_match": "alvo_nao_encontrado",
                            "alcance_sugerido": alcance,
                            "norma_revogadora_doc": source_doc,
                            "norma_revogadora": source_ref,
                            "data_inicio_vigencia_revogadora": source_start,
                            "trecho_referencia": trecho,
                            "tipo_alvo": tipo,
                            "numero_alvo": numero,
                            "ano_alvo": ano,
                            "documento_alvo": "",
                            "status_atual_alvo": "",
                            "data_fim_vigencia_atual": "",
                        }
                    )
                    continue

                for target_doc in targets:
                    target = docs.get(target_doc) or {}
                    row_key = (source_doc, tipo, numero, ano, trecho, target_doc)
                    if row_key in seen:
                        continue
                    seen.add(row_key)
                    rows.append(
                        {
                            "status_match": "alvo_encontrado",
                            "alcance_sugerido": alcance,
                            "norma_revogadora_doc": source_doc,
                            "norma_revogadora": source_ref,
                            "data_inicio_vigencia_revogadora": source_start,
                            "trecho_referencia": trecho,
                            "tipo_alvo": tipo,
                            "numero_alvo": numero,
                            "ano_alvo": ano,
                            "documento_alvo": target_doc,
                            "status_atual_alvo": target.get("status_normativo") or "",
                            "data_fim_vigencia_atual": target.get("data_fim_vigencia") or "",
                        }
                    )

    return rows


def write_revocation_impact_report(rows: List[Dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status_match",
        "alcance_sugerido",
        "norma_revogadora_doc",
        "norma_revogadora",
        "data_inicio_vigencia_revogadora",
        "trecho_referencia",
        "tipo_alvo",
        "numero_alvo",
        "ano_alvo",
        "documento_alvo",
        "status_atual_alvo",
        "data_fim_vigencia_atual",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Gera relatorio de normativos que parecem revogar/alterar normativos ja indexados."
    )
    parser.add_argument("--chunks", required=True, type=Path, help="Caminho do chunks.jsonl")
    parser.add_argument("--output", required=True, type=Path, help="CSV de saida")
    return parser.parse_args()


def main():
    args = parse_args()
    rows = detect_revocation_impacts(args.chunks)
    write_revocation_impact_report(rows, args.output)
    print(f"[OK] impactos detectados: {len(rows)}")
    print(f"[OK] relatorio: {args.output}")


if __name__ == "__main__":
    main()
