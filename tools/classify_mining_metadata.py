#!/usr/bin/env python
"""Add mining-domain classification metadata to CSV/JSONL metadata files."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


CLASSIFICATION_FIELDS = [
    "area_juridica_principal",
    "relacao_com_mineracao",
    "familia_normativa_mineraria",
    "papel_no_corpus_minerario",
    "aplicacao_mineraria",
    "usar_como_fundamento_principal",
    "confianca_classificacao_mineraria",
    "motivo_classificacao_mineraria",
]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def row_text(row: Dict[str, str]) -> str:
    fields = [
        "titulo",
        "ementa",
        "texto_preview",
        "texto_pdf_preview",
        "vigencia_trecho",
        "collection_title",
        "collection_slug",
        "menu_slug",
        "orgao",
        "tipo_norma",
        "textos_referencia_origem",
    ]
    return normalize(" ".join(row.get(field, "") or "" for field in fields))


def row_content_text(row: Dict[str, str]) -> str:
    fields = [
        "titulo",
        "ementa",
        "texto_preview",
        "texto_pdf_preview",
        "vigencia_trecho",
        "textos_referencia_origem",
    ]
    return normalize(" ".join(row.get(field, "") or "" for field in fields))


def has_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def without_institutional_mining_terms(text: str) -> str:
    replacements = [
        r"\bagencia nacional de mineracao\b",
        r"\bdepartamento nacional de producao mineral\b",
        r"\bministerio de minas e energia\b",
        r"\bsecretaria de geologia mineracao e transformacao mineral\b",
        r"\banm\b",
        r"\bdnpm\b",
    ]
    for pattern in replacements:
        text = re.sub(pattern, " ", text)
    return re.sub(r"\s+", " ", text).strip()


MINING_PATTERNS = [
    r"\bminer[a-z]*\b",
    r"\bmineral\b",
    r"\bminerio\b",
    r"\bminerios\b",
    r"\bmina\b",
    r"\bminas\b",
    r"\bgarimp",
    r"\blavra\b",
    r"\blavreir",
    r"\bjazid",
    r"\bsubstan(?:cia|cias) mineral",
    r"\bpesquisa mineral\b",
    r"\baproveitamento mineral\b",
    r"\bdireitos minerarios\b",
    r"\bprocesso minerario\b",
    r"\brecursos minerais\b",
    r"\bagencia nacional de mineracao\b",
    r"\banm\b",
    r"\bdnpm\b",
    r"\bcodigo de mineracao\b",
    r"\bplg\b",
    r"\bpermissao de lavra garimpeira\b",
    r"\bregistro de licenca\b",
    r"\bguia de utilizacao\b",
    r"\brelatorio final de pesquisa\b",
    r"\bdeclaracao de investimento em pesquisa mineral\b",
    r"\bdipm\b",
    r"\btah\b",
    r"\bcfem\b",
    r"\bcompensacao financeira pela exploracao",
    r"\bseguranca de barragens?\b",
    r"\bbarragem de mineracao\b",
    r"\bpilha de rejeito",
    r"\brejeitos? da mineracao\b",
    r"\bnr-?22\b",
    r"\bpoeiras minerais\b",
]

MINING_STRONG_PATTERNS = [
    pattern
    for pattern in MINING_PATTERNS
    if pattern not in {r"\banm\b", r"\bdnpm\b", r"\bminas\b"}
]

ENVIRONMENT_PATTERNS = [
    r"\bambient",
    r"\blicenciamento ambiental\b",
    r"\bibama\b",
    r"\bconama\b",
    r"\bunidades de conservacao\b",
    r"\brecursos hidricos\b",
    r"\bflorest",
    r"\bdano ambiental\b",
    r"\beia\b",
    r"\brima\b",
]

TAX_PATTERNS = [
    r"\btribut",
    r"\bimposto\b",
    r"\btaxa\b",
    r"\bcontribuic",
    r"\barrecad",
    r"\bcredito tributario\b",
    r"\bdivida ativa\b",
    r"\bparcelamento\b",
]

LABOR_PATTERNS = [
    r"\btrabalh",
    r"\bseguranca e saude\b",
    r"\bsaude ocupacional\b",
    r"\bnorma regulamentadora\b",
    r"\bmte\b",
]

ADMIN_PATTERNS = [
    r"\bestrutura regimental\b",
    r"\borganizacao basica\b",
    r"\bcargos? em comissao\b",
    r"\bfuncao de confianca\b",
    r"\bregimento interno\b",
    r"\bcompetencias?\b",
    r"\bministerio\b",
    r"\bcomite\b",
    r"\bgrupo de trabalho\b",
    r"\bdesigna",
    r"\bnomeia",
]

CONTRACT_PATTERNS = [
    r"\blicitac",
    r"\bcontrat",
    r"\bconvenio\b",
    r"\bacordo de cooperacao\b",
]

PENAL_PATTERNS = [
    r"\bpenal\b",
    r"\bcrime\b",
    r"\binfracao penal\b",
    r"\bcontrabando\b",
]

DATA_PATTERNS = [
    r"\bdados?\b",
    r"\bsistema eletronico\b",
    r"\bsei\b",
    r"\bprotocolo digital\b",
    r"\bassinat",
    r"\bprocesso eletronico\b",
]


def classify_area(text: str) -> str:
    material_text = without_institutional_mining_terms(text)
    if has_any(text, TAX_PATTERNS):
        return "tributario_arrecadatorio"
    if has_any(text, LABOR_PATTERNS):
        return "trabalhista_saude_seguranca"
    if has_any(text, ENVIRONMENT_PATTERNS):
        return "ambiental"
    if has_any(text, CONTRACT_PATTERNS):
        return "contratos_licitacoes"
    if has_any(text, PENAL_PATTERNS):
        return "penal_sancionatorio"
    if has_any(text, DATA_PATTERNS):
        return "administrativo_digital"
    if has_any(text, ADMIN_PATTERNS):
        return "administrativo_institucional"
    if has_any(material_text, MINING_STRONG_PATTERNS):
        return "minerario"
    return "administrativo_indeterminado"


def classify_family(text: str) -> str:
    family_rules: List[Tuple[str, List[str]]] = [
        (
            "cfem_tah_arrecadacao",
            [
                r"\bcfem\b",
                r"\btah\b",
                r"\btaxa anual por hectare\b",
                r"\bcompensacao financeira\b",
                r"\barrecadacao.*miner",
            ],
        ),
        (
            "barragens_rejeitos_seguranca",
            [
                r"\bbarragem",
                r"\brejeito",
                r"\bpilha de rejeito",
                r"\bseguranca de barragens?\b",
                r"\bpnsb\b",
            ],
        ),
        (
            "regimes_de_aproveitamento",
            [
                r"\balvara de pesquisa\b",
                r"\bautorizacao de pesquisa\b",
                r"\bconcessao de lavra\b",
                r"\bregistro de licenca\b",
                r"\blicenciamento mineral\b",
                r"\bguia de utilizacao\b",
                r"\brelatorio final de pesquisa\b",
                r"\bdisponibilidade\b",
            ],
        ),
        (
            "garimpo_plg",
            [
                r"\bgarimp",
                r"\bplg\b",
                r"\bpermissao de lavra garimpeira\b",
            ],
        ),
        (
            "fiscalizacao_sancoes_minerarias",
            [
                r"\bfiscalizacao.*miner",
                r"\bauto de infracao\b",
                r"\bsancao",
                r"\bmultas?\b",
                r"\binterdicao\b",
            ],
        ),
        (
            "seguranca_saude_ocupacional_mineracao",
            [
                r"\bnr-?22\b",
                r"\bsaude ocupacional na mineracao\b",
                r"\bpoeiras minerais\b",
                r"\bsilica\b",
            ],
        ),
        (
            "ambiental_mineracao",
            [
                r"\blicenciamento ambiental.*miner",
                r"\bimpacto ambiental.*miner",
                r"\breabilitacao de areas degradadas\b",
                r"\bprad\b",
            ],
        ),
        (
            "institucional_anm_dnpm",
            [
                r"\bagencia nacional de mineracao\b",
                r"\banm\b",
                r"\bdnpm\b",
                r"\bregimento interno.*anm\b",
                r"\bdepartamento nacional de producao mineral\b",
            ],
        ),
        (
            "mineracao_geral",
            MINING_STRONG_PATTERNS,
        ),
    ]
    for family, patterns in family_rules:
        if has_any(text, patterns):
            return family
    return "nao_classificada"


def classify_relation(
    row: Dict[str, str],
    text: str,
    content_text: str,
    area: str,
    family: str,
) -> Tuple[str, str, str, str, str]:
    collection_slug = normalize(row.get("collection_slug", ""))
    menu_slug = normalize(row.get("menu_slug", ""))
    orgao = normalize(row.get("orgao", ""))
    title_ementa = normalize(" ".join([row.get("titulo", "") or "", row.get("ementa", "") or ""]))
    material_text = without_institutional_mining_terms(content_text)
    material_title_ementa = without_institutional_mining_terms(title_ementa)

    strong_mining = has_any(material_title_ementa, MINING_STRONG_PATTERNS)
    any_mining = has_any(material_text, MINING_STRONG_PATTERNS)
    institutional_mining = bool(re.search(r"\b(anm|dnpm)\b", orgao)) or bool(
        re.search(r"\b(anm|dnpm|minera)", collection_slug + " " + menu_slug)
    )

    if strong_mining:
        return (
            "direta",
            "principal",
            "material",
            "true",
            "alta",
        )

    if family in {
        "cfem_tah_arrecadacao",
        "barragens_rejeitos_seguranca",
        "regimes_de_aproveitamento",
        "garimpo_plg",
        "fiscalizacao_sancoes_minerarias",
        "seguranca_saude_ocupacional_mineracao",
        "ambiental_mineracao",
        "mineracao_geral",
    }:
        return ("direta", "principal", "material", "true", "alta")

    if "legislacao_consolidada_mineracao" in collection_slug and any_mining:
        return ("direta", "principal", "material", "true", "media")

    if institutional_mining and area.startswith("administrativo"):
        return ("indireta", "apoio", "institucional", "false", "media")

    if any_mining:
        if area in {"ambiental", "trabalhista_saude_seguranca", "tributario_arrecadatorio"}:
            return ("indireta", "apoio", "setorial_transversal", "false", "media")
        return ("indireta", "apoio", "referencia_material", "false", "media")

    if row.get("textos_referencia_origem"):
        return ("referenciada", "apoio", "referencia_externa", "false", "media")

    return ("sem_relacao_identificada", "apoio", "contexto", "false", "baixa")


def classify_row(row: Dict[str, str]) -> Dict[str, str]:
    text = row_text(row)
    content_text = row_content_text(row)
    area = classify_area(content_text)
    family = classify_family(content_text)
    if family in {
        "regimes_de_aproveitamento",
        "garimpo_plg",
        "fiscalizacao_sancoes_minerarias",
        "mineracao_geral",
    }:
        area = "minerario"
    elif family == "cfem_tah_arrecadacao":
        area = "tributario_arrecadatorio"
    elif family == "ambiental_mineracao":
        area = "ambiental"
    elif family == "seguranca_saude_ocupacional_mineracao":
        area = "trabalhista_saude_seguranca"
    relation, role, application, use_main, confidence = classify_relation(row, text, content_text, area, family)

    if relation == "direta":
        reason = "sinais materiais de mineracao no titulo, ementa ou familia normativa"
    elif relation == "indireta":
        reason = "norma relacionada a mineracao, mas com uso preferencial como apoio no RAG"
    elif relation == "referenciada":
        reason = "norma externa recuperada por referencia em ato do corpus"
    else:
        reason = "sem sinal material suficiente de mineracao; manter apenas como contexto"

    return {
        "area_juridica_principal": area,
        "relacao_com_mineracao": relation,
        "familia_normativa_mineraria": family,
        "papel_no_corpus_minerario": role,
        "aplicacao_mineraria": application,
        "usar_como_fundamento_principal": use_main,
        "confianca_classificacao_mineraria": confidence,
        "motivo_classificacao_mineraria": reason,
    }


def read_csv(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    csv.field_size_limit(100_000_000)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def classify_file(csv_path: Path, jsonl_path: Path | None, backup_suffix: str) -> Dict[str, object]:
    rows, fieldnames = read_csv(csv_path)
    for field in CLASSIFICATION_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)

    if backup_suffix:
        csv_backup = csv_path.with_name(csv_path.name + backup_suffix)
        if not csv_backup.exists():
            shutil.copy2(csv_path, csv_backup)
        if jsonl_path and jsonl_path.exists():
            jsonl_backup = jsonl_path.with_name(jsonl_path.name + backup_suffix)
            if not jsonl_backup.exists():
                shutil.copy2(jsonl_path, jsonl_backup)

    for row in rows:
        row.update(classify_row(row))

    write_csv(csv_path, rows, fieldnames)
    if jsonl_path:
        write_jsonl(jsonl_path, rows)

    return {
        "arquivo": str(csv_path),
        "total": len(rows),
        "relacao": dict(Counter(row["relacao_com_mineracao"] for row in rows)),
        "area": dict(Counter(row["area_juridica_principal"] for row in rows)),
        "fundamento_principal": dict(Counter(row["usar_como_fundamento_principal"] for row in rows)),
    }


def write_summary(summary_path: Path, summaries: List[Dict[str, object]]) -> None:
    rows: List[Dict[str, str]] = []
    for summary in summaries:
        base = {"arquivo": str(summary["arquivo"]), "total": str(summary["total"])}
        for group in ("relacao", "area", "fundamento_principal"):
            counts = summary[group]
            if isinstance(counts, dict):
                for key, value in counts.items():
                    rows.append({**base, "grupo": group, "valor": key, "quantidade": str(value)})
    with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["arquivo", "total", "grupo", "valor", "quantidade"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-csv", action="append", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--backup-suffix", default=f".bak_mining_classification_{datetime.now():%Y%m%d_%H%M%S}")
    args = parser.parse_args()

    summaries = []
    for csv_value in args.metadata_csv:
        csv_path = Path(csv_value)
        jsonl_path = csv_path.with_suffix(".jsonl")
        summaries.append(classify_file(csv_path, jsonl_path if jsonl_path.exists() else None, args.backup_suffix))

    write_summary(Path(args.summary), summaries)
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
