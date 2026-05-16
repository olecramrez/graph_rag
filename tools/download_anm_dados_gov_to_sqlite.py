import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_persistent_data_dir


BASE_URL = "https://dados.gov.br"
ANM_OPEN_DATA_URL = "https://dadosabertos.anm.gov.br/"
SEARCH_ENDPOINT = "/api/publico/conjuntos-dados/buscar"
DETAIL_ENDPOINTS = (
    "/api/publico/conjuntos-dados/{id}",
    "/dados/api/publico/conjuntos-dados/{id}",
    "/api/conjuntos-dados/{id}",
)
DEFAULT_USER_AGENT = "graph-rag-anm-importer/1.0"
SUPPORTED_TABULAR_FORMATS = {"csv", "txt", "tsv", "xlsx", "xls", "json", "geojson", "parquet"}
SKIP_DIRECT_EXTENSIONS = {"aspx", "html", "htm"}


class DadosGovAuthError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slugify(value, max_len=64):
    text = clean_text(value).lower()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "item"
    return text[:max_len].strip("_") or "item"


def unique_name(base, used, max_len=63):
    candidate = slugify(base, max_len=max_len)
    if candidate not in used:
        used.add(candidate)
        return candidate
    digest = hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:8]
    candidate = f"{candidate[:max_len - 9]}_{digest}"
    counter = 2
    original = candidate
    while candidate in used:
        suffix = f"_{counter}"
        candidate = f"{original[:max_len - len(suffix)]}{suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def infer_format(resource):
    value = (
        resource.get("format")
        or resource.get("formato")
        or resource.get("mimetype")
        or resource.get("tipo")
        or ""
    )
    fmt = clean_text(value).lower().strip(".")
    if fmt:
        if "excel" in fmt:
            return "xlsx"
        if "csv" in fmt:
            return "csv"
        if "json" in fmt:
            return "json"
        if "parquet" in fmt:
            return "parquet"
        if "zip" in fmt:
            return "zip"
        return fmt

    path = urlparse(resource.get("url") or "").path.lower()
    suffix = Path(path).suffix.lower().strip(".")
    return suffix


def safe_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def request_json(session, url, params=None, timeout=60, retries=3):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code == 401:
                raise DadosGovAuthError(
                    "dados.gov.br retornou 401. Gere um token de consumidor e informe "
                    "por --token ou DADOS_GOV_BR_TOKEN."
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, DadosGovAuthError):
                break
            if attempt == retries:
                break
            time.sleep(min(2 * attempt, 8))
    raise last_exc


def request_text(session, url, timeout=60, retries=3):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            if not response.encoding:
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                break
            time.sleep(min(2 * attempt, 8))
    raise last_exc


def create_session(token=None):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE_URL}/dados/busca?termo=anm",
        }
    )
    token = clean_text(token or os.getenv("DADOS_GOV_BR_TOKEN") or os.getenv("DADOS_GOV_BR_API_TOKEN"))
    if token:
        if token.lower().startswith("bearer "):
            session.headers["Authorization"] = token
        else:
            session.headers["Authorization"] = f"Bearer {token}"
    return session


def init_db(conn):
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            query TEXT NOT NULL,
            base_url TEXT NOT NULL,
            datasets_found INTEGER DEFAULT 0,
            resources_found INTEGER DEFAULT 0,
            resources_imported INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS datasets (
            id TEXT PRIMARY KEY,
            name TEXT,
            title TEXT,
            notes TEXT,
            organization_id TEXT,
            organization_name TEXT,
            organization_title TEXT,
            metadata_created TEXT,
            metadata_modified TEXT,
            source_url TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resources (
            id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            name TEXT,
            description TEXT,
            format TEXT,
            url TEXT,
            mimetype TEXT,
            size INTEGER,
            table_name TEXT,
            local_path TEXT,
            imported_rows INTEGER,
            imported_at TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );

        CREATE TABLE IF NOT EXISTS import_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            dataset_id TEXT,
            resource_id TEXT,
            url TEXT,
            stage TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_resources_dataset_id ON resources(dataset_id);
        CREATE INDEX IF NOT EXISTS idx_resources_format ON resources(format);
        CREATE INDEX IF NOT EXISTS idx_datasets_org ON datasets(organization_title, organization_name);
        """
    )


def insert_error(conn, run_id, dataset_id, resource_id, url, stage, error):
    conn.execute(
        """
        INSERT INTO import_errors(run_id, dataset_id, resource_id, url, stage, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, dataset_id, resource_id, url, stage, str(error)[:4000], utc_now()),
    )


