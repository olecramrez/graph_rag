import argparse
import csv
import json
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_documents_dir, get_persistent_data_dir  # noqa: E402
from src.sqlite_schema_library import is_dictionary_file, save_schema_profile  # noqa: E402


SUPPORTED_EXTENSIONS = {".csv", ".txt", ".tsv"}
TEXT_ENCODINGS = ["utf-8-sig", "utf-8", "cp1252", "latin1"]


def slugify(value, max_len=64):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "dados"
    if re.match(r"^\d", text):
        text = f"t_{text}"
    return text[:max_len].strip("_") or "dados"


def unique_name(base, used):
    name = slugify(base)
    if name not in used:
        used.add(name)
        return name
    suffix = 2
    while f"{name}_{suffix}" in used:
        suffix += 1
    final = f"{name}_{suffix}"
    used.add(final)
    return final


def clean_columns(columns):
    used = set()
    result = []
    for idx, column in enumerate(columns, start=1):
        name = slugify(column or f"col_{idx}", max_len=96)
        if name in used:
            base = name
            suffix = 2
            while name in used:
                name = f"{base}_{suffix}"
                suffix += 1
        used.add(name)
        result.append(name)
    return result


def detect_text_encoding(path):
    sample = path.read_bytes()[:131072]
    for encoding in TEXT_ENCODINGS:
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin1"


def sniff_csv_options(path):
    sample = path.read_bytes()[:131072]
    encoding = detect_text_encoding(path)
    delimiters = [",", ";", "\t", "|"]
    try:
        text = sample.decode(encoding, errors="replace")
    except LookupError:
        text = sample.decode("latin1", errors="replace")
        encoding = "latin1"

    try:
        dialect = csv.Sniffer().sniff(text, delimiters=delimiters)
        return encoding, dialect.delimiter
    except csv.Error:
        pass

    for delimiter in delimiters:
        if delimiter in text:
            return encoding, delimiter

    return encoding, ";"


def read_csv_chunks(path, encoding, sep, chunksize):
    kwargs = {
        "encoding": encoding,
        "sep": sep,
        "dtype": "string",
        "chunksize": max(1000, int(chunksize)),
        "low_memory": False,
        "on_bad_lines": "skip",
    }
    try:
        return pd.read_csv(path, encoding_errors="replace", **kwargs)
    except TypeError:
        try:
            return pd.read_csv(path, **kwargs)
        except TypeError:
            kwargs.pop("on_bad_lines", None)
            kwargs["error_bad_lines"] = False
            kwargs["warn_bad_lines"] = False
            return pd.read_csv(path, **kwargs)


def read_csv_chunks_python(path, encoding, sep, chunksize):
    kwargs = {
        "encoding": encoding,
        "sep": sep,
        "engine": "python",
        "dtype": "string",
        "chunksize": max(1000, int(chunksize)),
        "on_bad_lines": "skip",
    }
    try:
        return pd.read_csv(path, encoding_errors="replace", **kwargs)
    except TypeError:
        try:
            return pd.read_csv(path, **kwargs)
        except TypeError:
            kwargs.pop("on_bad_lines", None)
            kwargs["error_bad_lines"] = False
            kwargs["warn_bad_lines"] = False
            return pd.read_csv(path, **kwargs)


def read_csv_dataframe(path, **kwargs):
    encoding = detect_text_encoding(path)
    try:
        return pd.read_csv(path, encoding=encoding, encoding_errors="replace", **kwargs)
    except TypeError:
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


def summarize_bad_csv_lines(path, encoding, sep, max_examples=5):
    if not sep or len(sep) != 1:
        return {"expected_fields": None, "bad_line_count": 0, "bad_line_examples": []}

    bad_line_count = 0
    examples = []
    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=sep)
            header = next(reader, None)
            if not header:
                return {"expected_fields": None, "bad_line_count": 0, "bad_line_examples": []}
            expected_fields = len(header)
            for line_number, row in enumerate(reader, start=2):
                if len(row) <= expected_fields:
                    continue
                bad_line_count += 1
                if len(examples) < max_examples:
                    examples.append(
                        {
                            "line": line_number,
                            "fields": len(row),
                            "expected_fields": expected_fields,
                        }
                    )
    except (OSError, csv.Error, UnicodeError):
        return {"expected_fields": None, "bad_line_count": 0, "bad_line_examples": []}

    return {
        "expected_fields": expected_fields,
        "bad_line_count": bad_line_count,
        "bad_line_examples": examples,
    }


