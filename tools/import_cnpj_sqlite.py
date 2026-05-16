import argparse
import base64
import csv
import html.parser
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import PROJECT_ROOT as CONFIG_PROJECT_ROOT


DEFAULT_MONTH_URL = (
    "https://arquivos.receitafederal.gov.br/dados/cnpj/"
    "dados_abertos_cnpj/2023-05/"
)
BASE_INDEX_URL = (
    "https://arquivos.receitafederal.gov.br/dados/cnpj/"
    "dados_abertos_cnpj/"
)
DEFAULT_SHARE_URL = (
    "https://arquivos.receitafederal.gov.br/index.php/s/"
    "YggdBLfdninEJX9"
)

TABLE_COLUMNS = {
    "empresas": [
        "cnpj_basico",
        "razao_social",
        "natureza_juridica",
        "qualificacao_responsavel",
        "capital_social",
        "porte_empresa",
        "ente_federativo_responsavel",
    ],
    "estabelecimentos": [
        "cnpj_basico",
        "cnpj_ordem",
        "cnpj_dv",
        "matriz_filial",
        "nome_fantasia",
        "situacao_cadastral",
        "data_situacao_cadastral",
        "motivo_situacao_cadastral",
        "nome_cidade_exterior",
        "pais",
        "data_inicio_atividade",
        "cnae_fiscal_principal",
        "cnae_fiscal_secundaria",
        "tipo_logradouro",
        "logradouro",
        "numero",
        "complemento",
        "bairro",
        "cep",
        "uf",
        "municipio",
        "ddd1",
        "telefone1",
        "ddd2",
        "telefone2",
        "ddd_fax",
        "fax",
        "correio_eletronico",
        "situacao_especial",
        "data_situacao_especial",
    ],
    "socios": [
        "cnpj_basico",
        "identificador_socio",
        "nome_socio_razao_social",
        "cpf_cnpj_socio",
        "qualificacao_socio",
        "data_entrada_sociedade",
        "pais",
        "representante_legal",
        "nome_representante",
        "qualificacao_representante_legal",
        "faixa_etaria",
    ],
    "simples": [
        "cnpj_basico",
        "opcao_simples",
        "data_opcao_simples",
        "data_exclusao_simples",
        "opcao_mei",
        "data_opcao_mei",
        "data_exclusao_mei",
    ],
    "cnaes": ["codigo", "descricao"],
    "motivos": ["codigo", "descricao"],
    "municipios": ["codigo", "descricao"],
    "naturezas": ["codigo", "descricao"],
    "qualificacoes": ["codigo", "descricao"],
    "paises": ["codigo", "descricao"],
}

PREFIX_TO_TABLE = {
    "empresa": "empresas",
    "estabelecimento": "estabelecimentos",
    "socio": "socios",
    "simples": "simples",
    "cnae": "cnaes",
    "motivo": "motivos",
    "municipio": "municipios",
    "natureza": "naturezas",
    "qualificacao": "qualificacoes",
    "pais": "paises",
}


class LinkParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if href:
            self.links.append(href)


def normalize_name(name):
    name = Path(str(name)).name.lower()
    name = name.replace(".zip", "")
    return re.sub(r"[^a-z]", "", name)


def table_for_name(name):
    normalized = normalize_name(name)
    for prefix, table in PREFIX_TO_TABLE.items():
        if normalized.startswith(prefix):
            return table
    return None


def make_request(url, headers=None, method=None, data=None):
    request_headers = {
        "User-Agent": "Mozilla/5.0 graph_rag-cnpj-importer",
    }
    if headers:
        request_headers.update(headers)
    return urllib.request.Request(
        url,
        headers=request_headers,
        method=method,
        data=data,
    )


def share_auth_header(token):
    raw = f"{token}:".encode("utf-8")
    return {
        "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
    }