def normalize_search_payload(payload):
    if isinstance(payload, dict) and "registros" in payload:
        return payload.get("registros") or [], int(payload.get("totalRegistros") or 0)
    if isinstance(payload, dict) and "results" in payload:
        return payload.get("results") or [], int(payload.get("count") or payload.get("total") or 0)
    if isinstance(payload, dict) and payload.get("result"):
        result = payload["result"]
        return result.get("results") or [], int(result.get("count") or 0)
    if isinstance(payload, list):
        return payload, len(payload)
    return [], 0


def search_datasets(session, query, page_size=100, max_pages=None, dados_abertos=True):
    offset = 0
    page = 0
    total = None
    while True:
        params = {
            "offset": str(offset),
            "tamanhoPagina": str(page_size),
            "titulo": query,
        }
        if dados_abertos is not None:
            params["dadosAbertos"] = str(bool(dados_abertos)).lower()
        payload = request_json(session, f"{BASE_URL}{SEARCH_ENDPOINT}", params=params)
        records, total_records = normalize_search_payload(payload)
        if total is None:
            total = total_records
        if not records:
            break
        for record in records:
            yield record
        page += 1
        offset += page_size
        if max_pages and page >= max_pages:
            break
        if total is not None and offset >= total:
            break


def parse_directory_index(html_text, base_url):
    links = []
    for match in re.finditer(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html_text, flags=re.I | re.S):
        href = html.unescape(match.group(1)).strip()
        label = re.sub(r"<[^>]+>", "", match.group(2))
        label = html.unescape(clean_text(label))
        if not href or href.startswith("#") or href.startswith("?"):
            continue
        if href in {"../", "/"} or label.lower() in {"to parent directory", "parent directory", "../"}:
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        is_dir = href.endswith("/") or label.endswith("/")
        links.append({"url": absolute, "name": label.strip("/") or Path(parsed.path).name, "is_dir": is_dir})
    return links


def direct_resource_from_url(url, name, dataset):
    suffix = Path(urlparse(url).path).suffix.lower().strip(".")
    return {
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest(),
        "name": name or Path(urlparse(url).path).name,
        "description": f"Arquivo publicado em diretorio aberto da ANM: {dataset.get('title')}",
        "format": suffix,
        "url": url,
        "mimetype": "",
        "size": None,
    }


def crawl_anm_open_data(session, root_url=ANM_OPEN_DATA_URL, max_depth=6, max_dirs=None):
    queue = [(root_url.rstrip("/") + "/", 0)]
    visited = set()
    yielded = 0

    while queue:
        url, depth = queue.pop(0)
        key = url.lower()
        if key in visited:
            continue
        visited.add(key)
        if max_dirs and len(visited) > max_dirs:
            break

        page = request_text(session, url)
        links = parse_directory_index(page, url)
        files = []
        for link in links:
            if link["is_dir"]:
                if depth < max_depth:
                    queue.append((link["url"].rstrip("/") + "/", depth + 1))
                continue
            suffix = Path(urlparse(link["url"]).path).suffix.lower().strip(".")
            if suffix in SKIP_DIRECT_EXTENSIONS:
                continue
            files.append(link)

        if not files:
            continue

        parsed = urlparse(url)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        dataset_name_value = "_".join(parts) if parts else "anm_dados_abertos"
        title = "ANM Dados Abertos - " + (" / ".join(parts) if parts else "raiz")
        dataset = {
            "id": hashlib.sha1(url.encode("utf-8")).hexdigest(),
            "name": dataset_name_value,
            "title": title,
            "notes": f"Diretorio aberto oficial da ANM: {url}",
            "organization": {
                "id": "anm",
                "name": "agencia-nacional-de-mineracao",
                "title": "Agencia Nacional de Mineracao - ANM",
            },
            "source_url": url,
            "resources": [],
        }
        dataset["resources"] = [
            direct_resource_from_url(link["url"], link["name"], dataset)
            for link in files
        ]
        yielded += 1
        yield dataset


