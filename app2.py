import os
import sys
import time
import re
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import (  # noqa: E402
    DOCUMENTOS_ROOT,
    PERSISTENT_DATA_ROOT,
    get_available_bases,
    get_documents_dir,
    get_default_base,
)


def _init_state():
    defaults = {
        "idx_process": None,
        "idx_running": False,
        "idx_pid": None,
        "idx_log_path": "",
        "idx_log_max_lines": 500,
        "idx_command": [],
        "idx_mode": "Rede (index.py)",
        "idx_all_bases": False,
        "idx_checkpoint_batches": 25,
        "idx_batch_retries": 8,
        "idx_retry_wait_seconds": 20,
        "idx_checkpoint_faiss_network": True,
        "idx_faiss_work_root": str(PERSISTENT_DATA_ROOT),
        "idx_metadata_csv": "",
        "idx_rebuild_index": False,
        "idx_last_exit_code": None,
        "idx_last_error": "",
        "idx_auto_refresh": True,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _tail_text(text: str, max_lines: int) -> tuple[str, int, int]:
    if not text:
        return "", 0, 0

    max_lines = max(1, int(max_lines))
    lines = text.splitlines()
    total = len(lines)

    if total <= max_lines:
        return text, total, total

    tail = "\n".join(lines[-max_lines:])
    return tail, max_lines, total


def _open_folder(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return ""
    except Exception as exc:
        return str(exc)


def _find_metadata_csv_candidates() -> list[str]:
    roots = [BASE_DIR, PERSISTENT_DATA_ROOT]
    names = {"metadados.csv", "metadata.csv", "index.csv"}
    candidates = []
    seen = set()

    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        try:
            for path in root.rglob("*.csv"):
                if not path.is_file():
                    continue
                lower_name = path.name.lower()
                if path.suffix.lower() != ".csv":
                    continue
                if ".bak" in lower_name:
                    continue
                if lower_name not in names and "metadad" not in lower_name and "metadata" not in lower_name:
                    continue
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    display = str(path.relative_to(BASE_DIR))
                except ValueError:
                    display = str(path)
                candidates.append(display)
        except Exception:
            continue

    return sorted(candidates, key=lambda value: (0 if "sr2" in value.lower() else 1, value.lower()))


def _select_metadata_candidate():
    selected = st.session_state.get("idx_metadata_candidate")
    if selected:
        st.session_state["idx_metadata_csv"] = selected


def _render_running_status(pid: int | None):
    pid_text = f"PID {pid}" if pid else "PID -"
    st.markdown(
        f"""
<style>
.idx-running-status {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.75rem 1rem;
    border-radius: 0.5rem;
    border: 1px solid rgba(40, 167, 69, 0.35);
    background: rgba(40, 167, 69, 0.12);
    color: var(--text-color);
    font-weight: 600;
}}
.idx-running-dot {{
    width: 14px;
    height: 14px;
    border: 2px solid rgba(40, 167, 69, 0.35);
    border-top-color: #28a745;
    border-radius: 50%;
    animation: idx-spin 0.9s linear infinite;
    flex-shrink: 0;
}}
@keyframes idx-spin {{
    to {{ transform: rotate(360deg); }}
}}
</style>
<div class="idx-running-status">
  <span class="idx-running-dot"></span>
  <span>Status: executando ({pid_text})</span>
</div>
""",
        unsafe_allow_html=True,
    )


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _extract_stage_progress(log_text: str) -> dict:
    result = {
        "metadata_pct": 0.0,
        "metadata_detail": "Aguardando metadados",
        "chunks_pct": 0.0,
        "chunks_detail": "Aguardando inicio",
        "batch_pct": 0.0,
        "batch_detail": "Aguardando etapa de batches",
    }

    if not log_text.strip():
        return result

    # Em --all, mostramos progresso da base ativa mais recente.
    marker = "===== BASE ATIVA:"
    if marker in log_text:
        segment = marker + log_text.rsplit(marker, 1)[-1]
    else:
        segment = log_text

    metadata_match = re.findall(
        r"\[METADADOS\]\s+enriquecendo chunks:\s+(\d+)/(\d+)\s+\(([\d.]+)%\)",
        segment,
    )
    metadata_consensus_match = re.findall(
        r"\[METADADOS\]\s+consenso por documento:\s+(\d+)/(\d+)\s+\(([\d.]+)%\)",
        segment,
    )
    if "[METADADOS] Enriquecendo chunks com:" in segment:
        result["metadata_pct"] = 5.0
        result["metadata_detail"] = "Carregando CSV de metadados"
    if "[METADADOS] CSV carregado:" in segment:
        result["metadata_pct"] = 20.0
        result["metadata_detail"] = "CSV carregado"
    if "[METADADOS] chunks carregados:" in segment:
        result["metadata_pct"] = 30.0
        result["metadata_detail"] = "Chunks carregados"
    if metadata_match:
        done, total, pct = metadata_match[-1]
        scaled_pct = 30.0 + (float(pct) * 0.45)
        result["metadata_pct"] = _clamp_pct(scaled_pct)
        result["metadata_detail"] = f"Enriquecendo chunks {done}/{total}"
    if metadata_consensus_match:
        done, total, pct = metadata_consensus_match[-1]
        scaled_pct = 75.0 + (float(pct) * 0.15)
        result["metadata_pct"] = _clamp_pct(scaled_pct)
        result["metadata_detail"] = f"Consenso por documento {done}/{total}"
    if "[METADADOS] gravando chunks enriquecidos" in segment:
        result["metadata_pct"] = 95.0
        result["metadata_detail"] = "Gravando chunks enriquecidos"
    if "[METADADOS] Enriquecimento concluido." in segment:
        result["metadata_pct"] = 100.0
        result["metadata_detail"] = "Metadados concluidos"

    docs_match = re.findall(
        r"\[DOCS\]\s+(\d+)\s+arquivo\(s\)\s+para processar\.",
        segment,
    )
    docs_total = int(docs_match[-1]) if docs_match else 0

    doc_progress = re.findall(
        r"\[PROGRESS\]\s+documento\s+(\d+)/(\d+)\s+\(([\d.]+)%\)",
        segment,
    )
    completed_docs = len(
        re.findall(
            r"^\[OK\]\s+.+\s+->\s+\d+\s+chunks$",
            segment,
            flags=re.MULTILINE,
        )
    )
    completed_docs += len(
        re.findall(
            r"^\[OK\]\s+.+\s+ja estava concluido no checkpoint\.$",
            segment,
            flags=re.MULTILINE,
        )
    )

    batch_started = any(
        token in segment
        for token in (
            "novos chunks para indexar",
            "[PROGRESS] batch",
            "[OK] Nenhum novo chunk para indexar.",
            "Reconstruindo BM25",
            "Construindo indice lexical avancado",
            "[CHECKPOINT] Persistencia final concluida.",
        )
    )

    if docs_total > 0:
        docs_done = min(completed_docs, docs_total)
        chunks_pct = (docs_done / docs_total) * 100.0

        if doc_progress:
            last_doc_no, last_doc_total, _ = doc_progress[-1]
            if int(last_doc_total) == docs_total:
                # Quando um documento entra em processamento, avanca para o
                # inicio da faixa dele (sem marcar 100% prematuro).
                doc_floor = ((max(int(last_doc_no) - 1, 0)) / docs_total) * 100.0
                chunks_pct = max(chunks_pct, doc_floor)

        if batch_started:
            chunks_pct = 100.0

        result["chunks_pct"] = _clamp_pct(chunks_pct)
        result["chunks_detail"] = f"{docs_done}/{docs_total} documentos concluidos"
    else:
        if batch_started or "[OK] Indexacao concluida." in segment:
            result["chunks_pct"] = 100.0
            result["chunks_detail"] = "Sem documentos para gerar chunks"

    batch_progress = re.findall(
        r"\[PROGRESS\]\s+batch\s+(\d+)/(\d+)\s+\(([\d.]+)%\)",
        segment,
    )
    batch_totals = re.findall(
        r"(\d+)\s+novos chunks para indexar\s+\((\d+)\s+batches\)\.",
        segment,
    )

    if "[OK] Nenhum novo chunk para indexar." in segment:
        result["batch_pct"] = 100.0
        result["batch_detail"] = "Sem novos chunks para indexar"
    elif batch_progress:
        batch_no, batch_total, batch_pct = batch_progress[-1]
        result["batch_pct"] = _clamp_pct(float(batch_pct))
        result["batch_detail"] = f"Lote {batch_no}/{batch_total}"
    elif batch_totals:
        _, batch_total = batch_totals[-1]
        result["batch_pct"] = 0.0
        result["batch_detail"] = f"Preparando batches (0/{batch_total})"

    if (
        "[CHECKPOINT] Persistencia final concluida." in segment
        or "Reconstruindo BM25" in segment
        or "[OK] Indexacao concluida." in segment
    ):
        result["batch_pct"] = 100.0
        if result["batch_detail"].startswith("Aguardando"):
            result["batch_detail"] = "Batches concluidos"

    result["chunks_pct"] = _clamp_pct(result["chunks_pct"])
    result["metadata_pct"] = _clamp_pct(result["metadata_pct"])
    result["batch_pct"] = _clamp_pct(result["batch_pct"])
    return result


def _render_stage_progress(progress: dict):
    chunks_pct = _clamp_pct(progress.get("chunks_pct", 0.0))
    metadata_pct = _clamp_pct(progress.get("metadata_pct", 0.0))
    batch_pct = _clamp_pct(progress.get("batch_pct", 0.0))
    chunks_detail = progress.get("chunks_detail", "")
    metadata_detail = progress.get("metadata_detail", "")
    batch_detail = progress.get("batch_detail", "")

    st.markdown("### Progresso das etapas")

    st.caption(f"Criacao de chunks: {chunks_pct:.1f}% | {chunks_detail}")
    st.progress(int(round(chunks_pct)))

    st.caption(f"Enriquecimento de metadados: {metadata_pct:.1f}% | {metadata_detail}")
    st.progress(int(round(metadata_pct)))

    st.caption(f"Indexacao em batches: {batch_pct:.1f}% | {batch_detail}")
    st.progress(int(round(batch_pct)))


def _refresh_process_state():
    proc = st.session_state.get("idx_process")
    if proc is None:
        return

    code = proc.poll()
    if code is None:
        st.session_state["idx_running"] = True
        st.session_state["idx_pid"] = proc.pid
        return

    st.session_state["idx_running"] = False
    st.session_state["idx_last_exit_code"] = code
    st.session_state["idx_process"] = None


def _build_command(
    mode: str,
    all_bases: bool,
    base_name: str,
    checkpoint_batches: int,
    batch_retries: int,
    retry_wait_seconds: int,
    checkpoint_faiss_network: bool,
    metadata_csv: str = "",
    rebuild_index: bool = False,
):
    script_name = "index.py" if mode.startswith("Rede") else "index2.py"
    cmd = [sys.executable, "-u", str(BASE_DIR / script_name)]

    if all_bases:
        cmd.append("--all")
    else:
        cmd.extend(["--base", base_name])

    cmd.extend(
        [
            "--checkpoint-batches",
            str(max(1, int(checkpoint_batches))),
            "--batch-retries",
            str(max(0, int(batch_retries))),
            "--retry-wait-seconds",
            str(max(0, int(retry_wait_seconds))),
        ]
    )

    if checkpoint_faiss_network:
        cmd.append("--checkpoint-faiss-network")
    else:
        cmd.append("--no-checkpoint-faiss-network")

    metadata_csv = (metadata_csv or "").strip()
    if metadata_csv:
        cmd.extend(["--metadata-csv", metadata_csv])

    if rebuild_index:
        cmd.append("--rebuild-index")

    return cmd


def _start_indexing(
    mode: str,
    all_bases: bool,
    base_name: str,
    checkpoint_batches: int,
    batch_retries: int,
    retry_wait_seconds: int,
    checkpoint_faiss_network: bool,
    faiss_work_root: str,
    metadata_csv: str,
    rebuild_index: bool,
):
    _refresh_process_state()
    if st.session_state.get("idx_running"):
        st.warning("Ja existe uma indexacao em execucao.")
        return

    cmd = _build_command(
        mode=mode,
        all_bases=all_bases,
        base_name=base_name,
        checkpoint_batches=checkpoint_batches,
        batch_retries=batch_retries,
        retry_wait_seconds=retry_wait_seconds,
        checkpoint_faiss_network=checkpoint_faiss_network,
        metadata_csv=metadata_csv,
        rebuild_index=rebuild_index,
    )

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    if mode.startswith("Rede"):
        root = (faiss_work_root or "").strip()
        if not root:
            st.error("Informe um caminho valido para RAG_FAISS_WORK_ROOT.")
            return
        Path(root).mkdir(parents=True, exist_ok=True)
        env["RAG_FAISS_WORK_ROOT"] = root
    else:
        env.pop("RAG_FAISS_WORK_ROOT", None)

    logs_dir = BASE_DIR / "logs_indexacao"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_tag = "rede" if mode.startswith("Rede") else "c"
    log_path = logs_dir / f"indexacao_{mode_tag}_{ts}.log"

    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    st.session_state["idx_process"] = proc
    st.session_state["idx_running"] = True
    st.session_state["idx_pid"] = proc.pid
    st.session_state["idx_log_path"] = str(log_path)
    st.session_state["idx_command"] = cmd
    st.session_state["idx_last_exit_code"] = None
    st.session_state["idx_last_error"] = ""


def _stop_indexing():
    _refresh_process_state()
    proc = st.session_state.get("idx_process")

    if proc is None or proc.poll() is not None:
        st.warning("Nao ha indexacao em execucao.")
        st.session_state["idx_running"] = False
        return

    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    finally:
        st.session_state["idx_running"] = False
        st.session_state["idx_process"] = None


st.set_page_config(
    layout="wide",
    page_title="Indexacao RAG - App2",
)

_init_state()
_refresh_process_state()

st.title("Indexacao RAG - App2")

bases = get_available_bases()
default_base = get_default_base()
if default_base not in bases and bases:
    default_base = bases[0]

with st.sidebar:
    st.header("Indexacao")

    mode = st.radio(
        "Modo",
        ["Rede (index.py)", "C: (index2.py)"],
        key="idx_mode",
    )

    all_bases = st.checkbox("Indexar todas as bases (--all)", key="idx_all_bases")

    selected_base = default_base
    if not all_bases:
        if not bases:
            st.warning("Nenhuma base encontrada em documentos.")
            selected_base = ""
        else:
            selected_base = st.selectbox(
                "Base",
                bases,
                index=bases.index(default_base) if default_base in bases else 0,
            )

    if all_bases:
        st.caption(f"Pasta de bases: `{DOCUMENTOS_ROOT}`")
        if st.button("Abrir pasta das bases", use_container_width=True):
            open_error = _open_folder(DOCUMENTOS_ROOT)
            if open_error:
                st.error(f"Erro ao abrir pasta: {open_error}")
    else:
        base_folder = get_documents_dir(selected_base) if selected_base else DOCUMENTOS_ROOT
        st.caption(f"Pasta da base: `{base_folder}`")
        if st.button(
            "Abrir pasta da base",
            use_container_width=True,
            disabled=not bool(selected_base),
        ):
            open_error = _open_folder(base_folder)
            if open_error:
                st.error(f"Erro ao abrir pasta: {open_error}")

    st.number_input(
        "Checkpoint batches",
        min_value=1,
        value=int(st.session_state["idx_checkpoint_batches"]),
        step=1,
        key="idx_checkpoint_batches",
    )
    st.number_input(
        "Batch retries",
        min_value=0,
        value=int(st.session_state["idx_batch_retries"]),
        step=1,
        key="idx_batch_retries",
    )
    st.number_input(
        "Retry wait (s)",
        min_value=0,
        value=int(st.session_state["idx_retry_wait_seconds"]),
        step=1,
        key="idx_retry_wait_seconds",
    )
    st.checkbox(
        "Checkpoint FAISS na rede",
        key="idx_checkpoint_faiss_network",
    )

    if st.button("Localizar metadados CSV", use_container_width=True):
        st.session_state["idx_metadata_candidates"] = _find_metadata_csv_candidates()
        st.session_state["idx_metadata_picker_visible"] = True

    metadata_candidates = st.session_state.get("idx_metadata_candidates", [])
    if st.session_state.get("idx_metadata_picker_visible"):
        if metadata_candidates:
            current_value = st.session_state.get("idx_metadata_csv") or metadata_candidates[0]
            index = metadata_candidates.index(current_value) if current_value in metadata_candidates else 0
            st.selectbox(
                "CSV localizado",
                metadata_candidates,
                index=index,
                key="idx_metadata_candidate",
                on_change=_select_metadata_candidate,
            )
            if not st.session_state.get("idx_metadata_csv"):
                st.session_state["idx_metadata_csv"] = metadata_candidates[index]
            st.caption("Ao selecionar um item, o caminho abaixo e preenchido automaticamente.")
        else:
            st.warning("Nenhum CSV de metadados encontrado no workspace.")

    st.text_input(
        "Metadados CSV (--metadata-csv)",
        key="idx_metadata_csv",
        help=(
            "Caminho do metadados.csv usado para enriquecer os chunks antes da indexacao. "
            "Se vazio, o indexador tenta localizar metadados na pasta data da base."
        ),
    )
    st.checkbox(
        "Recriar FAISS/id_map (--rebuild-index)",
        key="idx_rebuild_index",
        help="Use quando os chunks foram enriquecidos depois de o FAISS ja ter sido criado.",
    )

    if mode.startswith("Rede"):
        st.text_input(
            "RAG_FAISS_WORK_ROOT",
            key="idx_faiss_work_root",
            help="Ex.: Z:\\RAG_Compartilhado\\base_rag\\data",
        )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Iniciar", type="primary", use_container_width=True):
            _start_indexing(
                mode=mode,
                all_bases=all_bases,
                base_name=selected_base,
                checkpoint_batches=st.session_state["idx_checkpoint_batches"],
                batch_retries=st.session_state["idx_batch_retries"],
                retry_wait_seconds=st.session_state["idx_retry_wait_seconds"],
                checkpoint_faiss_network=st.session_state["idx_checkpoint_faiss_network"],
                faiss_work_root=st.session_state["idx_faiss_work_root"],
                metadata_csv=st.session_state["idx_metadata_csv"],
                rebuild_index=st.session_state["idx_rebuild_index"],
            )
            st.rerun()
    with c2:
        if st.button("Interromper", use_container_width=True):
            _stop_indexing()
            st.rerun()

    st.markdown("---")
    st.checkbox("Auto atualizar (2s)", key="idx_auto_refresh")
    st.number_input(
        "Linhas do log na tela",
        min_value=100,
        max_value=10000,
        value=int(st.session_state["idx_log_max_lines"]),
        step=100,
        key="idx_log_max_lines",
    )

    cmd_preview = _build_command(
        mode=mode,
        all_bases=all_bases,
        base_name=selected_base or "",
        checkpoint_batches=st.session_state["idx_checkpoint_batches"],
        batch_retries=st.session_state["idx_batch_retries"],
        retry_wait_seconds=st.session_state["idx_retry_wait_seconds"],
        checkpoint_faiss_network=st.session_state["idx_checkpoint_faiss_network"],
        metadata_csv=st.session_state["idx_metadata_csv"],
        rebuild_index=st.session_state["idx_rebuild_index"],
    )
    st.caption("Comando previsto")
    st.code(" ".join(cmd_preview), language="bash")


if st.session_state.get("idx_running"):
    _render_running_status(st.session_state.get("idx_pid"))
else:
    code = st.session_state.get("idx_last_exit_code")
    if code is None:
        st.info("Status: aguardando inicio.")
    elif code == 0:
        st.success("Status: finalizado com sucesso (codigo 0).")
    else:
        st.error(f"Status: finalizado com erro (codigo {code}).")

log_path_raw = st.session_state.get("idx_log_path") or ""
log_text = ""
if log_path_raw:
    log_path = Path(log_path_raw)
    log_text = _safe_read_text(log_path)
    _render_stage_progress(_extract_stage_progress(log_text))
    st.write(f"Log: `{log_path}`")

    if log_text:
        display_log, shown_lines, total_lines = _tail_text(
            log_text,
            int(st.session_state.get("idx_log_max_lines", 500)),
        )
        if total_lines > shown_lines:
            st.caption(f"Mostrando ultimas {shown_lines} de {total_lines} linhas.")
        st.download_button(
            "Baixar log completo",
            data=log_text,
            file_name=log_path.name,
            mime="text/plain",
        )
    else:
        display_log = ""

    st.code(display_log if display_log else "(sem saida ainda)", language="text")
else:
    st.code("(nenhum log iniciado)", language="text")

if st.button("Atualizar agora"):
    st.rerun()

if st.session_state.get("idx_running") and st.session_state.get("idx_auto_refresh"):
    time.sleep(2)
    st.rerun()