def parse_share_url(share_url):
    parsed = urllib.parse.urlparse(share_url)
    match = re.search(r"/index\.php/s/([^/]+)", parsed.path)
    if not match:
        raise SystemExit(
            "A URL compartilhada deve ter o formato "
            "https://arquivos.receitafederal.gov.br/index.php/s/<token>?dir=/AAAA-MM"
        )

    token = match.group(1)
    query = urllib.parse.parse_qs(parsed.query)
    share_path = query.get("dir", [""])[0] or query.get("path", [""])[0]
    if not share_path:
        share_path = "/"
    if not share_path.startswith("/"):
        share_path = "/" + share_path
    return token, share_path


def dav_url(token, share_path):
    quoted_path = "/".join(
        urllib.parse.quote(part)
        for part in share_path.strip("/").split("/")
        if part
    )
    suffix = f"{quoted_path}/" if quoted_path else ""
    return (
        "https://arquivos.receitafederal.gov.br/public.php/dav/files/"
        f"{token}/{suffix}"
    )


def create_schema(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")

    for table, columns in TABLE_COLUMNS.items():
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        column_sql = ", ".join(f"{column} TEXT" for column in columns)
        conn.execute(f"CREATE TABLE {table} ({column_sql})")

    conn.commit()


def create_indexes(conn):
    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_empresas_cnpj_basico ON empresas(cnpj_basico)",
        "CREATE INDEX IF NOT EXISTS idx_empresas_razao ON empresas(razao_social)",
        "CREATE INDEX IF NOT EXISTS idx_estab_cnpj_basico ON estabelecimentos(cnpj_basico)",
        "CREATE INDEX IF NOT EXISTS idx_estab_cnpj_completo ON estabelecimentos(cnpj_basico, cnpj_ordem, cnpj_dv)",
        "CREATE INDEX IF NOT EXISTS idx_estab_uf_municipio ON estabelecimentos(uf, municipio)",
        "CREATE INDEX IF NOT EXISTS idx_estab_cnae ON estabelecimentos(cnae_fiscal_principal)",
        "CREATE INDEX IF NOT EXISTS idx_estab_situacao ON estabelecimentos(situacao_cadastral)",
        "CREATE INDEX IF NOT EXISTS idx_socios_cnpj_basico ON socios(cnpj_basico)",
        "CREATE INDEX IF NOT EXISTS idx_socios_nome ON socios(nome_socio_razao_social)",
        "CREATE INDEX IF NOT EXISTS idx_simples_cnpj_basico ON simples(cnpj_basico)",
    ]
    for statement in index_statements:
        conn.execute(statement)

    # Busca textual eficiente para nome empresarial e fantasia.
    conn.execute("DROP TABLE IF EXISTS empresas_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE empresas_fts USING fts5(
            cnpj_basico UNINDEXED,
            razao_social,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO empresas_fts(cnpj_basico, razao_social)
        SELECT cnpj_basico, razao_social FROM empresas
        """
    )

    conn.execute("DROP TABLE IF EXISTS estabelecimentos_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE estabelecimentos_fts USING fts5(
            cnpj_basico UNINDEXED,
            cnpj_ordem UNINDEXED,
            cnpj_dv UNINDEXED,
            nome_fantasia,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO estabelecimentos_fts(
            cnpj_basico, cnpj_ordem, cnpj_dv, nome_fantasia
        )
        SELECT cnpj_basico, cnpj_ordem, cnpj_dv, nome_fantasia
        FROM estabelecimentos
        WHERE nome_fantasia IS NOT NULL AND nome_fantasia <> ''
        """
    )
    conn.commit()