def iter_catalog_datasets(session, args):
    source = args.source
    if source == "anm-direct":
        yield from crawl_anm_open_data(
            session,
            root_url=args.anm_open_data_url,
            max_depth=args.max_depth,
            max_dirs=args.max_dirs,
        )
        return

    try:
        yield from search_datasets(
            session,
            args.query,
            page_size=args.page_size,
            max_pages=args.max_pages,
            dados_abertos=None if args.include_non_open else True,
        )
    except DadosGovAuthError:
        if source == "dados-gov":
            raise
        print("[WARN] dados.gov.br exige token. Usando fonte direta oficial da ANM sem autenticacao.")
        yield from crawl_anm_open_data(
            session,
            root_url=args.anm_open_data_url,
            max_depth=args.max_depth,
            max_dirs=args.max_dirs,
        )


def dataset_id(dataset):
    return clean_text(dataset.get("id") or dataset.get("name") or dataset.get("nome") or dataset.get("nomeConjuntoDados"))


def dataset_name(dataset):
    return clean_text(dataset.get("name") or dataset.get("nome") or dataset.get("nomeConjuntoDados") or dataset_id(dataset))


def dataset_title(dataset):
    return clean_text(dataset.get("title") or dataset.get("titulo") or dataset.get("tituloConjuntoDados") or dataset_name(dataset))


def organization_fields(dataset):
    org = dataset.get("organization") or dataset.get("organizacao") or {}
    return {
        "organization_id": clean_text(dataset.get("organizationId") or org.get("id") or dataset.get("idOrganizacao")),
        "organization_name": clean_text(dataset.get("organizationName") or org.get("name") or org.get("nome")),
        "organization_title": clean_text(dataset.get("organizationTitle") or org.get("title") or org.get("titulo")),
    }


def is_anm_dataset(dataset):
    org = organization_fields(dataset)
    haystack = " ".join(
        [
            dataset_title(dataset),
            dataset_name(dataset),
            org["organization_name"],
            org["organization_title"],
        ]
    ).lower()
    normalized = (
        haystack.replace("ê", "e")
        .replace("é", "e")
        .replace("ç", "c")
        .replace("ã", "a")
        .replace("á", "a")
        .replace("í", "i")
        .replace("ó", "o")
    )
    return "anm" in normalized or "agencia nacional de mineracao" in normalized


def fetch_dataset_detail(session, dataset):
    ident = dataset_name(dataset) or dataset_id(dataset)
    if not ident:
        return dataset
    for endpoint in DETAIL_ENDPOINTS:
        try:
            payload = request_json(session, f"{BASE_URL}{endpoint.format(id=ident)}", retries=1)
            if isinstance(payload, dict):
                if payload.get("conjuntoDadosEdicao"):
                    detail = dict(payload.get("conjuntoDadosEdicao") or {})
                    if payload.get("resources"):
                        detail["resources"] = payload["resources"]
                    return {**dataset, **detail}
                if payload.get("result"):
                    return {**dataset, **payload["result"]}
                return {**dataset, **payload}
        except Exception:
            continue
    return dataset


def get_resources(dataset):
    resources = (
        dataset.get("resources")
        or dataset.get("recursos")
        or dataset.get("resourcesFormatado")
        or dataset.get("recursosFormatado")
        or []
    )
    if isinstance(resources, dict):
        resources = list(resources.values())
    return [item for item in resources if isinstance(item, dict)]


def upsert_dataset(conn, dataset):
    org = organization_fields(dataset)
    ident = dataset_id(dataset) or dataset_name(dataset)
    conn.execute(
        """
        INSERT INTO datasets(
            id, name, title, notes, organization_id, organization_name, organization_title,
            metadata_created, metadata_modified, source_url, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            title=excluded.title,
            notes=excluded.notes,
            organization_id=excluded.organization_id,
            organization_name=excluded.organization_name,
            organization_title=excluded.organization_title,
            metadata_created=excluded.metadata_created,
            metadata_modified=excluded.metadata_modified,
            source_url=excluded.source_url,
            raw_json=excluded.raw_json
        """,
        (
            ident,
            dataset_name(dataset),
            dataset_title(dataset),
            clean_text(dataset.get("notes") or dataset.get("descricao") or dataset.get("markdownNotes")),
            org["organization_id"],
            org["organization_name"],
            org["organization_title"],
            clean_text(dataset.get("metadata_created") or dataset.get("dataCriacao")),
            clean_text(dataset.get("metadata_modified") or dataset.get("dataUltimaAtualizacaoDados")),
            clean_text(dataset.get("source_url")) or f"{BASE_URL}/dados/conjuntos-dados/{dataset_name(dataset)}",
            safe_json(dataset),
        ),
    )


