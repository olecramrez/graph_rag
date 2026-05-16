import fitz  # pymupdf
import io
import json
import hashlib
import re
from typing import List, Dict, Callable, Optional
from pathlib import Path
from PIL import Image
import pytesseract

from .safe_jsonl import load_valid_jsonl, atomic_write_jsonl
from .config import (
    PROJECT_ROOT,
    get_documents_dir,
    get_chunks_path,
    get_chunk_checkpoint_path,
)


# ==========================================================
# CONFIGURAÇÃO TESSERACT (relativo ao projeto)
# ==========================================================

TESSERACT_PATH = PROJECT_ROOT / "tesseract" / "tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_PATH)

try:
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
except Exception:
    pass


# ==========================================================
# CONFIGURAÇÕES DE CHUNK
# ==========================================================

CHUNK_SIZE = 1800
CHUNK_OVERLAP = 300
MIN_CHUNK_SIZE = 450
MIN_TEXT_THRESHOLD = 50


# ==========================================================
# LIMPEZA DE TEXTO EXTRAIDO
# ==========================================================

PRINT_STAMP_RE = re.compile(
    r"^\s*\d{1,2}/\d{1,2}/(?:19|20)\d{2}\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?\s+"
)

LEGISLATIVE_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?="
    r"(?:Art\.?\s*\d+[ºo]?(?:-[A-Z])?\.?)|"
    r"(?:§\s*\d+[ºo]?\.?)|"
    r"(?:Par[aá]grafo\s+único\.?)|"
    r"(?:[IVXLCDM]+\s*[-–])|"
    r"(?:[a-z]\)\s+)"
    r")"
)

PARAGRAPH_RE = re.compile(r"\n\s*\n+")
SENTENCE_RE = re.compile(r"(?<=[.;:!?])\s+")


def clean_extracted_text(text: str) -> str:
    if not text:
        return ""
    return PRINT_STAMP_RE.sub("", text, count=1).lstrip()


def _select_overlap_blocks(blocks: List[str], max_chars: int = CHUNK_OVERLAP) -> List[str]:
    selected: List[str] = []
    total = 0

    for block in reversed(blocks):
        block_len = len(block)
        separator_len = 2 if selected else 0
        if total + separator_len + block_len > max_chars:
            break
        selected.append(block)
        total += separator_len + block_len

    return list(reversed(selected))


def _split_long_block(text: str, chunk_size: int = CHUNK_SIZE) -> List[str]:
    parts = [part.strip() for part in PARAGRAPH_RE.split(text) if part.strip()]
    if len(parts) <= 1:
        parts = [part.strip() for part in SENTENCE_RE.split(text) if part.strip()]
    if len(parts) <= 1:
        chunks = []
        start = 0
        while start < len(text):
            chunks.append(text[start : start + chunk_size].strip())
            start += max(1, chunk_size - CHUNK_OVERLAP)
        return [chunk for chunk in chunks if chunk]

    return _pack_blocks(parts, chunk_size=chunk_size)


def _split_legislative_blocks(text: str) -> List[str]:
    positions = [match.start() for match in LEGISLATIVE_BOUNDARY_RE.finditer(text)]
    if not positions or positions[0] != 0:
        positions.insert(0, 0)
    positions = sorted(set(positions))

    blocks = []
    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)

    if len(blocks) <= 1:
        blocks = [part.strip() for part in PARAGRAPH_RE.split(text) if part.strip()]

    return blocks or [text.strip()]


def _pack_blocks(blocks: List[str], chunk_size: int = CHUNK_SIZE) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        if len(block) > chunk_size:
            if current:
                chunks.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            chunks.extend(_split_long_block(block, chunk_size=chunk_size))
            continue

        separator_len = 2 if current else 0
        would_fit = current_len + separator_len + len(block) <= chunk_size
        if would_fit or current_len < MIN_CHUNK_SIZE:
            current.append(block)
            current_len += separator_len + len(block)
            continue

        emitted = "\n\n".join(current).strip()
        chunks.append(emitted)

        overlap_blocks = _select_overlap_blocks(current)
        current = [*overlap_blocks, block]
        current_len = sum(len(part) for part in current) + 2 * (len(current) - 1)

    if current:
        chunks.append("\n\n".join(current).strip())

    return [chunk for chunk in chunks if chunk]


# ==========================================================
# OCR
# ==========================================================

def ocr_page(page) -> str:
    try:
        pix = page.get_pixmap(dpi=300)
        img_bytes = pix.tobytes("png")
        image = Image.open(io.BytesIO(img_bytes))
        text = pytesseract.image_to_string(image, lang="por")
        return text
    except Exception as e:
        print(f"[OCR ERRO] {e}")
        return ""


