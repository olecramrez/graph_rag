import json
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from src.config import get_user_root
from src.lia_client import LIAClientError, chat_completion


MEMORY_DIRNAME = "memory"
CONVERSATIONS_FILENAME = "conversations.jsonl"
SUMMARIES_FILENAME = "memory_summaries.jsonl"
MAX_STORED_TEXT_CHARS = 8000
SENSITIVE_PATTERNS = [
    re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"),
]


def _memory_dir():
    path = get_user_root() / MEMORY_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def memory_paths():
    root = _memory_dir()
    return {
        "dir": root,
        "conversations": root / CONVERSATIONS_FILENAME,
        "summaries": root / SUMMARIES_FILENAME,
    }


def _normalize(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", normalized.lower()).strip()


def _tokens(text):
    return [
        token
        for token in re.findall(r"[a-zA-Z0-9_]{3,}", _normalize(text))
        if token not in {"para", "como", "que", "com", "uma", "das", "dos", "por", "sobre"}
    ]


def _truncate(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _redact_sensitive(text):
    redacted = str(text or "")
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub("[identificador omitido]", redacted)
    return redacted


def _append_jsonl(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _read_jsonl(path, limit=None):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit:
        return rows[-int(limit):]
    return rows


def clear_memory():
    paths = memory_paths()
    removed = []
    for key in ("conversations", "summaries"):
        path = paths[key]
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def append_conversation_turn(question, answer, base=None, mode=None, metadata=None):
    paths = memory_paths()
    payload = {
        "type": "turn",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "question": _truncate(_redact_sensitive(question), MAX_STORED_TEXT_CHARS),
        "answer": _truncate(_redact_sensitive(answer), MAX_STORED_TEXT_CHARS),
        "base": base or "",
        "mode": mode or "",
        "metadata": metadata or {},
    }
    _append_jsonl(paths["conversations"], payload)
    return payload


def _fallback_summary(question, answer, base=None, mode=None):
    question = _truncate(_redact_sensitive(question), 500)
    answer = _truncate(_redact_sensitive(answer), 900)
    label = f"Base: {base or 'nao informada'}; modo: {mode or 'nao informado'}."
    return f"{label}\nPergunta: {question}\nResumo da resposta: {answer}"


def build_turn_summary(question, answer, base=None, mode=None, llm_model=None):
    prompt = f"""
Resuma uma interacao para memoria persistente de um sistema RAG.
Guarde somente fatos uteis para conversas futuras: preferencias, decisoes, bases consultadas,
entidades relevantes nao sensiveis, pendencias e conclusoes operacionais.
Nao inclua CPF, CNPJ, chaves, tokens ou dados pessoais sensiveis.
Escreva em portugues, em ate 6 bullets curtos.

Base: {base or "nao informada"}
Modo: {mode or "nao informado"}
Pergunta:
{_truncate(question, 3000)}

Resposta:
{_truncate(answer, 5000)}
""".strip()
    try:
        summary = chat_completion(
            [
                {
                    "role": "system",
                    "content": "Voce cria memoria persistente curta, fiel e sem dados sensiveis.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_retries=1,
            llm_model=llm_model,
        )
        return _truncate(_redact_sensitive(summary), 2200)
    except LIAClientError:
        return _fallback_summary(question, answer, base=base, mode=mode)


def append_memory_summary(question, answer, base=None, mode=None, llm_model=None):
    summary = build_turn_summary(
        question,
        answer,
        base=base,
        mode=mode,
        llm_model=llm_model,
    )
    payload = {
        "type": "summary",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "base": base or "",
        "mode": mode or "",
    }
    _append_jsonl(memory_paths()["summaries"], payload)
    return payload


def _score_memory(query_tokens, memory):
    summary = memory.get("summary") or ""
    memory_tokens = _tokens(summary)
    if not query_tokens or not memory_tokens:
        return 0.0
    memory_set = set(memory_tokens)
    overlap = sum(1 for token in query_tokens if token in memory_set)
    if overlap <= 0:
        return 0.0
    age_bonus = 1.0
    length_penalty = math.log(max(len(memory_tokens), 3), 10)
    return (overlap * age_bonus) / max(length_penalty, 1.0)


def retrieve_relevant_memories(query, limit=5, base=None):
    memories = _read_jsonl(memory_paths()["summaries"])
    query_tokens = _tokens(query)
    scored = []
    for idx, memory in enumerate(memories):
        score = _score_memory(query_tokens, memory)
        if base and memory.get("base") == base:
            score += 0.25
        if score > 0:
            scored.append((score, idx, memory))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [memory for _, _, memory in scored[: int(limit)]]


def build_short_memory(chat_history, max_turns=4, max_chars=3500):
    items = list(chat_history or [])[-int(max_turns):]
    if not items:
        return ""
    parts = []
    for item in items:
        question = _truncate(item.get("question", ""), 500)
        answer = _truncate(item.get("answer", ""), 900)
        if question or answer:
            parts.append(f"Usuario: {question}\nAssistente: {answer}")
    return _truncate("\n\n".join(parts), max_chars)


def build_memory_context(
    query,
    chat_history=None,
    short_turns=4,
    retrieval_limit=5,
    base=None,
    include_short=True,
    include_persistent=True,
):
    blocks = []
    if include_short:
        short_memory = build_short_memory(chat_history, max_turns=short_turns)
        if short_memory:
            blocks.append("Memoria curta da conversa atual:\n" + short_memory)

    relevant = []
    if include_persistent:
        relevant = retrieve_relevant_memories(query, limit=retrieval_limit, base=base)
        if relevant:
            lines = []
            for memory in relevant:
                created_at = memory.get("created_at", "")
                summary = _truncate(memory.get("summary", ""), 1000)
                label = f"[{created_at}]"
                if memory.get("base"):
                    label += f" base={memory.get('base')}"
                lines.append(f"{label}\n{summary}")
            blocks.append("Memoria persistente recuperada por similaridade:\n" + "\n\n".join(lines))

    if not blocks:
        return "", {"short_turns": 0, "retrieved": 0}

    context = (
        "Use a memoria abaixo apenas como contexto conversacional e preferencias do usuario. "
        "Ela nao substitui as evidencias documentais do RAG e nao deve ser citada como fonte.\n\n"
        + "\n\n---\n\n".join(blocks)
    )
    return context, {
        "short_turns": min(len(chat_history or []), int(short_turns)),
        "retrieved": len(relevant),
    }


def memory_stats():
    paths = memory_paths()
    conversations = _read_jsonl(paths["conversations"])
    summaries = _read_jsonl(paths["summaries"])
    return {
        "dir": str(paths["dir"]),
        "turns": len(conversations),
        "summaries": len(summaries),
    }