def list_month_zips(month_url):
    parser = LinkParser()
    try:
        request = make_request(month_url)
        with urllib.request.urlopen(request) as response:
            parser.feed(response.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(
                "A pasta mensal informada nao existe na Receita Federal: "
                f"{month_url}\n"
                "Abra o indice oficial e escolha uma competencia existente: "
                f"{BASE_INDEX_URL}\n"
                "Se estiver usando o link compartilhado da Receita, use "
                "--share-url em vez de --month-url."
            ) from exc
        raise

    urls = []
    for href in parser.links:
        if href.lower().endswith(".zip"):
            urls.append(urllib.parse.urljoin(month_url, href))
    return sorted(set(urls))


def list_share_zips(share_url):
    token, share_path = parse_share_url(share_url)
    url = dav_url(token, share_path)
    headers = share_auth_header(token)
    headers["Depth"] = "1"
    request = make_request(url, headers=headers, method="PROPFIND")

    try:
        with urllib.request.urlopen(request) as response:
            xml_data = response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            "Nao consegui listar a pasta compartilhada da Receita: "
            f"{share_url}\nHTTP {exc.code}: {exc.reason}"
        ) from exc

    namespace = {"d": "DAV:"}
    root = ET.fromstring(xml_data)
    zip_items = []
    for response_node in root.findall("d:response", namespace):
        href_node = response_node.find("d:href", namespace)
        if href_node is None or not href_node.text:
            continue
        href = urllib.parse.unquote(href_node.text)
        if not href.lower().endswith(".zip"):
            continue
        name = Path(href).name
        file_path = share_path.rstrip("/") + "/" + name
        zip_items.append((token, file_path, name))

    return sorted(set(zip_items), key=lambda item: item[2].lower())


def download_file(url, target_dir, headers=None):
    target = target_dir / Path(urllib.parse.urlparse(url).path).name
    if target.exists() and target.stat().st_size > 0:
        print(f"[DOWNLOAD] Ja existe: {target.name}")
        return target

    print(f"[DOWNLOAD] {url}")
    temp_target = target.with_suffix(target.suffix + ".part")
    request = make_request(url, headers=headers)
    with urllib.request.urlopen(request) as response, temp_target.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    temp_target.replace(target)
    return target


def download_share_file(token, file_path, name, target_dir):
    target = target_dir / name
    if target.exists() and target.stat().st_size > 0:
        print(f"[DOWNLOAD] Ja existe: {target.name}")
        return target

    url = dav_url(token, file_path)
    print(f"[DOWNLOAD] {name}")
    temp_target = target.with_suffix(target.suffix + ".part")
    request = make_request(url, headers=share_auth_header(token))
    with urllib.request.urlopen(request) as response, temp_target.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    temp_target.replace(target)
    return target


def collect_zip_paths(args, work_dir):
    paths = []

    if args.source_zip:
        paths.append(Path(args.source_zip))

    if args.source_dir:
        paths.extend(sorted(Path(args.source_dir).glob("*.zip")))

    if args.download:
        download_dir = Path(args.download_dir or (work_dir / "downloads"))
        download_dir.mkdir(parents=True, exist_ok=True)
        if args.share_url:
            for token, file_path, name in list_share_zips(args.share_url):
                paths.append(download_share_file(token, file_path, name, download_dir))
        else:
            month_url = args.month_url.rstrip("/") + "/"
            for url in list_month_zips(month_url):
                paths.append(download_file(url, download_dir))

    unique = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def materialize_nested_zips(zip_paths, work_dir):
    result = []
    nested_dir = work_dir / "nested_zips"
    nested_dir.mkdir(parents=True, exist_ok=True)

    for zip_path in zip_paths:
        table = table_for_name(zip_path.name)
        if table:
            result.append(zip_path)
            continue

        print(f"[ZIP] Procurando ZIPs internos em {zip_path.name}")
        with zipfile.ZipFile(zip_path) as outer_zip:
            for member in outer_zip.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".zip"):
                    continue
                output_path = nested_dir / Path(member.filename).name
                if output_path.exists() and output_path.stat().st_size > 0:
                    result.append(output_path)
                    continue
                print(f"[ZIP] Extraindo interno: {member.filename}")
                with outer_zip.open(member) as source, output_path.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                result.append(output_path)

    return sorted(set(result))