def extract_text(page) -> str:

    text = page.get_text("text")

    if text and len(text.strip()) >= MIN_TEXT_THRESHOLD:
        return text

    ocr_text = ocr_page(page)

    if ocr_text and len(ocr_text.strip()) > len(text.strip()):
        return ocr_text

    return text or ""


def open_pdf_document(pdf_path: Path):
    try:
        return fitz.open(pdf_path)
    except Exception as first_error:
        try:
            with fitz.open(stream=pdf_path.read_bytes(), filetype="pdf") as doc:
                repaired = doc.tobytes(garbage=4, deflate=True, clean=True)
            return fitz.open(stream=repaired, filetype="pdf")
        except Exception:
            raise first_error


# ==========================================================
# CHUNKING
# ==========================================================

def chunk_text(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= CHUNK_SIZE:
        return [text]

    blocks = _split_legislative_blocks(text)
    return _pack_blocks(blocks)


# ==========================================================
# PROCESSAMENTO DO DOCUMENTO
# ==========================================================

def process_document(filename: str, base_name: str) -> List[Dict]:

    source_dir = get_documents_dir(base_name)
    pdf_path = source_dir / filename

    if not pdf_path.exists():
        print(f"[ERRO] Arquivo nao encontrado: {filename}")
        return []

    print(f"[PROCESSANDO] {filename}")

    doc = open_pdf_document(pdf_path)
    all_chunks = []

    for page_number, page in enumerate(doc):

        try:
            text = clean_extracted_text(extract_text(page))

            if not text.strip():
                continue

            chunks = chunk_text(text)

            for idx, chunk in enumerate(chunks):

                chunk_id = f"{filename}::p{page_number+1}::c{idx}"

                chunk_data = {
                    "chunk_id": chunk_id,
                    "doc_name": filename,
                    "doc": filename,
                    "page": page_number + 1,
                    "chunk_index": idx,
                    "text": chunk
                }

                all_chunks.append(chunk_data)

        except Exception as e:
            print(f"[ERRO PAGINA {page_number+1}] {e}")
            continue

    print(f"[OK] {filename} -> {len(all_chunks)} chunks")

    return all_chunks


def _file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_chunk_checkpoint(base_name: str) -> Dict:
    checkpoint_path = get_chunk_checkpoint_path(base_name)
    if not checkpoint_path.exists():
        return {}
    try:
        return json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_chunk_checkpoint(state: Dict, base_name: str):
    checkpoint_path = get_chunk_checkpoint_path(base_name)
    checkpoint_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def clear_chunk_checkpoint_for_docs(doc_names: List[str], base_name: str):
    if not doc_names:
        return

    checkpoint = load_chunk_checkpoint(base_name)
    changed = False

    for doc in doc_names:
        if doc in checkpoint:
            checkpoint.pop(doc, None)
            changed = True

    if changed:
        save_chunk_checkpoint(checkpoint, base_name)


def process_document_with_checkpoint(
    filename: str,
    base_name: str,
    checkpoint_every_pages: int = 100,
    progress_every_pages: int = 10,
    progress_callback: Optional[Callable[[], None]] = None,
) -> int:
    source_dir = get_documents_dir(base_name)
    pdf_path = source_dir / filename

    if not pdf_path.exists():
        print(f"[ERRO] Arquivo nao encontrado: {filename}")
        return 0

    checkpoint_every_pages = max(1, int(checkpoint_every_pages))
    progress_every_pages = int(progress_every_pages)
    show_page_progress = progress_every_pages > 0
    current_hash = _file_hash(pdf_path)
    checkpoint = load_chunk_checkpoint(base_name)
    entry = checkpoint.get(filename) or {}

    resume_page = 1
    should_reset_chunks = True

    if entry.get("hash") == current_hash:
        next_page = int(entry.get("next_page", 1))
        if next_page > 1:
            resume_page = next_page
            should_reset_chunks = False
            print(f"[RESUME] {filename} retomado na pagina {resume_page}.")

    if should_reset_chunks:
        remove_chunks([filename], base_name)
        checkpoint[filename] = {
            "hash": current_hash,
            "next_page": 1,
        }
        save_chunk_checkpoint(checkpoint, base_name)

    print(f"[PROCESSANDO] {filename}")
    total_chunks = 0
    buffered_chunks = []
    pages_since_checkpoint = 0

    with open_pdf_document(pdf_path) as doc:
        total_pages = len(doc)
        start_done = resume_page - 1

        if resume_page > total_pages:
            checkpoint.pop(filename, None)
            save_chunk_checkpoint(checkpoint, base_name)
            print(f"[OK] {filename} ja estava concluido no checkpoint.")
            return 0

        if show_page_progress:
            start_pct = (start_done / total_pages * 100) if total_pages else 100.0
            print(
                f"[PROGRESS CHUNKS] {filename}: {start_done}/{total_pages} paginas "
                f"({start_pct:.1f}%)"
            )

        pages_since_progress = 0

        for page_idx in range(resume_page - 1, total_pages):
            page = doc[page_idx]

            try:
                text = clean_extracted_text(extract_text(page))
                if text.strip():
                    chunks = chunk_text(text)

                    for idx, chunk in enumerate(chunks):
                        chunk_id = f"{filename}::p{page_idx+1}::c{idx}"
                        buffered_chunks.append(
                            {
                                "chunk_id": chunk_id,
                                "doc_name": filename,
                                "doc": filename,
                                "page": page_idx + 1,
                                "chunk_index": idx,
                                "text": chunk,
                            }
                        )
                        total_chunks += 1

            except Exception as e:
                print(f"[ERRO PAGINA {page_idx+1}] {e}")

            pages_since_checkpoint += 1
            if show_page_progress:
                pages_since_progress += 1

            if progress_callback is not None:
                progress_callback()

            pages_done = page_idx + 1
            should_print_progress = (
                show_page_progress
                and (
                    pages_since_progress >= progress_every_pages
                    or pages_done == total_pages
                )
            )

            if should_print_progress:
                pct = (pages_done / total_pages * 100) if total_pages else 100.0
                print(
                    f"[PROGRESS CHUNKS] {filename}: {pages_done}/{total_pages} paginas "
                    f"({pct:.1f}%)"
                )
                pages_since_progress = 0

            if pages_since_checkpoint >= checkpoint_every_pages:
                if buffered_chunks:
                    append_chunks(buffered_chunks, base_name)
                    buffered_chunks = []

                checkpoint[filename] = {
                    "hash": current_hash,
                    "next_page": page_idx + 2,
                }
                save_chunk_checkpoint(checkpoint, base_name)
                pct = ((page_idx + 1) / total_pages * 100) if total_pages else 100.0
                print(
                    f"[CHECKPOINT CHUNKS] {filename}: pagina {page_idx+1}/{total_pages} "
                    f"({pct:.1f}%)."
                )
                pages_since_checkpoint = 0

    if buffered_chunks:
        append_chunks(buffered_chunks, base_name)

    checkpoint.pop(filename, None)
    save_chunk_checkpoint(checkpoint, base_name)
    print(f"[OK] {filename} -> {total_chunks} chunks")
    return total_chunks


def get_pending_pages_for_document(filename: str, base_name: str) -> int:
    source_dir = get_documents_dir(base_name)
    pdf_path = source_dir / filename

    if not pdf_path.exists():
        return 0

    current_hash = _file_hash(pdf_path)
    checkpoint = load_chunk_checkpoint(base_name)
    entry = checkpoint.get(filename) or {}

    resume_page = 1
    if entry.get("hash") == current_hash:
        next_page = int(entry.get("next_page", 1))
        if next_page > 1:
            resume_page = next_page

    with open_pdf_document(pdf_path) as doc:
        total_pages = len(doc)

    if resume_page > total_pages:
        return 0

    return total_pages - resume_page + 1


# ==========================================================
# APPEND CHUNKS
# ==========================================================

def append_chunks(new_chunks: List[Dict], base_name: str):

    CHUNKS_PATH = get_chunks_path(base_name)

    existing = load_valid_jsonl(CHUNKS_PATH)
    new_ids = {chunk["chunk_id"] for chunk in new_chunks}

    # Idempotencia para retomada: substitui chunks com mesmo chunk_id.
    existing = [
        chunk for chunk in existing
        if chunk.get("chunk_id") not in new_ids
    ]
    existing.extend(new_chunks)

    atomic_write_jsonl(CHUNKS_PATH, existing)

    print(f"[CHUNKS] Total atual: {len(existing)}")


# ==========================================================
# REMOVER CHUNKS
# ==========================================================

def remove_chunks(removed_files: List[str], base_name: str):

    CHUNKS_PATH = get_chunks_path(base_name)

    if not CHUNKS_PATH.exists():
        return

    existing = load_valid_jsonl(CHUNKS_PATH)

    filtered = [
        chunk for chunk in existing
        if chunk["doc_name"] not in removed_files
    ]

    atomic_write_jsonl(CHUNKS_PATH, filtered)

    print(f"[REMOVIDO] Chunks atualizados apos remocao.")
