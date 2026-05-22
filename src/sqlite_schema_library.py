import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.config import PERSISTENT_DATA_ROOT, get_sqlite_files
from src.sql_agent import discover_sqlite_schema, quote_identifier


LIBRARY_FILENAME = "sqlite_schema_library.json"
DICTIONARY_FILENAMES = {
    "dicionario.csv",
    "dicionario.xlsx",
    "dicionario.xls",
    "dicionario_dados.csv",
    "dicionario_dados.xlsx",
    "dictionary.csv",
    "dictionary.xlsx",
    "metadata.csv",
    "metadata.xlsx",
    "metadados.csv",
    "metadados.xlsx",
}
TEXT_ENCODINGS = ["utf-8-sig", "utf-8", "cp1252", "latin1"]
GLOBAL_DICTIONARY_TABLE_KEYS = {"", "*", "all", "any", "geral", "global", "todos", "todas", "qualquer"}


def _normalize(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.replace("\ufeff", "").lower().strip()


def is_dictionary_file(path):
    name = Path(path).name.lower()
    return name in DICTIONARY_FILENAMES or name.startswith(("dicionario_", "dictionary_", "metadados_", "metadata_"))


def find_dictionary_candidates(source_dir):
    root = Path(source_dir)
    if not root.exists():
        return []
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in {".csv", ".xlsx", ".xls"}
        and is_dictionary_file(path)
    ]


def _safe_stat(path):
    try:
        stat = Path(path).stat()
        return {"mtime": stat.st_mtime, "size": stat.st_size}
    except OSError:
        return {"mtime": None, "size": None}


def _profile_key(path):
    return str(Path(path).resolve()).lower()


def get_schema_library_path():
    return PERSISTENT_DATA_ROOT / LIBRARY_FILENAME


def get_sidecar_schema_path(sqlite_path):
    return Path(sqlite_path).with_suffix(".schema.json")


def _count_rows(conn, table_name):
    try:
        return conn.execute(f"SELECT COUNT(*) AS total FROM {quote_identifier(table_name)}").fetchone()["total"]
    except sqlite3.Error:
        return None


def _load_import_sources(conn):
    try:
        rows = conn.execute(
            """
            SELECT source_path, member_name, table_name, rows_imported, imported_at
            FROM import_files
            ORDER BY id
            """
        ).fetchall()
    except sqlite3.Error:
        return []

    return [
        {
            "source_path": row["source_path"],
            "member_name": row["member_name"],
            "table_name": row["table_name"],
            "rows_imported": row["rows_imported"],
            "imported_at": row["imported_at"],
        }
        for row in rows
    ]


def _first_present(row, aliases):
    normalized = {_normalize(key): key for key in row.keys()}
    for alias in aliases:
        key = normalized.get(_normalize(alias))
        if key is not None:
            value = row.get(key)
            if value is not None and str(value).strip() and str(value).lower() != "nan":
                return str(value).strip()
    return ""


def _split_aliases(value):
    aliases = []
    for item in str(value or "").replace("|", ";").split(";"):
        item = item.strip()
        if item:
            aliases.append(item)
    return aliases


def _extract_value_map(description):
    value_map = {}
    for line in str(description or "").splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]{1,30})\s*[-:–]\s*(.+?)\s*$", line)
        if not match:
            continue
        code = match.group(1).strip()
        label = match.group(2).strip()
        if code and label:
            value_map[code] = label
    return value_map


def _detect_text_encoding(path):
    sample = Path(path).read_bytes()[:131072]
    for encoding in TEXT_ENCODINGS:
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin1"


def _read_dictionary_csv(path):
    kwargs = {
        "dtype": "string",
        "sep": None,
        "engine": "python",
        "on_bad_lines": "skip",
    }
    encoding = _detect_text_encoding(path)
    try:
        return pd.read_csv(path, encoding=encoding, encoding_errors="replace", **kwargs)
    except TypeError:
        try:
            return pd.read_csv(path, encoding=encoding, **kwargs)
        except TypeError:
            kwargs.pop("on_bad_lines", None)
            kwargs["error_bad_lines"] = False
            kwargs["warn_bad_lines"] = False
            return pd.read_csv(path, encoding=encoding, **kwargs)
    except UnicodeDecodeError:
        for fallback in TEXT_ENCODINGS:
            if fallback == encoding:
                continue
            try:
                return pd.read_csv(path, encoding=fallback, **kwargs)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(path, encoding="latin1", **kwargs)


def load_data_dictionary(dictionary_path):
    if not dictionary_path:
        return {}
    path = Path(dictionary_path)
    if not path.exists():
        return {}

    if path.suffix.lower() == ".csv":
        dataframe = _read_dictionary_csv(path)
    else:
        dataframe = pd.read_excel(path, dtype="string")

    tables = {}
    for raw_row in dataframe.fillna("").to_dict(orient="records"):
        table_name = _first_present(raw_row, ["tabela", "table", "nome_tabela", "table_name", "entidade", "arquivo"])
        column_name = _first_present(raw_row, ["coluna", "column", "campo", "field", "nome_coluna", "column_name"])
        description = _first_present(raw_row, ["descricao", "descrição", "description", "significado", "definicao", "definição"])
        table_description = _first_present(raw_row, ["descricao_tabela", "descrição_tabela", "table_description", "descricao_entidade"])
        dtype = _first_present(raw_row, ["tipo", "type", "datatype", "tipo_dado", "data_type"])
        aliases = _split_aliases(_first_present(raw_row, ["aliases", "alias", "sinonimos", "sinônimos", "termos"]))

        if not table_name and not column_name:
            continue

        table_key = _normalize(table_name)
        table_entry = tables.setdefault(table_key, {"table": table_name, "description": "", "columns": {}})
        if table_description and not table_entry["description"]:
            table_entry["description"] = table_description
        elif description and not column_name and not table_entry["description"]:
            table_entry["description"] = description

        if column_name:
            table_entry["columns"][_normalize(column_name)] = {
                "column": column_name,
                "description": description,
                "type": dtype,
                "aliases": aliases,
                "value_map": _extract_value_map(description),
            }

    return tables


