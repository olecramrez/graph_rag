import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.enrich_chunks_metadata import _maybe_fix_mojibake, _normalize_spaces, _strip_accents


TRIGGER_PATTERNS = (
    r"\bentra\s+em\s+vigor\b",
    r"\bentrara\s+em\s+vigor\b",
    r"\bpassa\s+a\s+vigorar\b",
    r"\bpassara\s+a\s+vigorar\b",
    r"\bproduz(?:ira)?\s+efeitos?\b",
    r"\bpassa\s+a\s+produzir\s+efeitos?\b",
    r"\bcomeca\s+a\s+vigorar\b",
    r"\bcomecara\s+a\s+vigorar\b",
    r"\bcomeca\s+a\s+produzir\s+efeitos?\b",
)

SAME_DAY_PATTERNS = (
    r"\bna\s+data\s+de\s+(?:sua\s+)?publicacao\b",
    r"\bna\s+data\s+da\s+publicacao\b",
    r"\bna\s+data\s+da\s+sua\s+publicacao\b",
    r"\ba\s+partir\s+da\s+data\s+de\s+(?:sua\s+)?publicacao\b",
    r"\ba\s+contar\s+da\s+data\s+de\s+(?:sua\s+)?publicacao\b",
    r"\ba\s+contar\s+da\s+publicacao\b",
)

