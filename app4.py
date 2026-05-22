import os
import subprocess
import sys
import traceback
from pathlib import Path

import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import get_available_bases, get_documents_dir, get_persistent_data_dir  # noqa: E402
from src.sqlite_schema_library import (
    build_schema_library_for_base,
    find_dictionary_candidates,
    get_schema_library_path,
    is_dictionary_file,
)  # noqa: E402
from tools.import_tabular_sqlite import import_tabular_to_sqlite, slugify  # noqa: E402


st.set_page_config(
    layout="wide",
    page_title="Criar SQLite da Base",
    page_icon="🗄️",
)


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def list_tabular_files(source_dir: Path):
    if not source_dir.exists():
        return []
    return [
        path
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
        and not is_dictionary_file(path)
        and path.suffix.lower() in {".csv", ".txt", ".tsv", ".zip"}
    ]


st.title("Criar SQLite a partir de CSV/ZIP")

bases = get_available_bases()
if not bases:
    st.warning("Nenhuma base encontrada em base_rag/documentos.")
    st.stop()

base = st.sidebar.selectbox("Base", bases)
source_dir = get_documents_dir(base)
data_dir = get_persistent_data_dir(base)
default_sqlite = data_dir / f"{slugify(base)}.sqlite"

st.sidebar.caption(f"Origem: `{source_dir}`")
st.sidebar.caption(f"Destino: `{data_dir}`")

col1, col2 = st.sidebar.columns(2)
if col1.button("Abrir origem", use_container_width=True):
    open_folder(source_dir)
if col2.button("Abrir destino", use_container_width=True):
    open_folder(data_dir)

sqlite_name = st.text_input(
    "Nome do arquivo SQLite",
    value=default_sqlite.name,
    help="O arquivo sera criado em base_rag/data/<base>.",
)
chunksize = st.number_input("Linhas por lote", min_value=1000, max_value=500000, value=50000, step=5000)

sqlite_path = data_dir / sqlite_name
files = list_tabular_files(source_dir)
dictionary_candidates = find_dictionary_candidates(source_dir)

st.markdown("### Arquivos encontrados")
if files:
    st.dataframe(
        [
            {
                "arquivo": str(path.relative_to(source_dir)),
                "tipo": path.suffix.lower(),
                "tamanho_mb": round(path.stat().st_size / (1024 * 1024), 2),
            }
            for path in files
        ],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("Coloque arquivos `.csv`, `.txt`, `.tsv` ou `.zip` na pasta de documentos desta base.")

st.markdown("### Biblioteca de schemas")
schema_library_path = get_schema_library_path()
st.caption(f"Catalogo geral: `{schema_library_path}`")

dictionary_options = ["Sem dicionario"] + [str(path) for path in dictionary_candidates]
dictionary_choice = st.selectbox(
    "Dicionario de dados",
    dictionary_options,
    help=(
        "Opcional. Use CSV/XLSX com colunas como tabela, coluna, descricao, tipo e aliases. "
        "Nomes reconhecidos automaticamente: dicionario, dictionary, metadados ou metadata."
    ),
    format_func=lambda value: value if value == "Sem dicionario" else Path(value).name,
)
dictionary_path = None if dictionary_choice == "Sem dicionario" else Path(dictionary_choice)

if st.button("Recriar biblioteca de schemas desta base"):
    try:
        profiles = build_schema_library_for_base(base, dictionary_path=dictionary_path)
        st.success(f"Biblioteca atualizada com {len(profiles)} SQLite(s).")
        st.dataframe(
            [
                {
                    "sqlite": profile["db_name"],
                    "tabelas": len(profile.get("schema") or []),
                    "dicionario": profile.get("dictionary_entries") or 0,
                    "schema": str(Path(profile["db_path"]).with_suffix(".schema.json")),
                }
                for profile in profiles
            ],
            use_container_width=True,
            hide_index=True,
        )
    except Exception as exc:
        st.error(f"Erro ao recriar biblioteca de schemas: {exc}")

incremental_update = st.checkbox(
    "Atualizar incrementalmente",
    value=True,
    help="Importa apenas arquivos/membros ZIP novos ou alterados. O que nao mudou e pulado.",
)

overwrite = st.checkbox(
    "Recriar do zero",
    value=False,
    disabled=incremental_update,
    help="Quando marcado, apaga o SQLite de destino antes da importacao completa.",
)

if st.button("Criar/atualizar SQLite", type="primary", disabled=not files):
    if sqlite_path.exists() and not overwrite and not incremental_update:
        st.error("O SQLite de destino ja existe. Ative a atualizacao incremental, marque recriar do zero ou informe outro nome.")
    else:
        try:
            if sqlite_path.exists() and overwrite:
                sqlite_path.unlink()
            with st.spinner("Importando arquivos tabulares para SQLite..."):
                manifest = import_tabular_to_sqlite(
                    source_dir,
                    sqlite_path,
                    chunksize=int(chunksize),
                    base_name=base,
                    incremental=bool(incremental_update and sqlite_path.exists()),
                    dictionary_path=dictionary_path,
                )
            st.success(f"SQLite criado em: {sqlite_path}")
            bad_line_total = sum(int(item.get("bad_lines_skipped") or 0) for item in manifest["imported"])
            if bad_line_total:
                st.warning(
                    f"{bad_line_total} linha(s) com colunas a mais foram puladas. "
                    "Veja as colunas bad_lines_skipped e bad_line_examples abaixo."
                )
            st.markdown("### Tabelas importadas")
            st.dataframe(manifest["imported"], use_container_width=True, hide_index=True)
            st.caption(f"Manifesto: `{sqlite_path.with_suffix('.import_manifest.json')}`")
            st.caption(f"Schema: `{manifest.get('schema_path')}`")
            if dictionary_path:
                st.caption(f"Dicionario incorporado: `{dictionary_path}`")
        except Exception as exc:
            st.error(f"Erro ao criar SQLite: {exc}")
            with st.expander("Detalhes tecnicos do erro"):
                st.code(traceback.format_exc())
