import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.enrich_chunks_metadata import (  # noqa: E402
    _choose_date_by_year,
    _coerce_iso_date,
    _extract_dates_from_text,
    _extract_entry_into_force_dates,
    _extract_publication_dates_from_text,
    _extract_relative_start_date,
    _extract_relative_vacatio_days,
    _first_business_day_of_month,
    _normalize_name,
    _normalize_spaces,
    _parse_portuguese_quantity,
    _parse_year,
    _strip_accents,
)


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, bak)
    return bak


def _load_chunks(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _save_chunks(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _parse_date(value: Any) -> Optional[datetime.date]:
    iso = _coerce_iso_date(value)
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        return None


def _extract_month_count_from_text(text: str) -> int:
    normalized = _strip_accents(_normalize_spaces(text or "")).lower()
    if not normalized:
        return 1
    m = re.search(
        r"primeiro\s+dia\s+util\s+do\s+(?:(?P<count>\d{1,4}|[a-z]+(?:\s+e\s+[a-z]+)*)\s+)?mes(?:es)?",
        normalized,
        flags=re.IGNORECASE,
    )
    if not m:
        return 1
    count = _parse_portuguese_quantity(m.group("count"))
    return count if count and count > 0 else 1


def _year_hint_from_doc(doc_name: str, rows: List[Dict[str, Any]]) -> Optional[int]:
    if rows:
        hint = _parse_year(rows[0].get("ano_norma"))
        if hint:
            return hint
    return _parse_year(doc_name)


def _infer_publication_for_doc(
    doc_name: str,
    rows: List[Dict[str, Any]],
    candidate_pub: Any = None,
) -> Tuple[Optional[str], str]:
    year_hint = _year_hint_from_doc(doc_name, rows)

    marker_dates: List[str] = []
    title_dates: List[str] = []
    head_dates: List[str] = []
    tail_dates: List[str] = []
    current_dates: List[str] = []

    for row in rows:
        text = str(row.get("text") or "")
        marker_dates.extend(_extract_publication_dates_from_text(text))
        title_dates.extend(_extract_dates_from_text(row.get("titulo_norma")))
        title_dates.extend(_extract_dates_from_text(doc_name))
        head_dates.extend(_extract_dates_from_text(text[:1600]))
        tail_dates.extend(_extract_dates_from_text(text[-1200:]))
        cur = _coerce_iso_date(row.get("data_publicacao"))
        if cur:
            current_dates.append(cur)

    buckets = (
        ("publication_marker", marker_dates),
        ("title_or_filename", title_dates),
        ("text_head", head_dates),
        ("text_tail", tail_dates),
        ("current_metadata", current_dates),
    )

    for method, values in buckets:
        if not values:
            continue
        chosen = _choose_date_by_year(values, year_hint) or sorted(values)[0]
        if chosen:
            return chosen, method

    candidate_pub_iso = _coerce_iso_date(candidate_pub)
    if candidate_pub_iso:
        return candidate_pub_iso, "candidate_sheet"

    if year_hint:
        return f"{int(year_hint):04d}-01-01", "year_hint_fallback"

    return None, "missing_publication"


def _texts_for_doc(evidence: Any, rows: List[Dict[str, Any]]) -> List[str]:
    texts: List[str] = []
    if evidence not in (None, ""):
        texts.append(str(evidence))

    sorted_rows = sorted(
        rows,
        key=lambda r: (
            int(r.get("page") or 0),
            int(r.get("chunk_index") or 0),
        ),
    )
    for row in sorted_rows[:12]:
        txt = str(row.get("text") or "")
        if txt:
            texts.append(txt)

    if rows:
        title = str(rows[0].get("titulo_norma") or "")
        if title:
            texts.append(title)

    return texts


def _infer_start_for_doc(
    publication_iso: str,
    rule: str,
    evidence: Any,
    rows: List[Dict[str, Any]],
) -> Tuple[str, str]:
    pub_date = _parse_date(publication_iso)
    if not pub_date:
        raise ValueError("publication_iso invalida para inferencia de vigencia.")

    sources = _texts_for_doc(evidence, rows)
    rule_norm = _strip_accents(_normalize_spaces(rule or "")).lower()

    explicit_dates: List[str] = []
    for src in sources:
        explicit_dates.extend(_extract_entry_into_force_dates(src))
    explicit_dates = sorted({d for d in explicit_dates if d})
    gt_explicit = [d for d in explicit_dates if d > publication_iso]
    if gt_explicit:
        return gt_explicit[0], "explicit_clause"

    for src in sources:
        rel = _extract_relative_start_date(src, publication_iso)
        if rel and rel > publication_iso:
            return rel, "relative_calendar"

    for src in sources:
        days = _extract_relative_vacatio_days(src)
        if days and days > 0:
            candidate = (pub_date + timedelta(days=int(days))).isoformat()
            if candidate > publication_iso:
                return candidate, "relative_days"

    if "business_day_month" in rule_norm:
        month_count = 1
        for src in sources:
            month_count = max(month_count, _extract_month_count_from_text(src))
        candidate = _first_business_day_of_month(pub_date, month_count).isoformat()
        if candidate > publication_iso:
            return candidate, "business_day_month"

    if "explicit_absolute_date" in rule_norm:
        generic_dates: List[str] = []
        for src in sources:
            generic_dates.extend(_extract_dates_from_text(src))
        generic_dates = sorted({d for d in generic_dates if d})
        gt_generic = [d for d in generic_dates if d > publication_iso]
        if gt_generic:
            return gt_generic[0], "explicit_generic"

    existing_starts = sorted(
        {
            _coerce_iso_date(row.get("data_inicio_vigencia"))
            for row in rows
            if _coerce_iso_date(row.get("data_inicio_vigencia"))
        }
    )
    gt_existing = [d for d in existing_starts if d and d > publication_iso]
    if gt_existing:
        return gt_existing[0], "existing_metadata"

    return (pub_date + timedelta(days=1)).isoformat(), "fallback_plus_1_day"


def _build_doc_maps(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[str]]]:
    by_doc: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_norm: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        doc_name = row.get("doc_name") or row.get("doc")
        if not doc_name:
            continue
        doc_name = str(doc_name)
        by_doc[doc_name].append(row)
        by_norm[_normalize_name(doc_name)].append(doc_name)
    return by_doc, by_norm


def _resolve_doc_name(
    candidate_name: str,
    by_doc: Dict[str, List[Dict[str, Any]]],
    by_norm: Dict[str, List[str]],
) -> Optional[str]:
    if candidate_name in by_doc:
        return candidate_name
    norm = _normalize_name(candidate_name)
    matches = by_norm.get(norm) or []
    if len(matches) == 1:
        return matches[0]
    if matches:
        return sorted(matches, key=len)[0]
    return None


def _normalize_status(value: Any) -> str:
    return _strip_accents(_normalize_spaces(str(value or ""))).lower()


def _docid_start_map(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    buckets: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        doc_id = row.get("doc_id")
        if not doc_id:
            continue
        start = _coerce_iso_date(row.get("data_inicio_vigencia"))
        if start:
            buckets[str(doc_id)].append(start)
    out: Dict[str, str] = {}
    for doc_id, values in buckets.items():
        out[doc_id] = sorted(set(values))[0]
    return out


def _apply_revocation_end_fix(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    docid_to_start = _docid_start_map(rows)
    changed: List[Dict[str, Any]] = []

    for row in rows:
        if _normalize_status(row.get("status_normativo")) != "revogado":
            continue
        revoker_id = row.get("revogado_por_doc_id")
        if not revoker_id:
            continue
        revoker_start = docid_to_start.get(str(revoker_id))
        if not revoker_start:
            continue

        own_pub = _coerce_iso_date(row.get("data_publicacao"))
        if own_pub and revoker_start < own_pub:
            continue

        if row.get("data_fim_vigencia") != revoker_start:
            row["data_fim_vigencia"] = revoker_start
            changed.append(row)

        if row.get("revogado_por_data_inicio_vigencia") != revoker_start:
            row["revogado_por_data_inicio_vigencia"] = revoker_start

    return changed


def _update_registry_csv(
    df: pd.DataFrame,
    corrections: Dict[str, Dict[str, Any]],
) -> Tuple[pd.DataFrame, int, int]:
    updated = 0
    for doc_name, corr in corrections.items():
        mask = df["source_file_name"] == doc_name
        if not mask.any():
            continue
        df.loc[mask, "data_publicacao"] = corr["data_publicacao"]
        df.loc[mask, "data_inicio_vigencia"] = corr["data_inicio_vigencia"]
        df.loc[mask, "vacatio_dias"] = str(corr["vacatio_dias"])
        updated += int(mask.sum())

    docid_to_start: Dict[str, str] = {}
    for _, row in df.iterrows():
        doc_id = row.get("doc_id")
        start = _coerce_iso_date(row.get("data_inicio_vigencia"))
        if doc_id and start and str(doc_id) not in docid_to_start:
            docid_to_start[str(doc_id)] = start

    rev_updated = 0
    for idx, row in df.iterrows():
        if _normalize_status(row.get("status_normativo")) != "revogado":
            continue
        revoker_id = row.get("revogado_por_doc_id")
        revoker_start = docid_to_start.get(str(revoker_id)) if revoker_id else None
        if not revoker_start:
            continue
        own_pub = _coerce_iso_date(row.get("data_publicacao"))
        if own_pub and revoker_start < own_pub:
            continue
        if str(row.get("data_fim_vigencia") or "") != revoker_start:
            df.at[idx, "data_fim_vigencia"] = revoker_start
            rev_updated += 1

    return df, updated, rev_updated


def _update_registry_json(
    payload: List[Dict[str, Any]],
    corrections: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, int]:
    updated = 0
    for item in payload:
        name = item.get("source_file_name")
        if not name or name not in corrections:
            continue
        corr = corrections[name]
        item["data_publicacao"] = corr["data_publicacao"]
        item["data_inicio_vigencia"] = corr["data_inicio_vigencia"]
        item["vacatio_dias"] = corr["vacatio_dias"]
        updated += 1

    docid_to_start: Dict[str, str] = {}
    for item in payload:
        doc_id = item.get("doc_id")
        start = _coerce_iso_date(item.get("data_inicio_vigencia"))
        if doc_id and start and str(doc_id) not in docid_to_start:
            docid_to_start[str(doc_id)] = start

    rev_updated = 0
    for item in payload:
        if _normalize_status(item.get("status_normativo")) != "revogado":
            continue
        revoker_id = item.get("revogado_por_doc_id")
        revoker_start = docid_to_start.get(str(revoker_id)) if revoker_id else None
        if not revoker_start:
            continue
        own_pub = _coerce_iso_date(item.get("data_publicacao"))
        if own_pub and revoker_start < own_pub:
            continue
        if str(item.get("data_fim_vigencia") or "") != revoker_start:
            item["data_fim_vigencia"] = revoker_start
            rev_updated += 1

    return payload, updated, rev_updated


def _write_report(
    path: Path,
    per_doc_rows: List[Dict[str, Any]],
    revocation_rows: List[Dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(per_doc_rows).to_excel(writer, sheet_name="candidatos", index=False)
        pd.DataFrame(revocation_rows).to_excel(writer, sheet_name="revogacoes", index=False)
        summary = pd.DataFrame(
            [
                {
                    "total_candidatos": len(per_doc_rows),
                    "candidatos_ok_start_gt_pub": int(sum(1 for r in per_doc_rows if r.get("ok_start_gt_pub"))),
                    "candidatos_com_fallback": int(sum(1 for r in per_doc_rows if "fallback" in str(r.get("metodo_inicio") or ""))),
                    "revogacoes_ajustadas_chunks": len(revocation_rows),
                }
            ]
        )
        summary.to_excel(writer, sheet_name="resumo", index=False)


def run(
    chunks_path: Path,
    registry_csv_path: Path,
    registry_json_path: Path,
    candidates_xlsx: Path,
    report_xlsx: Path,
    no_backup: bool,
) -> None:
    if not chunks_path.exists():
        raise FileNotFoundError(f"Chunks nao encontrado: {chunks_path}")
    if not registry_csv_path.exists():
        raise FileNotFoundError(f"Registry CSV nao encontrado: {registry_csv_path}")
    if not registry_json_path.exists():
        raise FileNotFoundError(f"Registry JSON nao encontrado: {registry_json_path}")
    if not candidates_xlsx.exists():
        raise FileNotFoundError(f"Planilha de candidatos nao encontrada: {candidates_xlsx}")

    if not no_backup:
        print(f"[BACKUP] chunks: {_backup(chunks_path)}")
        print(f"[BACKUP] registry csv: {_backup(registry_csv_path)}")
        print(f"[BACKUP] registry json: {_backup(registry_json_path)}")

    chunks = _load_chunks(chunks_path)
    by_doc, by_norm = _build_doc_maps(chunks)

    candidates_df = pd.read_excel(candidates_xlsx)
    per_doc_report: List[Dict[str, Any]] = []
    corrections_for_registry: Dict[str, Dict[str, Any]] = {}

    for _, cand in candidates_df.iterrows():
        cand_name = str(cand.get("arquivo") or "").strip()
        cand_rule = str(cand.get("regra") or "").strip()
        cand_evidence = cand.get("evidencia_textual")
        cand_pub = cand.get("data_publicacao")
        cand_start = cand.get("data_inicio_vigencia")

        resolved_doc = _resolve_doc_name(cand_name, by_doc, by_norm)
        if not resolved_doc:
            per_doc_report.append(
                {
                    "arquivo": cand_name,
                    "doc_resolvido": "",
                    "regra": cand_rule,
                    "data_publicacao_anterior": cand_pub,
                    "data_inicio_anterior": cand_start,
                    "data_publicacao_nova": "",
                    "data_inicio_nova": "",
                    "vacatio_dias_novo": "",
                    "metodo_publicacao": "doc_not_found",
                    "metodo_inicio": "doc_not_found",
                    "ok_start_gt_pub": False,
                    "chunks_doc": 0,
                }
            )
            continue

        doc_rows = by_doc[resolved_doc]
        old_pub = _coerce_iso_date(doc_rows[0].get("data_publicacao")) if doc_rows else None
        old_start = _coerce_iso_date(doc_rows[0].get("data_inicio_vigencia")) if doc_rows else None

        publication_iso, pub_method = _infer_publication_for_doc(
            resolved_doc,
            doc_rows,
            cand_pub,
        )
        if not publication_iso:
            publication_iso = _coerce_iso_date(cand_pub) or old_pub

        if not publication_iso:
            year_hint = _year_hint_from_doc(resolved_doc, doc_rows)
            if year_hint:
                publication_iso = f"{int(year_hint):04d}-01-01"
                pub_method = "year_hint_fallback"

        if not publication_iso:
            per_doc_report.append(
                {
                    "arquivo": cand_name,
                    "doc_resolvido": resolved_doc,
                    "regra": cand_rule,
                    "data_publicacao_anterior": old_pub or cand_pub,
                    "data_inicio_anterior": old_start or cand_start,
                    "data_publicacao_nova": "",
                    "data_inicio_nova": "",
                    "vacatio_dias_novo": "",
                    "metodo_publicacao": "missing_publication",
                    "metodo_inicio": "missing_publication",
                    "ok_start_gt_pub": False,
                    "chunks_doc": len(doc_rows),
                }
            )
            continue

        start_iso, start_method = _infer_start_for_doc(
            publication_iso,
            cand_rule,
            cand_evidence,
            doc_rows,
        )

        pub_date = _parse_date(publication_iso)
        start_date = _parse_date(start_iso)
        if not pub_date or not start_date:
            raise ValueError(f"Data invalida calculada para {resolved_doc}: pub={publication_iso}, start={start_iso}")

        if start_date <= pub_date:
            start_date = pub_date + timedelta(days=1)
            start_iso = start_date.isoformat()
            start_method = f"{start_method}+force_plus_1_day"

        vacatio_days = (start_date - pub_date).days
        if vacatio_days <= 0:
            vacatio_days = 1

        for row in doc_rows:
            row["data_publicacao"] = publication_iso
            row["data_inicio_vigencia"] = start_iso
            row["vacatio_dias"] = vacatio_days

        corrections_for_registry[resolved_doc] = {
            "data_publicacao": publication_iso,
            "data_inicio_vigencia": start_iso,
            "vacatio_dias": vacatio_days,
        }

        per_doc_report.append(
            {
                "arquivo": cand_name,
                "doc_resolvido": resolved_doc,
                "regra": cand_rule,
                "data_publicacao_anterior": old_pub or cand_pub,
                "data_inicio_anterior": old_start or cand_start,
                "data_publicacao_nova": publication_iso,
                "data_inicio_nova": start_iso,
                "vacatio_dias_novo": vacatio_days,
                "metodo_publicacao": pub_method,
                "metodo_inicio": start_method,
                "ok_start_gt_pub": bool(start_iso > publication_iso),
                "chunks_doc": len(doc_rows),
            }
        )

    rev_changed_rows = _apply_revocation_end_fix(chunks)
    rev_report = []
    for row in rev_changed_rows:
        rev_report.append(
            {
                "doc_name": row.get("doc_name") or row.get("doc"),
                "doc_id": row.get("doc_id"),
                "data_fim_vigencia_nova": row.get("data_fim_vigencia"),
                "revogado_por_doc_id": row.get("revogado_por_doc_id"),
                "revogado_por_data_inicio_vigencia": row.get("revogado_por_data_inicio_vigencia"),
            }
        )

    _save_chunks(chunks_path, chunks)

    registry_df = pd.read_csv(registry_csv_path, dtype=str, encoding="utf-8-sig")
    registry_df, registry_docs_updated, registry_rev_updated = _update_registry_csv(
        registry_df,
        corrections_for_registry,
    )
    registry_df.to_csv(registry_csv_path, index=False, encoding="utf-8-sig")

    with registry_json_path.open("r", encoding="utf-8") as handle:
        registry_json = json.load(handle)
    registry_json, registry_json_docs_updated, registry_json_rev_updated = _update_registry_json(
        registry_json,
        corrections_for_registry,
    )
    with registry_json_path.open("w", encoding="utf-8") as handle:
        json.dump(registry_json, handle, ensure_ascii=False, indent=2)

    _write_report(report_xlsx, per_doc_report, rev_report)

    total = len(per_doc_report)
    ok = sum(1 for r in per_doc_report if r.get("ok_start_gt_pub"))
    print(f"[OK] Candidatos processados: {total}")
    print(f"[OK] Candidatos com inicio > publicacao: {ok}")
    print(f"[OK] Candidatos com correcao aplicada no registry: {len(corrections_for_registry)}")
    print(f"[OK] Ajustes de data_fim_vigencia por revogador: {len(rev_report)}")
    print(f"[OK] Registry CSV rows atualizadas (candidatos): {registry_docs_updated}")
    print(f"[OK] Registry CSV rows atualizadas (revogacao): {registry_rev_updated}")
    print(f"[OK] Registry JSON rows atualizadas (candidatos): {registry_json_docs_updated}")
    print(f"[OK] Registry JSON rows atualizadas (revogacao): {registry_json_rev_updated}")
    print(f"[OK] Relatorio: {report_xlsx}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Corrige casos de vacatio um a um a partir da planilha de candidatos, "
            "atualizando chunks e registry de metadados."
        )
    )
    parser.add_argument("--chunks", required=True, help="Caminho do chunks.jsonl a corrigir.")
    parser.add_argument("--registry-csv", required=True, help="Caminho do index.csv (registry de metadados).")
    parser.add_argument("--registry-json", required=True, help="Caminho do index.json (registry de metadados).")
    parser.add_argument("--candidates-xlsx", required=True, help="Planilha com candidatos de vacatio.")
    parser.add_argument("--report-xlsx", required=True, help="Planilha de auditoria da correcao.")
    parser.add_argument("--no-backup", action="store_true", help="Nao criar backups dos arquivos de entrada.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(
        chunks_path=Path(args.chunks),
        registry_csv_path=Path(args.registry_csv),
        registry_json_path=Path(args.registry_json),
        candidates_xlsx=Path(args.candidates_xlsx),
        report_xlsx=Path(args.report_xlsx),
        no_backup=bool(args.no_backup),
    )


if __name__ == "__main__":
    main()