def _merge_table_dictionary(target, source):
    if not source:
        return target

    if source.get("description"):
        target["description"] = source["description"]
    table_aliases = set(target.get("aliases") or [])
    if source.get("table") and _normalize(source.get("table")) not in GLOBAL_DICTIONARY_TABLE_KEYS:
        table_aliases.add(source["table"])
    target["aliases"] = sorted(table_aliases)

    column_dict = source.get("columns") or {}
    for column in target.get("columns") or []:
        column_meta = column_dict.get(_normalize(column.get("name")))
        if not column_meta:
            continue
        if column_meta.get("description"):
            column["description"] = column_meta["description"]
        if column_meta.get("type"):
            column["dictionary_type"] = column_meta["type"]
        if column_meta.get("value_map"):
            column["value_map"] = column_meta["value_map"]
        aliases = set(column.get("aliases") or [])
        aliases.update(column_meta.get("aliases") or [])
        aliases.add(column_meta.get("column") or column.get("name"))
        column["aliases"] = sorted(alias for alias in aliases if alias)

    return target


def apply_data_dictionary(schema, dictionary):
    if not dictionary:
        return schema

    global_tables = [
        table_dict
        for table_key, table_dict in dictionary.items()
        if _normalize(table_key) in GLOBAL_DICTIONARY_TABLE_KEYS
    ]

    for table in schema:
        table_key = _normalize(table.get("table"))
        for table_dict in global_tables:
            _merge_table_dictionary(table, table_dict)
        _merge_table_dictionary(table, dictionary.get(table_key))

    return schema


def build_sqlite_schema_profile(sqlite_path, base_name=None, max_tables=200, max_columns=120, dictionary_path=None):
    path = Path(sqlite_path)
    stat = _safe_stat(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        schema = discover_sqlite_schema(conn, max_tables=max_tables, max_columns=max_columns)
        data_dictionary = load_data_dictionary(dictionary_path)
        schema = apply_data_dictionary(schema, data_dictionary)
        row_counts = {
            table["table"]: _count_rows(conn, table["table"])
            for table in schema
        }
        import_sources = _load_import_sources(conn)

    return {
        "version": 1,
        "base_name": base_name,
        "db_path": str(path.resolve()),
        "db_name": path.name,
        "source_mtime": stat["mtime"],
        "source_size": stat["size"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "schema": schema,
        "row_counts": row_counts,
        "import_sources": import_sources,
        "dictionary_path": str(Path(dictionary_path).resolve()) if dictionary_path else None,
        "dictionary_entries": sum(len(item.get("columns") or {}) for item in data_dictionary.values()) if dictionary_path else 0,
    }


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _profile_matches_file(profile, sqlite_path):
    stat = _safe_stat(sqlite_path)
    return (
        profile
        and profile.get("source_mtime") == stat["mtime"]
        and profile.get("source_size") == stat["size"]
    )


def load_schema_profile(sqlite_path):
    path = Path(sqlite_path)
    sidecar = _read_json(get_sidecar_schema_path(path))
    if _profile_matches_file(sidecar, path):
        return sidecar

    library = _read_json(get_schema_library_path()) or {}
    profile = (library.get("databases") or {}).get(_profile_key(path))
    if _profile_matches_file(profile, path):
        return profile

    return None


def load_or_build_schema_profile(sqlite_path, base_name=None, dictionary_path=None):
    profile = load_schema_profile(sqlite_path)
    if profile and dictionary_path:
        wanted = str(Path(dictionary_path).resolve())
        if profile.get("dictionary_path") != wanted:
            profile = None
    if profile:
        return profile, False
    return save_schema_profile(sqlite_path, base_name=base_name, dictionary_path=dictionary_path), True


def save_schema_profile(sqlite_path, base_name=None, dictionary_path=None):
    profile = build_sqlite_schema_profile(sqlite_path, base_name=base_name, dictionary_path=dictionary_path)
    _write_json(get_sidecar_schema_path(sqlite_path), profile)
    update_schema_library([profile])
    return profile


def update_schema_library(profiles):
    library_path = get_schema_library_path()
    library = _read_json(library_path) or {"version": 1, "databases": {}}
    databases = library.setdefault("databases", {})

    for profile in profiles:
        if not profile or not profile.get("db_path"):
            continue
        databases[_profile_key(profile["db_path"])] = profile

    library["generated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(library_path, library)
    return library


def build_schema_library_for_base(base_name, dictionary_path=None):
    profiles = []
    for sqlite_path in get_sqlite_files(base_name):
        profiles.append(save_schema_profile(sqlite_path, base_name=base_name, dictionary_path=dictionary_path))
    return profiles