def ensure_metadata_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            member_name TEXT,
            table_name TEXT NOT NULL,
            rows_imported INTEGER NOT NULL,
            encoding TEXT,
            delimiter TEXT,
            source_mtime REAL,
            source_size INTEGER,
            source_signature TEXT,
            imported_at TEXT NOT NULL
        )
        """
    )
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(import_files)").fetchall()
    }
    migrations = {
        "source_mtime": "ALTER TABLE import_files ADD COLUMN source_mtime REAL",
        "source_size": "ALTER TABLE import_files ADD COLUMN source_size INTEGER",
        "source_signature": "ALTER TABLE import_files ADD COLUMN source_signature TEXT",
    }
    for column, sql in migrations.items():
        if column not in existing:
            conn.execute(sql)


def existing_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _file_signature(path):
    stat = Path(path).stat()
    return stat.st_mtime, stat.st_size, f"file:{stat.st_mtime_ns}:{stat.st_size}"


def _zip_member_signature(zip_path, member):
    stat = Path(zip_path).stat()
    return (
        stat.st_mtime,
        stat.st_size,
        f"zip:{stat.st_mtime_ns}:{stat.st_size}:{member.filename}:{member.CRC}:{member.file_size}",
    )


def _find_previous_import(conn, source_path, member_name=None):
    return conn.execute(
        """
        SELECT id, table_name, source_signature
        FROM import_files
        WHERE source_path = ?
          AND COALESCE(member_name, '') = COALESCE(?, '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(source_path), member_name),
    ).fetchone()


def _delete_previous_import_rows(conn, source_path, member_name=None):
    conn.execute(
        """
        DELETE FROM import_files
        WHERE source_path = ?
          AND COALESCE(member_name, '') = COALESCE(?, '')
        """,
        (str(source_path), member_name),
    )


