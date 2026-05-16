import argparse
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_cnpj_db_path


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_empresas_cnpj_basico ON empresas(cnpj_basico)",
    "CREATE INDEX IF NOT EXISTS idx_empresas_razao ON empresas(razao_social)",
    "CREATE INDEX IF NOT EXISTS idx_estab_cnpj_basico ON estabelecimentos(cnpj_basico)",
    "CREATE INDEX IF NOT EXISTS idx_estab_cnpj_completo ON estabelecimentos(cnpj_basico, cnpj_ordem, cnpj_dv)",
    "CREATE INDEX IF NOT EXISTS idx_estab_matriz ON estabelecimentos(cnpj_basico, cnpj_ordem)",
    "CREATE INDEX IF NOT EXISTS idx_estab_uf_municipio ON estabelecimentos(uf, municipio)",
    "CREATE INDEX IF NOT EXISTS idx_estab_cnae ON estabelecimentos(cnae_fiscal_principal)",
    "CREATE INDEX IF NOT EXISTS idx_estab_situacao ON estabelecimentos(situacao_cadastral)",
    "CREATE INDEX IF NOT EXISTS idx_estab_nome_fantasia ON estabelecimentos(nome_fantasia)",
    "CREATE INDEX IF NOT EXISTS idx_socios_cnpj_basico ON socios(cnpj_basico)",
    "CREATE INDEX IF NOT EXISTS idx_socios_nome ON socios(nome_socio_razao_social)",
    "CREATE INDEX IF NOT EXISTS idx_socios_doc ON socios(cpf_cnpj_socio)",
    "CREATE INDEX IF NOT EXISTS idx_cnaes_codigo ON cnaes(codigo)",
]


def table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def create_indexes(conn):
    for statement in INDEXES:
        table = statement.split(" ON ", 1)[1].split("(", 1)[0].strip()
        if table_exists(conn, table):
            print("[INDEX]", statement)
            conn.execute(statement)
    conn.commit()


def create_fts(conn):
    if table_exists(conn, "empresas"):
        print("[FTS] Recriando empresas_fts")
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
            SELECT cnpj_basico, razao_social
            FROM empresas
            WHERE razao_social IS NOT NULL AND razao_social <> ''
            """
        )
        conn.commit()

    if table_exists(conn, "estabelecimentos"):
        print("[FTS] Recriando estabelecimentos_fts")
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


def optimize(conn, vacuum=False):
    print("[SQLITE] ANALYZE")
    conn.execute("ANALYZE")
    conn.commit()
    print("[SQLITE] PRAGMA optimize")
    conn.execute("PRAGMA optimize")
    conn.commit()
    if vacuum:
        print("[SQLITE] VACUUM")
        conn.execute("VACUUM")
        conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Cria indices e FTS no SQLite CNPJ ja importado."
    )
    parser.add_argument("--db", default=str(get_cnpj_db_path()))
    parser.add_argument("--skip-fts", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Banco nao encontrado: {db_path}")

    print("[DB]", db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-200000")
        create_indexes(conn)
        if not args.skip_fts:
            create_fts(conn)
        optimize(conn, vacuum=args.vacuum)
        print("[OK] Otimizacao concluida.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