def upsert_resource(conn, dataset, resource, table_name=None, local_path=None, imported_rows=None):
    ident = clean_text(resource.get("id") or hashlib.sha1(safe_json(resource).encode("utf-8")).hexdigest())
    dataset_ident = dataset_id(dataset) or dataset_name(dataset)
    conn.execute(
        """
        INSERT INTO resources(
            id, dataset_id, name, description, format, url, mimetype, size,
            table_name, local_path, imported_rows, imported_at, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            dataset_id=excluded.dataset_id,
            name=excluded.name,
            description=excluded.description,
            format=excluded.format,
            url=excluded.url,
            mimetype=excluded.mimetype,
            size=excluded.size,
            table_name=COALESCE(excluded.table_name, resources.table_name),
            local_path=COALESCE(excluded.local_path, resources.local_path),
            imported_rows=COALESCE(excluded.imported_rows, resources.imported_rows),
            imported_at=COALESCE(excluded.imported_at, resources.imported_at),
            raw_json=excluded.raw_json
        """,
        (
            ident,
            dataset_ident,
            clean_text(resource.get("name") or resource.get("nome") or resource.get("title")),
            clean_text(resource.get("description") or resource.get("descricao")),
            infer_format(resource),
            clean_text(resource.get("url") or resource.get("downloadUrl")),
            clean_text(resource.get("mimetype")),
            resource.get("size") if str(resource.get("size") or "").isdigit() else None,
            table_name,
            str(local_path) if local_path else None,
            imported_rows,
            utc_now() if imported_rows is not None else None,
            safe_json(resource),
        ),
    )
    return ident