ABSOLUTE_DATE_RE = re.compile(
    r"\b\d{1,2}\D{0,3}\s+de\s+[a-z]+\s+de\s+(?:19\d{2}|20\d{2})\b|\b\d{1,2}/\d{1,2}/(?:19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)

RELATIVE_DAYS_RE = re.compile(
    r"\b(?:em|apos|a\s+partir\s+de|a\s+contar\s+de|a\s+contar\s+da|contados?\s+de|contados?\s+da|decorridos?\s+de|decorridos?\s+da|no\s+prazo\s+de|dentro\s+de)?\s*\d{1,3}\s*(?:\([^)]*\)\s*)?dias?\b",
    re.IGNORECASE,
)

RELATIVE_MONTH_YEAR_RE = re.compile(
    r"\b(?:em|apos|a\s+partir\s+de|a\s+contar\s+de|a\s+contar\s+da|contados?\s+de|contados?\s+da|decorridos?\s+de|decorridos?\s+da|no\s+prazo\s+de|dentro\s+de)?\s*\d{1,2}\s*(?:\([^)]*\)\s*)?(?:mes(?:es)?|anos?)\b",
    re.IGNORECASE,
)

BUSINESS_DAY_RE = re.compile(
    r"\b(?:no\s+)?primeiro\s+dia\s+util\s+do\s+(?:(?:primeiro|segundo|terceiro|quarto|quinto|sexto|setimo|oitavo|nono|decimo|\d{1,2})\s+)?mes(?:es)?\b",
    re.IGNORECASE,
)


def _normalize(text: Any) -> str:
    value = _maybe_fix_mojibake(str(text or ""))
    value = _strip_accents(value)
    value = _normalize_spaces(value)
    return value.lower()


def _is_normative(row: Dict[str, Any]) -> bool:
    status = _normalize(row.get("status_normativo"))
    if status in {"nao normativo", "nao_normativo"}:
        return False
    if status in {"vigente", "revogado", "indeterminado", "vacatio", "parcialmente_revogado"}:
        return True
    return bool(_normalize(row.get("tipo_norma")) or _normalize(row.get("titulo_norma")))


def _group_docs(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        name = row.get("doc_name") or row.get("doc")
        if not name:
            continue
        buckets[str(name)].append(row)

    for name, items in buckets.items():
        items.sort(key=lambda r: (r.get("chunk_index") is None, r.get("chunk_index", 0), r.get("page", 0)))
        first = items[0]
        docs[name] = {
            "doc_name": name,
            "data_publicacao": first.get("data_publicacao"),
            "data_inicio_vigencia": first.get("data_inicio_vigencia"),
            "status_normativo": first.get("status_normativo"),
            "tipo_norma": first.get("tipo_norma"),
            "titulo_norma": first.get("titulo_norma"),
            "texto": "\n".join(str(item.get("text") or "") for item in items),
            "chunk_count": len(items),
        }
    return docs


def _find_trigger_sentences(text: str) -> List[str]:
    normalized = _normalize(text)
    if not normalized:
        return []

    trigger_union = re.compile("|".join(TRIGGER_PATTERNS), re.IGNORECASE)
    sentences: List[str] = []
    for match in trigger_union.finditer(normalized):
        tail = normalized[match.end() : min(len(normalized), match.end() + 320)]
        sentence = re.split(r"[.\n;]", tail, maxsplit=1)[0].strip()
        if sentence:
            sentences.append(sentence)
    return sentences


def _sentence_labels(sentence: str) -> List[str]:
    labels: List[str] = []
    if not sentence:
        return labels

    absolute_dates = ABSOLUTE_DATE_RE.findall(sentence)
    has_same_day = any(re.search(pattern, sentence, re.IGNORECASE) for pattern in SAME_DAY_PATTERNS)
    has_business_day = bool(BUSINESS_DAY_RE.search(sentence))
    has_relative_days = bool(RELATIVE_DAYS_RE.search(sentence))
    has_relative_month_year = bool(RELATIVE_MONTH_YEAR_RE.search(sentence))

    if has_same_day and absolute_dates:
        labels.append("same_day_publication")
        labels.append("explicit_absolute_date")
        return labels

    if has_same_day:
        labels.append("same_day_publication")

    if has_business_day:
        labels.append("business_day_month")

    if len(absolute_dates) >= 2:
        labels.append("multi_explicit_dates")
    elif len(absolute_dates) == 1:
        labels.append("explicit_absolute_date")

    if has_relative_days:
        labels.append("relative_days")

    if has_relative_month_year:
        labels.append("relative_month_year")

    if not labels:
        labels.append("trigger_without_clause")

    return labels


def classify_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    text = doc.get("texto") or ""
    title = _normalize(doc.get("titulo_norma"))
    combined = f"{title}\n{text}"

    trigger_sentences = _find_trigger_sentences(combined)
    labels: List[str] = []
    evidence_parts: List[str] = []

    for sentence in trigger_sentences:
        sentence_labels = _sentence_labels(sentence)
        for label in sentence_labels:
            if label not in labels:
                labels.append(label)
        if sentence_labels:
            evidence_parts.append(sentence)

    if not labels:
        if any(token in combined for token in ("entra em vigor", "passa a vigorar", "produz efeitos", "vigencia", "vigente")):
            labels.append("other_vigencia_expression")
            evidence_parts.append("other_vigencia_expression")
        else:
            labels.append("silence")
            evidence_parts.append("silence")

    substantive_labels = [label for label in labels if label not in {"trigger_without_clause"}]
    if substantive_labels:
        labels = substantive_labels

    if len(labels) > 1:
        rule_id = "combo_" + "__".join(sorted(labels))
    else:
        rule_id = labels[0]

    return {
        **doc,
        "rule_id": rule_id,
        "rule_labels": "|".join(labels),
        "evidence": " || ".join(evidence_parts[:3]),
    }


def _human_rule(rule_id: str) -> str:
    mapping = {
        "silence": "Nao ha dispositivo de vigencia detectavel no texto examinado.",
        "same_day_publication": "A vigencia coincide com a data da publicacao.",
        "explicit_absolute_date": "A vigencia aponta uma data absoluta expressa no proprio dispositivo.",
        "multi_explicit_dates": "O mesmo dispositivo traz mais de uma data absoluta, normalmente com recortes por artigo ou grupo de artigos.",
        "relative_days": "A vigencia e calculada por prazo em dias contado da publicacao.",
        "relative_month_year": "A vigencia e calculada por prazo em meses ou anos contado da publicacao.",
        "business_day_month": "A vigencia cai no primeiro dia util de um mes indicado ou subsequente.",
        "combo_same_day_plus_explicit_date": "O dispositivo combina a data da publicacao com outra data expressa no mesmo trecho.",
        "trigger_without_clause": "Ha gatilho de vigencia, mas o trecho capturado nao traz a data ou o prazo completo.",
        "other_vigencia_expression": "Ha referencia de vigencia, mas o trecho nao bateu com as regras padrao extraidas.",
    }
    if rule_id.startswith("combo_"):
        parts = rule_id[len("combo_") :].split("__")
        pretty = ", ".join(parts)
        return f"Combinacao de regras: {pretty}."
    return mapping.get(rule_id, "Regra nao classificada.")


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fieldnames = [
        "doc_name",
        "status_normativo",
        "tipo_norma",
        "titulo_norma",
        "data_publicacao",
        "data_inicio_vigencia",
        "rule_id",
        "rule_labels",
        "evidence",
        "chunk_count",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_markdown(path: Path, rows: List[Dict[str, Any]]) -> None:
    by_rule: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_rule[str(row["rule_id"])].append(row)

    ordered = sorted(by_rule.items(), key=lambda item: (-len(item[1]), item[0]))
    lines: List[str] = []
    lines.append("# Vigencia Rules")
    lines.append("")
    lines.append("Resumo gerado a partir da varredura do corpus.")
    lines.append("")
    lines.append("| Regra | Casos | Definicao | Exemplo |")
    lines.append("| --- | ---: | --- | --- |")
    for rule_id, items in ordered:
        sample = items[0]
        example = sample.get("evidence") or sample.get("doc_name") or ""
        if len(example) > 120:
            example = example[:117] + "..."
        lines.append(
            f"| `{rule_id}` | {len(items)} | { _human_rule(rule_id) } | {example.replace('|', ' / ')} |"
        )
    lines.append("")
    lines.append("## Regras")
    lines.append("")
    for rule_id, items in ordered:
        sample = items[0]
        lines.append(f"### `{rule_id}`")
        lines.append("")
        lines.append(_human_rule(rule_id))
        lines.append("")
        lines.append(f"Casos: {len(items)}")
        lines.append("")
        lines.append(f"Exemplo: {sample.get('doc_name')} - {sample.get('evidence')}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(chunks_path: Path, output_dir: Path, normative_only: bool = True) -> List[Dict[str, Any]]:
    with chunks_path.open("r", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]

    docs = _group_docs(rows)
    classified: List[Dict[str, Any]] = []
    for doc in docs.values():
        if normative_only and not _is_normative(doc):
            continue
        classified.append(classify_doc(doc))

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "vigencia_casos_classificados.csv"
    md_path = output_dir / "vigencia_regras.md"
    _write_csv(csv_path, classified)
    _write_markdown(md_path, classified)

    counts = Counter(row["rule_id"] for row in classified)
    print(f"[OK] documentos analisados: {len(classified)}")
    for rule_id, count in counts.most_common():
        print(f"[OK] {rule_id}: {count}")
    print(f"[OK] arquivo CSV: {csv_path}")
    print(f"[OK] arquivo MD: {md_path}")
    return classified


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classifica casos de vigencia do corpus e extrai regras a partir dos trechos."
    )
    parser.add_argument(
        "--chunks",
        default=str(PROJECT_ROOT.parent / "base_rag" / "data" / "ANM_Legis_tratada2" / "chunks.jsonl"),
        help="Caminho do chunks.jsonl de entrada.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "reports"),
        help="Diretorio onde os relatorios serao gravados.",
    )
    parser.add_argument(
        "--include-nonnormative",
        action="store_true",
        help="Inclui documentos nao normativos na classificacao.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(
        chunks_path=Path(args.chunks),
        output_dir=Path(args.output_dir),
        normative_only=not args.include_nonnormative,
    )


if __name__ == "__main__":
    main()