def iter_zip_csv_rows(zip_path, expected_columns):
    with zipfile.ZipFile(zip_path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members:
            return

        for member in members:
            if member.filename.lower().endswith(".zip"):
                continue
            with archive.open(member) as raw_file:
                text_file = (
                    line.decode("latin1", errors="replace")
                    for line in raw_file
                )
                reader = csv.reader(text_file, delimiter=";", quotechar='"')
                for row in reader:
                    if not row:
                        continue
                    if len(row) < expected_columns:
                        row = row + [""] * (expected_columns - len(row))
                    elif len(row) > expected_columns:
                        row = row[:expected_columns]
                    yield row


def import_zip(conn, zip_path, batch_size):
    table = table_for_name(zip_path.name)
    if not table:
        print(f"[IGNORADO] Tipo nao reconhecido: {zip_path.name}")
        return 0

    columns = TABLE_COLUMNS[table]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    insert_sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"

    print(f"[IMPORT] {zip_path.name} -> {table}")
    total = 0
    batch = []
    for row in iter_zip_csv_rows(zip_path, len(columns)):
        batch.append(row)
        if len(batch) >= batch_size:
            conn.executemany(insert_sql, batch)
            total += len(batch)
            batch.clear()
            if total % (batch_size * 10) == 0:
                print(f"[IMPORT] {zip_path.name}: {total:,} linhas")

    if batch:
        conn.executemany(insert_sql, batch)
        total += len(batch)
    conn.commit()
    print(f"[IMPORT] {zip_path.name}: {total:,} linhas importadas")
    return total


def vacuum_analyze(conn):
    print("[SQLITE] ANALYZE")
    conn.execute("ANALYZE")
    conn.commit()
    print("[SQLITE] VACUUM")
    conn.execute("VACUUM")
    conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Baixa/descompacta os dados publicos de CNPJ da Receita Federal "
            "e monta um banco SQLite consultavel."
        )
    )
    parser.add_argument(
        "--db",
        default=str(CONFIG_PROJECT_ROOT / "data_cnpj" / "cnpj_2023_05.sqlite"),
        help="Caminho do arquivo SQLite de saida.",
    )
    parser.add_argument(
        "--month-url",
        default=DEFAULT_MONTH_URL,
        help="URL da pasta mensal da Receita Federal.",
    )
    parser.add_argument(
        "--share-url",
        help=(
            "URL compartilhada da Receita/Nextcloud, por exemplo "
            f"{DEFAULT_SHARE_URL}?dir=/2026-01."
        ),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Baixa todos os ZIPs encontrados em --month-url.",
    )
    parser.add_argument(
        "--download-dir",
        help="Pasta onde os ZIPs baixados serao mantidos.",
    )
    parser.add_argument(
        "--source-zip",
        help="ZIP local ja baixado. Pode ser um ZIP unico contendo outros ZIPs.",
    )
    parser.add_argument(
        "--source-dir",
        help="Pasta local contendo ZIPs da Receita.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50000,
        help="Quantidade de linhas por lote de INSERT.",
    )
    parser.add_argument(
        "--skip-indexes",
        action="store_true",
        help="Importa sem criar indices/FTS no final.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Mantem arquivos temporarios extraidos.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.download and not args.source_zip and not args.source_dir:
        raise SystemExit(
            "Informe --download, --source-zip ou --source-dir. "
            "Exemplo: python tools/import_cnpj_sqlite.py --download"
        )

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    temp_context = tempfile.TemporaryDirectory(prefix="cnpj_import_")
    work_dir = Path(temp_context.name)

    try:
        zip_paths = collect_zip_paths(args, work_dir)
        zip_paths = materialize_nested_zips(zip_paths, work_dir)
        if not zip_paths:
            raise SystemExit("Nenhum ZIP de CNPJ encontrado para importar.")

        if db_path.exists():
            db_path.unlink()

        conn = sqlite3.connect(db_path)
        try:
            create_schema(conn)
            totals = {}
            for zip_path in zip_paths:
                table = table_for_name(zip_path.name)
                imported = import_zip(conn, zip_path, args.batch_size)
                if table:
                    totals[table] = totals.get(table, 0) + imported

            if not args.skip_indexes:
                print("[SQLITE] Criando indices e FTS")
                create_indexes(conn)
                vacuum_analyze(conn)

            print("[OK] Banco criado:", db_path)
            for table in sorted(totals):
                print(f"[OK] {table}: {totals[table]:,} linhas")
        finally:
            conn.close()
    finally:
        if args.keep_workdir:
            print("[TEMP] Mantido em:", work_dir)
        else:
            temp_context.cleanup()


if __name__ == "__main__":
    main()