def download_resource(session, resource, output_dir, timeout=180):
    url = clean_text(resource.get("url") or resource.get("downloadUrl"))
    if not url:
        raise ValueError("recurso sem URL")
    output_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    name = Path(parsed.path).name or f"resource_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"
    target = output_dir / name
    if target.exists() and target.stat().st_size > 0:
        return target

    with session.get(url, stream=True, timeout=timeout) as response:
        if response.status_code == 401:
            raise RuntimeError("download retornou 401; verifique o token/autorizacao")
        response.raise_for_status()
        tmp = target.with_suffix(target.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
        tmp.replace(target)
    return target


def sniff_csv_options(path):
    sample = path.read_bytes()[:65536]
    encodings = ["utf-8-sig", "utf-8", "latin1"]
    delimiters = [",", ";", "\t", "|"]
    for encoding in encodings:
        try:
            text = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            dialect = csv.Sniffer().sniff(text, delimiters=delimiters)
            return encoding, dialect.delimiter
        except csv.Error:
            pass
    return "utf-8-sig", ";"


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


def write_dataframe(conn, table_name, dataframe, if_exists="replace"):
    dataframe = dataframe.copy()
    dataframe.columns = clean_columns(dataframe.columns)
    for column in dataframe.columns:
        if dataframe[column].dtype == "object":
            dataframe[column] = dataframe[column].map(lambda value: None if pd.isna(value) else str(value))
    dataframe.to_sql(table_name, conn, if_exists=if_exists, index=False, chunksize=5000)
    return len(dataframe)


def read_dataframes(path, fmt, max_rows=None):
    fmt = (fmt or "").lower()
    if fmt in {"csv", "txt", "tsv"}:
        encoding, delimiter = sniff_csv_options(path)
        kwargs = {
            "encoding": encoding,
            "sep": "\t" if fmt == "tsv" else delimiter,
            "dtype": "string",
            "low_memory": False,
        }
        if max_rows:
            kwargs["nrows"] = max_rows
        return [("data", pd.read_csv(path, **kwargs))]

    if fmt in {"xlsx", "xls"}:
        sheets = pd.read_excel(path, sheet_name=None, dtype="string", nrows=max_rows)
        return [(slugify(sheet, max_len=40), df) for sheet, df in sheets.items()]

    if fmt in {"json", "geojson"}:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "features" in data and isinstance(data["features"], list):
            rows = []
            for feature in data["features"]:
                row = dict(feature.get("properties") or {})
                geometry = feature.get("geometry")
                if geometry is not None:
                    row["geometry_json"] = json.dumps(geometry, ensure_ascii=False)
                rows.append(row)
            return [("features", pd.DataFrame(rows[:max_rows] if max_rows else rows))]
        if isinstance(data, dict):
            for key in ("data", "results", "registros", "items"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    return [(slugify(key), pd.json_normalize(rows[:max_rows] if max_rows else rows))]
            return [("data", pd.json_normalize(data))]
        if isinstance(data, list):
            return [("data", pd.json_normalize(data[:max_rows] if max_rows else data))]

    if fmt == "parquet":
        return [("data", pd.read_parquet(path))]

    raise ValueError(f"formato tabular nao suportado: {fmt}")


def import_file_to_sqlite(conn, path, resource, dataset, used_tables, max_rows=None):
    fmt = infer_format(resource) or path.suffix.lower().strip(".")
    dataset_slug = slugify(dataset_name(dataset), max_len=32)
    resource_slug = slugify(resource.get("name") or resource.get("id") or path.stem, max_len=32)
    total_rows = 0
    table_names = []

    for suffix, dataframe in read_dataframes(path, fmt, max_rows=max_rows):
        if dataframe.empty:
            continue
        table_name = unique_name(f"anm_{dataset_slug}_{resource_slug}_{suffix}", used_tables)
        rows = write_dataframe(conn, table_name, dataframe)
        total_rows += rows
        table_names.append(table_name)

    return ",".join(table_names), total_rows


def import_zip_to_sqlite(conn, zip_path, resource, dataset, used_tables, extract_dir, max_rows=None):
    total_rows = 0
    table_names = []
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            suffix = Path(member.filename).suffix.lower().strip(".")
            if suffix not in SUPPORTED_TABULAR_FORMATS:
                continue
            target = extract_dir / Path(member.filename).name
            with zf.open(member) as source, target.open("wb") as out:
                out.write(source.read())
            member_resource = dict(resource)
            member_resource["format"] = suffix
            member_resource["name"] = f"{resource.get('name') or resource.get('id')} - {Path(member.filename).name}"
            names, rows = import_file_to_sqlite(conn, target, member_resource, dataset, used_tables, max_rows=max_rows)
            if names:
                table_names.extend(names.split(","))
                total_rows += rows
    return ",".join(table_names), total_rows


def import_resource(conn, session, dataset, resource, run_id, files_dir, used_tables, max_rows=None, download_only=False):
    resource_id = upsert_resource(conn, dataset, resource)
    fmt = infer_format(resource)
    try:
        local_path = download_resource(session, resource, files_dir / dataset_name(dataset))
        table_names = None
        imported_rows = None
        if not download_only:
            if fmt == "zip" or local_path.suffix.lower() == ".zip":
                table_names, imported_rows = import_zip_to_sqlite(
                    conn,
                    local_path,
                    resource,
                    dataset,
                    used_tables,
                    files_dir / "_extracted" / resource_id,
                    max_rows=max_rows,
                )
            elif fmt in SUPPORTED_TABULAR_FORMATS or local_path.suffix.lower().strip(".") in SUPPORTED_TABULAR_FORMATS:
                table_names, imported_rows = import_file_to_sqlite(
                    conn,
                    local_path,
                    resource,
                    dataset,
                    used_tables,
                    max_rows=max_rows,
                )
            else:
                table_names = None
                imported_rows = 0
        upsert_resource(conn, dataset, resource, table_name=table_names, local_path=local_path, imported_rows=imported_rows)
        return 1 if imported_rows else 0
    except Exception as exc:
        insert_error(
            conn,
            run_id,
            dataset_id(dataset),
            resource_id,
            resource.get("url") or resource.get("downloadUrl"),
            "resource_import",
            exc,
        )
        return 0


def existing_table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def run(args):
    output_dir = Path(args.output_dir) if args.output_dir else get_persistent_data_dir("anm_dados_gov")
    db_path = Path(args.db_path) if args.db_path else output_dir / "anm_dados_gov.sqlite"
    files_dir = Path(args.files_dir) if args.files_dir else output_dir / "files"
    output_dir.mkdir(parents=True, exist_ok=True)

    session = create_session(args.token)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        started_at = utc_now()
        cur = conn.execute(
            "INSERT INTO import_runs(started_at, query, base_url) VALUES (?, ?, ?)",
            (started_at, args.query, BASE_URL),
        )
        run_id = cur.lastrowid
        conn.commit()

        used_tables = existing_table_names(conn)
        datasets_seen = {}
        resources_found = 0
        resources_imported = 0

        print(f"[INFO] Descobrindo conjuntos: source={args.source} query={args.query!r}")
        for raw_dataset in iter_catalog_datasets(session, args):
            if args.only_anm and not is_anm_dataset(raw_dataset):
                continue
            ident = dataset_id(raw_dataset) or dataset_name(raw_dataset)
            if ident in datasets_seen:
                continue
            detail = fetch_dataset_detail(session, raw_dataset) if args.fetch_details else raw_dataset
            datasets_seen[ident] = detail
            upsert_dataset(conn, detail)
            resources = get_resources(detail)
            resources_found += len(resources)
            print(f"[DATASET] {dataset_title(detail)} | recursos={len(resources)}")
            for resource in resources:
                upsert_resource(conn, detail, resource)
                if args.metadata_only:
                    continue
                resources_imported += import_resource(
                    conn,
                    session,
                    detail,
                    resource,
                    run_id,
                    files_dir,
                    used_tables,
                    max_rows=args.max_rows,
                    download_only=args.download_only,
                )
            conn.commit()

        errors = conn.execute("SELECT COUNT(*) FROM import_errors WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.execute(
            """
            UPDATE import_runs
            SET finished_at=?, datasets_found=?, resources_found=?, resources_imported=?, errors=?
            WHERE id=?
            """,
            (utc_now(), len(datasets_seen), resources_found, resources_imported, errors, run_id),
        )
        conn.commit()
        print(f"[OK] SQLite: {db_path}")
        print(
            f"[OK] datasets={len(datasets_seen)} recursos={resources_found} "
            f"recursos_importados={resources_imported} erros={errors}"
        )
    finally:
        conn.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Baixa datasets da ANM no dados.gov.br e converte recursos tabulares para SQLite."
    )
    parser.add_argument("--query", default="anm", help="Termo de busca no catalogo do dados.gov.br.")
    parser.add_argument("--token", default=None, help="Token Bearer do dados.gov.br. Alternativa: DADOS_GOV_BR_TOKEN.")
    parser.add_argument(
        "--source",
        choices=("auto", "dados-gov", "anm-direct"),
        default="auto",
        help="Fonte de descoberta. auto usa dados.gov.br quando autorizado e cai para dadosabertos.anm.gov.br sem token.",
    )
    parser.add_argument(
        "--anm-open-data-url",
        default=ANM_OPEN_DATA_URL,
        help="Raiz do diretorio aberto oficial da ANM para modo sem token.",
    )
    parser.add_argument("--db-path", default=None, help="Caminho do SQLite final.")
    parser.add_argument("--output-dir", default=None, help="Diretorio base para SQLite e arquivos baixados.")
    parser.add_argument("--files-dir", default=None, help="Diretorio para downloads dos recursos.")
    parser.add_argument("--page-size", type=int, default=100, help="Tamanho da pagina na API.")
    parser.add_argument("--max-pages", type=int, default=None, help="Limite opcional de paginas para teste.")
    parser.add_argument("--max-depth", type=int, default=6, help="Profundidade maxima no crawler direto da ANM.")
    parser.add_argument("--max-dirs", type=int, default=None, help="Limite opcional de diretorios no crawler direto da ANM.")
    parser.add_argument("--max-rows", type=int, default=None, help="Limite opcional de linhas por recurso para teste.")
    parser.add_argument("--fetch-details", action="store_true", help="Busca detalhe de cada dataset antes de importar recursos.")
    parser.add_argument("--include-non-open", action="store_true", help="Inclui conjuntos nao marcados como dados abertos.")
    parser.add_argument("--metadata-only", action="store_true", help="Salva apenas metadados de datasets/recursos.")
    parser.add_argument("--download-only", action="store_true", help="Baixa arquivos, mas nao cria tabelas tabulares.")
    parser.add_argument(
        "--no-only-anm",
        dest="only_anm",
        action="store_false",
        help="Nao filtra resultados por sinais de ANM/agencia nacional de mineracao.",
    )
    parser.set_defaults(only_anm=True)
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