def import_csv_path(
    conn,
    path,
    table_base,
    run_id,
    source_path=None,
    member_name=None,
    chunksize=50000,
    source_mtime=None,
    source_size=None,
    source_signature=None,
    incremental=False,
):
    source_path = source_path or path
    if source_signature is None:
        source_mtime, source_size, source_signature = _file_signature(source_path)

    previous = _find_previous_import(conn, source_path, member_name) if incremental else None
    if previous and previous["source_signature"] == source_signature:
        return {
            "table": previous["table_name"],
            "rows": None,
            "source": str(source_path),
            "member": member_name,
            "status": "skipped_unchanged",
        }

    encoding, delimiter = sniff_csv_options(path)
    sep = "\t" if path.suffix.lower() == ".tsv" else delimiter
    bad_lines = summarize_bad_csv_lines(path, encoding, sep)
    table_name = previous["table_name"] if previous else unique_name(table_base, existing_tables(conn))
    first = True
    total_rows = 0

    reader = read_csv_chunks(path, encoding, sep, chunksize)
    try:
        chunk_iter = iter(reader)
        while True:
            try:
                chunk = next(chunk_iter)
            except StopIteration:
                break
            if first:
                chunk.columns = clean_columns(chunk.columns)
            else:
                chunk.columns = columns
            columns = list(chunk.columns)
            for column in columns:
                chunk[column] = chunk[column].map(lambda value: None if pd.isna(value) else str(value))
            chunk.to_sql(table_name, conn, if_exists="replace" if first else "append", index=False, chunksize=5000)
            total_rows += len(chunk)
            first = False
    except pd.errors.ParserError:
        first = True
        total_rows = 0
        reader = read_csv_chunks_python(path, encoding, sep, chunksize)
        for chunk in reader:
            if first:
                chunk.columns = clean_columns(chunk.columns)
            else:
                chunk.columns = columns
            columns = list(chunk.columns)
            for column in columns:
                chunk[column] = chunk[column].map(lambda value: None if pd.isna(value) else str(value))
            chunk.to_sql(table_name, conn, if_exists="replace" if first else "append", index=False, chunksize=5000)
            total_rows += len(chunk)
            first = False

    _delete_previous_import_rows(conn, source_path, member_name)
    conn.execute(
        """
        INSERT INTO import_files (
            run_id, source_path, member_name, table_name, rows_imported,
            encoding, delimiter, source_mtime, source_size, source_signature, imported_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            str(source_path),
            member_name,
            table_name,
            total_rows,
            encoding,
            sep,
            source_mtime,
            source_size,
            source_signature,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return {
        "table": table_name,
        "rows": total_rows,
        "source": str(source_path),
        "member": member_name,
        "status": "updated" if previous else "imported",
        "encoding": encoding,
        "delimiter": sep,
        "bad_lines_skipped": bad_lines["bad_line_count"],
        "bad_line_examples": bad_lines["bad_line_examples"],
    }


def iter_sources(source_dir):
    for path in sorted(source_dir.rglob("*")):
        if (
            path.is_file()
            and not is_dictionary_file(path)
            and (path.suffix.lower() in SUPPORTED_EXTENSIONS or path.suffix.lower() == ".zip")
        ):
            yield path


def import_zip(conn, zip_path, run_id, chunksize=50000, incremental=False):
    imported = []
    with tempfile.TemporaryDirectory(prefix="sqlite_import_") as tmp_name:
        tmp_dir = Path(tmp_name)
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                member_path = Path(member.filename)
                if member_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                source_mtime, source_size, source_signature = _zip_member_signature(zip_path, member)
                previous = _find_previous_import(conn, zip_path, member.filename) if incremental else None
                if previous and previous["source_signature"] == source_signature:
                    imported.append(
                        {
                            "table": previous["table_name"],
                            "rows": None,
                            "source": str(zip_path),
                            "member": member.filename,
                            "status": "skipped_unchanged",
                        }
                    )
                    continue
                target = tmp_dir / member_path.name
                with archive.open(member) as source, target.open("wb") as out:
                    out.write(source.read())
                table_base = f"{zip_path.stem}_{member_path.stem}"
                imported.append(
                    import_csv_path(
                        conn,
                        target,
                        table_base,
                        run_id,
                        source_path=zip_path,
                        member_name=member.filename,
                        chunksize=chunksize,
                        source_mtime=source_mtime,
                        source_size=source_size,
                        source_signature=source_signature,
                        incremental=incremental,
                    )
                )
    return imported


def import_tabular_to_sqlite(
    source_dir,
    sqlite_path,
    chunksize=50000,
    base_name=None,
    build_schema=True,
    incremental=False,
    dictionary_path=None,
):
    source_dir = Path(source_dir)
    sqlite_path = Path(sqlite_path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    if not source_dir.exists():
        raise FileNotFoundError(f"Pasta de documentos nao encontrada: {source_dir}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    imported = []
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_tables(conn)
        for path in iter_sources(source_dir):
            if path.suffix.lower() == ".zip":
                imported.extend(import_zip(conn, path, run_id, chunksize=chunksize, incremental=incremental))
            else:
                imported.append(
                    import_csv_path(
                        conn,
                        path,
                        path.stem,
                        run_id,
                        chunksize=chunksize,
                        incremental=incremental,
                    )
                )
        conn.commit()

    manifest_path = sqlite_path.with_suffix(".import_manifest.json")
    manifest = {
        "run_id": run_id,
        "source_dir": str(source_dir),
        "sqlite_path": str(sqlite_path),
        "incremental": incremental,
        "imported": imported,
    }
    if build_schema:
        schema_profile = save_schema_profile(sqlite_path, base_name=base_name, dictionary_path=dictionary_path)
        manifest["schema_path"] = str(sqlite_path.with_suffix(".schema.json"))
        manifest["schema_tables"] = len(schema_profile.get("schema") or [])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Importa CSV/TXT/TSV diretos ou dentro de ZIP para um SQLite em base_rag/data."
    )
    parser.add_argument("--base", required=True, help="Nome da base em base_rag/documentos/<base>.")
    parser.add_argument("--source-dir", help="Pasta de origem. Padrao: base_rag/documentos/<base>.")
    parser.add_argument("--sqlite-path", help="Arquivo SQLite de destino. Padrao: base_rag/data/<base>/<base>.sqlite.")
    parser.add_argument("--chunksize", type=int, default=50000)
    parser.add_argument("--incremental", action="store_true", help="Importa somente arquivos/membros novos ou alterados.")
    parser.add_argument("--dictionary-path", help="CSV/XLSX com dicionario de dados da base.")
    args = parser.parse_args()

    source_dir = Path(args.source_dir) if args.source_dir else get_documents_dir(args.base)
    sqlite_path = Path(args.sqlite_path) if args.sqlite_path else get_persistent_data_dir(args.base) / f"{slugify(args.base)}.sqlite"
    manifest = import_tabular_to_sqlite(
        source_dir,
        sqlite_path,
        chunksize=args.chunksize,
        base_name=args.base,
        incremental=args.incremental,
        dictionary_path=args.dictionary_path,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
