import argparse
import csv
import hashlib
import html
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

try:
    import fitz
except ImportError:  # pragma: no cover - fallback quando PyMuPDF nao estiver instalado
    fitz = None


BASE_URL = "https://anmlegis.datalegis.net"
CATEGORIES_URL = (
    BASE_URL
    + "/action/ActionDatalegis.php?acao=categorias&cod_menu=8997&cod_modulo=351&menuOpen=true"
)
PDF_ACTION = BASE_URL + "/action/ActionDatalegis.php?acao=gerarPdfAto"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
DEFAULT_MAX_FILENAME_LEN = 55

MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "marÃ§o": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


@dataclass(frozen=True)
class ActRef:
    tipo: str
    numero: str
    sequencia: str
    ano: str
    orgao: str
    cod_modulo: str
    cod_menu: str
    list_url: str = ""
    list_title: str = ""

    @property
    def key(self) -> str:
        orgao_key = re.sub(r"[^A-Za-z0-9]+", "_", self.orgao).strip("_")
        return f"{self.tipo}_{self.numero}_{self.sequencia}_{self.ano}_{orgao_key}"

    def public_url(self) -> str:
        params = {
            "acao": "abrirAtoPublico",
            "sgl_tipo": self.tipo,
            "num_ato": self.numero,
            "seq_ato": self.sequencia,
            "vlr_ano": self.ano,
            "sgl_orgao": self.orgao,
            "cod_modulo": self.cod_modulo,
            "cod_menu": self.cod_menu,
        }
        return BASE_URL + "/action/UrlPublicasAction.php?" + urlencode(params)


def decode_response(response: requests.Response) -> str:
    encoding = response.encoding or "iso-8859-1"
    try:
        return response.content.decode(encoding, errors="replace")
    except LookupError:
        return response.content.decode("iso-8859-1", errors="replace")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</p\s*>", "\n", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(normalize_space(text))


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_for_match(value: str) -> str:
    return strip_accents(normalize_space(value)).lower()


def safe_filename(value: str, max_len: int = DEFAULT_MAX_FILENAME_LEN) -> str:
    value = html.unescape(normalize_space(value))
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r'[<>:"|?*\x00-\x1f]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if len(value) > max_len:
        value = value[:max_len].rstrip(" .")
    return value or "normativo_anm"


def clean_title(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\s+Veja\s+Tamb[eÃ©]m\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+\d{1,2}/\d{1,2}/(?:19|20)\d{2}\s*\|\s*\d{1,2}:\d{2}:\d{2}\s*$", "", value)
    return normalize_space(value)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def ref_suffix(ref: ActRef) -> str:
    return hashlib.sha1(ref.key.encode("utf-8")).hexdigest()[:8]


def act_filename_stem(title: str, ref: ActRef) -> str:
    return f"{safe_filename(title, max_len=46)}_{ref_suffix(ref)}"


def parse_pt_date(value: str) -> Optional[str]:
    text = strip_accents(normalize_space(value)).lower()
    match = re.search(
        r"(\d{1,2})(?:o|Âº|Â°)?\s+de\s+([a-z]+)\s+de\s+((?:19|20)\d{2})",
        text,
        flags=re.I,
    )
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS_PT.get(match.group(2))
    year = int(match.group(3))
    if not month:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_br_date(value: str) -> Optional[str]:
    match = re.search(r"\b([0-3]?\d)/([01]?\d)/((?:19|20)?\d{2})\b", value or "")
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3))
    if year < 100:
        year = 2000 + year if year <= 49 else 1900 + year
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_any_date(value: str) -> Optional[str]:
    return parse_br_date(value) or parse_pt_date(value)


def extract_dates_in_order(value: str) -> List[str]:
    text = normalize_space(value or "")
    if not text:
        return []

    matches: List[Tuple[int, str]] = []
    for match in re.finditer(r"\b([0-3]?\d)/([01]?\d)/((?:19|20)?\d{2})\b", text):
        parsed = parse_br_date(match.group(0))
        if parsed:
            matches.append((match.start(), parsed))

    date_words = r"[A-Za-zÃƒÂ§Ãƒâ€¡ÃƒÂ£ÃƒÆ’ÃƒÂ¡ÃƒÂÃƒÂ©Ãƒâ€°ÃƒÂªÃƒÅ ÃƒÂ­ÃƒÂÃƒÂ³Ãƒâ€œÃƒÂ´Ãƒâ€ÃƒÂºÃƒÅ¡]+"
    for match in re.finditer(
        rf"\b\d{{1,2}}(?:o|Ã‚Âº|Ã‚Â°)?\s+de\s+{date_words}\s+de\s+(?:19|20)\d{{2}}\b",
        text,
        flags=re.I,
    ):
        parsed = parse_pt_date(match.group(0))
        if parsed:
            matches.append((match.start(), parsed))

    ordered: List[str] = []
    seen = set()
    for _, parsed in sorted(matches, key=lambda item: item[0]):
        if parsed not in seen:
            seen.add(parsed)
            ordered.append(parsed)
    return ordered


def iso_add_days(value: str, days: int) -> Optional[str]:
    try:
        base = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (base + timedelta(days=days)).isoformat()


def iso_add_years(value: str, years: int) -> Optional[str]:
    try:
        base = date.fromisoformat(value)
        return base.replace(year=base.year + years).isoformat()
    except ValueError:
        if not value:
            return None
        try:
            base = date.fromisoformat(value)
            return base.replace(year=base.year + years, day=28).isoformat()
        except (TypeError, ValueError):
            return None
    except TypeError:
        return None


def days_between(start: str, end: str) -> Optional[int]:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def extract_pdf_text(path: Path, max_pages: Optional[int] = None) -> str:
    if fitz is None or not path.exists():
        return ""
    parts: List[str] = []
    try:
        with fitz.open(path) as doc:
            page_limit = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
            for index in range(page_limit):
                page = doc[index]
                parts.append(page.get_text("text") or "")
    except Exception:
        return ""
    return "\n".join(parts)


def extract_publication_date_from_text(text: str) -> str:
    if not text:
        return ""
    normalized = normalize_space(text)
    patterns = (
        r"(?:publicad[oa]\s+internamente).{0,120}?\bem\s+"
        r"(\d{1,2}/\d{1,2}/(?:19|20)\d{2})",
        r"(?:D\.?\s*O\.?\s*U\.?|DOU)\s*,?\s*"
        r"(\d{1,2}\s+de\s+[A-Za-zÃƒÂ§Ãƒâ€¡ÃƒÂ£ÃƒÆ’ÃƒÂ©Ãƒâ€°]+\s+de\s+(?:19|20)\d{2})",
        r"(?:D\.?\s*O\.?\s*U\.?|DOU)\s*,?\s*"
        r"(\d{1,2}/\d{1,2}/(?:19|20)\d{2})",
        r"(?:publicad[oa]\s+no\s+DOU|Di[aÃ¡]rio\s+Oficial\s+da\s+Uni[aÃ£]o|DOU)"
        r".{0,180}?(\d{1,2}/\d{1,2}/(?:19|20)\d{2})",
        r"(\d{1,2}/\d{1,2}/(?:19|20)\d{2}).{0,80}?"
        r"(?:publicad[oa]\s+no\s+DOU|Di[aÃ¡]rio\s+Oficial\s+da\s+Uni[aÃ£]o|DOU)",
        r"(?:publicad[oa]|publica[cÃ§][aÃ£]o).{0,120}?"
        r"(\d{1,2}\s+de\s+[A-Za-zÃ§Ã‡Ã£ÃƒÃ©Ã‰]+\s+de\s+(?:19|20)\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        parsed = parse_any_date(match.group(1))
        if parsed:
            return parsed
    return ""


def parse_query(url: str) -> Dict[str, str]:
    parsed = urlparse(html.unescape(url))
    query = parse_qs(parsed.query, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in query.items()}


def parse_js_args(args_text: str) -> List[str]:
    values = []
    for match in re.finditer(r"'((?:\\'|[^'])*)'", args_text):
        values.append(match.group(1).replace("\\'", "'"))
    return values


def linktexto_to_url(args_text: str, cod_modulo: str, cod_menu: str) -> str:
    values = parse_js_args(args_text)
    if len(values) < 5:
        return ""
    params = {
        "acao": "abrirAtoPublico",
        "sgl_tipo": values[0],
        "num_ato": values[1],
        "seq_ato": values[2],
        "vlr_ano": values[3],
        "sgl_orgao": values[4],
        "cod_modulo": cod_modulo,
        "cod_menu": cod_menu,
    }
    return BASE_URL + "/action/UrlPublicasAction.php?" + urlencode(params)


def extract_menu_urls(categories_html: str) -> List[Tuple[str, str]]:
    menus: Dict[str, str] = {}

    for href, label in re.findall(
        r'<a[^>]+href=["\']([^"\']*abrirResenhaAnoData[^"\']*)["\'][^>]*>(.*?)</a>',
        categories_html,
        flags=re.I | re.S,
    ):
        absolute = urljoin(BASE_URL, html.unescape(href))
        menus[absolute] = strip_tags(label)

    for data_id, label in re.findall(
        r'<li[^>]+data-id=["\']?(\d+)["\']?[^>]*>\s*<a[^>]*>(.*?)</a>',
        categories_html,
        flags=re.I | re.S,
    ):
        clean_label = strip_tags(label)
        if not clean_label:
            continue
        url = (
            BASE_URL
            + "/action/ActionDatalegis.php?"
            + urlencode(
                {
                    "acao": "abrirResenhaAnoData",
                    "cod_modulo": "566",
                    "cod_menu": data_id,
                }
            )
        )
        menus[url] = clean_label

    return sorted(menus.items(), key=lambda item: (item[1].lower(), item[0]))


def extract_year_urls(menu_html: str, menu_url: str) -> List[str]:
    urls: List[str] = []
    seen = set()
    menu_cod_modulo = parse_query(menu_url).get("cod_modulo")
    for href in re.findall(
        r'href=["\']([^"\']*abrirResenhaAnoData[^"\']*ano=\d{4}[^"\']*)["\']',
        menu_html,
        flags=re.I,
    ):
        absolute = urljoin(BASE_URL, html.unescape(href))
        if menu_cod_modulo:
            parsed = urlparse(absolute)
            query = parse_qs(parsed.query, keep_blank_values=True)
            query["cod_modulo"] = [menu_cod_modulo]
            absolute = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls or [menu_url]


def extract_pagination_url(list_html: str, list_url: str) -> str:
    match = re.search(
        r"var\s+link\s*=\s*[\"']([^\"']*paginaResenhaAno[^\"']*)[\"']",
        list_html,
        flags=re.I,
    )
    if not match:
        return ""
    return urljoin(list_url, html.unescape(match.group(1)))


def act_ref_from_url(url: str, list_url: str = "", list_title: str = "") -> Optional[ActRef]:
    params = parse_query(url)
    tipo = params.get("tipo") or params.get("sgl_tipo")
    numero = params.get("numeroAto") or params.get("num_ato")
    sequencia = params.get("seqAto") or params.get("seq_ato") or "000"
    ano = params.get("valorAno") or params.get("vlr_ano")
    orgao = params.get("orgao") or params.get("sgl_orgao")
    cod_modulo = params.get("cod_modulo") or "566"
    cod_menu = params.get("cod_menu") or ""
    if not all([tipo, numero, sequencia, ano, orgao, cod_menu]):
        return None
    return ActRef(
        tipo=unquote(tipo),
        numero=unquote(numero),
        sequencia=unquote(sequencia),
        ano=unquote(ano),
        orgao=unquote(orgao),
        cod_modulo=unquote(cod_modulo),
        cod_menu=unquote(cod_menu),
        list_url=list_url,
        list_title=list_title,
    )


def extract_act_refs(list_html: str, list_url: str, list_title: str = "") -> List[ActRef]:
    refs: Dict[str, ActRef] = {}
    for href, label in re.findall(
        r'<a[^>]+href=["\']([^"\']*abrirTextoAto[^"\']*)["\'][^>]*>(.*?)</a>',
        list_html,
        flags=re.I | re.S,
    ):
        ref = act_ref_from_url(href, list_url=list_url, list_title=strip_tags(label) or list_title)
        if ref:
            refs[ref.key] = ref
    return list(refs.values())


def extract_public_act_refs(list_html: str, list_url: str, list_title: str = "") -> List[ActRef]:
    refs: Dict[str, ActRef] = {}
    patterns = (
        r"copyTextToClipboard\(\s*['\"]([^'\"]*UrlPublicasAction\.php\?acao=abrirAtoPublico[^'\"]+)['\"]",
        r"copyTextToClipboard\(\s*['\"]([^'\"]*/action/UrlPublicasAction\.php\?acao=abrirAtoPublico[^'\"]+)['\"]",
    )
    for pattern in patterns:
        for raw_url in re.findall(pattern, list_html, flags=re.I):
            public_url = urljoin(list_url, html.unescape(raw_url))
            ref = act_ref_from_url(public_url, list_url=list_url, list_title=list_title)
            if ref:
                refs[ref.key] = ref
    return list(refs.values())


def extract_ato_html(page_html: str) -> Optional[str]:
    match = re.search(
        r'<div\s+class=["\']ato["\'][^>]*>([\s\S]*?)</div>\s*</article>',
        page_html,
        flags=re.I,
    )
    if match:
        return match.group(1)
    match = re.search(
        r'<div\s+class=["\']ato["\'][^>]*>([\s\S]*?)</div>',
        page_html,
        flags=re.I,
    )
    if match:
        return match.group(1)
    match = re.search(
        r'<article[^>]+class=["\'][^"\']*\barticle\b[^"\']*["\'][^>]*>([\s\S]*?)</article>',
        page_html,
        flags=re.I,
    )
    if match:
        return match.group(1)
    match = re.search(
        r'<div[^>]+id=["\']detalharAto["\'][^>]*>([\s\S]*?)</div>',
        page_html,
        flags=re.I,
    )
    return match.group(1) if match else None


def detect_dispositivo(text: str, state: Dict[str, str]) -> Tuple[Dict[str, str], Optional[Dict[str, str]]]:
    normalized = normalize_space(text)
    comparable = normalize_for_match(normalized)
    node = None

    article = re.match(r"^(art\.?\s*\d+[a-zÂºÂ°-]*\.?)\b", normalized, flags=re.I)
    if article:
        state = {"artigo": article.group(1).rstrip(".")}
        node = {"tipo": "artigo", "rotulo": state["artigo"]}
        return state, node

    paragraph = re.match(r"^(paragrafo\s+unico|parÃ¡grafo\s+Ãºnico|Â§\s*\d+[ÂºÂ°]?)\b", normalized, flags=re.I)
    if paragraph:
        state = dict(state)
        state["paragrafo"] = paragraph.group(1).rstrip(".")
        state.pop("inciso", None)
        state.pop("alinea", None)
        state.pop("item", None)
        node = {"tipo": "paragrafo", "rotulo": state["paragrafo"]}
        return state, node

    inciso = re.match(r"^([IVXLCDM]+)\s*[-â€“â€”]\s+", normalized)
    if inciso:
        state = dict(state)
        state["inciso"] = inciso.group(1)
        state.pop("alinea", None)
        state.pop("item", None)
        node = {"tipo": "inciso", "rotulo": state["inciso"]}
        return state, node

    alinea = re.match(r'^([a-z])\)\s+', comparable)
    if alinea:
        state = dict(state)
        state["alinea"] = alinea.group(1)
        state.pop("item", None)
        node = {"tipo": "alinea", "rotulo": state["alinea"]}
        return state, node

    item = re.match(r"^(\d+)\.\s+", normalized)
    if item:
        state = dict(state)
        state["item"] = item.group(1)
        node = {"tipo": "item", "rotulo": state["item"]}
        return state, node

    return state, node


def build_caminho_dispositivo(state: Dict[str, str], node: Optional[Dict[str, str]]) -> str:
    current = dict(state)
    if node:
        key = node["tipo"]
        current[key] = node["rotulo"]

    parts = []
    labels = [
        ("artigo", "Artigo"),
        ("paragrafo", "ParÃ¡grafo"),
        ("inciso", "Inciso"),
        ("alinea", "AlÃ­nea"),
        ("item", "Item"),
    ]
    for key, label in labels:
        value = current.get(key)
        if value:
            parts.append(f"{label} {value}")
    return " > ".join(parts)


def detect_event_type(text: str) -> Optional[str]:
    comparable = normalize_for_match(text)
    revocation_patterns = (
        "revogado pela",
        "revogada pela",
        "revogados pela",
        "revogadas pela",
        "fica revogado",
        "fica revogada",
        "ficam revogados",
        "ficam revogadas",
        "revoga-se",
        "revogam-se",
    )
    if any(pattern in comparable for pattern in revocation_patterns):
        return "revogacao"
    if "alterad" in comparable or "redacao dada" in comparable:
        return "alteracao"
    inclusion_patterns = (
        "incluido pela",
        "incluida pela",
        "acrescido pela",
        "acrescida pela",
        "fica incluido",
        "fica incluida",
        "ficam incluidos",
        "ficam incluidas",
        "fica acrescido",
        "fica acrescida",
        "ficam acrescidos",
        "ficam acrescidas",
    )
    if any(pattern in comparable for pattern in inclusion_patterns):
        return "inclusao"
    return None


def detect_event_direction(text: str) -> str:
    comparable = normalize_for_match(text)
    passive_patterns = (
        "revogado pela",
        "revogada pela",
        "alterado pela",
        "alterada pela",
        "redacao dada pela",
        "incluido pela",
        "incluida pela",
        "acrescido pela",
        "acrescida pela",
    )
    if any(pattern in comparable for pattern in passive_patterns):
        return "passiva"
    active_patterns = (
        "ficam revogados",
        "fica revogado",
        "fica revogada",
        "revoga-se",
        "revogam-se",
        "revoga ",
        "altera ",
    )
    if any(pattern in comparable for pattern in active_patterns):
        return "ativa"
    return "indeterminada"


def extract_tracking_events(ref: ActRef, ato_html: str) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []
    state: Dict[str, str] = {}

    blocks = re.findall(r"<p[^>]*>([\s\S]*?)</p>", ato_html, flags=re.I)
    for index, block in enumerate(blocks, start=1):
        text = strip_tags(block)
        if not text:
            continue

        state, node = detect_dispositivo(text, state)
        event_type = detect_event_type(text)
        if not event_type:
            continue
        direction = detect_event_direction(text)

        caminho = build_caminho_dispositivo(state, node)
        refs = []
        for link_args, link_text in re.findall(
            r'<a[^>]+href=["\']javascript:LinkTexto\((.*?)\)["\'][^>]*>([\s\S]*?)</a>',
            block,
            flags=re.I,
        ):
            refs.append(
                {
                    "texto": strip_tags(link_text),
                    "url": linktexto_to_url(link_args, ref.cod_modulo, ref.cod_menu),
                    "args": parse_js_args(link_args),
                }
            )

        events.append(
            {
                "doc_key": ref.key,
                "source_url": ref.public_url(),
                "evento": event_type,
                "direcao": direction,
                "dispositivo_tipo": node["tipo"] if node else "",
                "dispositivo_rotulo": node["rotulo"] if node else "",
                "caminho_dispositivo": caminho,
                "artigo": state.get("artigo", ""),
                "paragrafo": state.get("paragrafo", ""),
                "inciso": state.get("inciso", ""),
                "alinea": state.get("alinea", ""),
                "item": state.get("item", ""),
                "referencias_json": json.dumps(refs, ensure_ascii=False),
                "trecho": text[:1000],
                "ordem_bloco": str(index),
            }
        )

    return events


def extract_publication_date(ref: ActRef, title: str, paragraphs: List[str]) -> str:
    # Rodapes oficiais do Datalegis sao a fonte mais confiavel: evitam
    # confundir datas de atos citados com a publicacao do ato corrente.
    for paragraph in reversed(paragraphs):
        comparable = normalize_for_match(paragraph)
        if (
            "publicado internamente" in comparable
            or re.search(r"\b(?:d\.?\s*o\.?\s*u\.?|dou)\b", comparable, flags=re.I)
            or "diario oficial da uniao" in comparable
        ):
            parsed = extract_publication_date_from_text(paragraph)
            if parsed:
                return parsed

    candidates = [ref.list_title, title]
    candidates.extend(paragraphs[:5])
    for candidate in candidates:
        parsed = parse_br_date(candidate or "")
        if parsed:
            return parsed
    return ""


def extract_vigencia_clause(paragraphs: List[str]) -> str:
    trigger = re.compile(
        r"\b(?:entra(?:ra)?\s+em\s+vigor|passa(?:ra)?\s+a\s+vigorar|"
        r"produz(?:ira)?\s+efeitos?|passa\s+a\s+produzir\s+efeitos?)\b",
        flags=re.I,
    )
    for paragraph in reversed(paragraphs):
        if trigger.search(normalize_for_match(paragraph)):
            return paragraph
    return ""


def extract_relative_vacatio_days(clause: str) -> Optional[int]:
    comparable = normalize_for_match(clause)
    match = re.search(r"\b(\d{1,4})\s*(?:\([^)]*\)\s*)?dias?\b", comparable)
    if match:
        return int(match.group(1))

    number_words = {
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "trinta": 30,
        "quarenta e cinco": 45,
        "sessenta": 60,
        "noventa": 90,
        "cento e oitenta": 180,
    }
    for word, days in sorted(number_words.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(word)}\s+dias?\b", comparable):
            return days
    return None


def extract_entry_into_force_date(clause: str) -> Optional[str]:
    comparable = normalize_for_match(clause)
    trigger = re.search(r"\b(?:entra(?:ra)?\s+em\s+vigor|passa(?:ra)?\s+a\s+vigorar)\b", comparable)
    if not trigger:
        return None

    tail = comparable[trigger.end() : trigger.end() + 260]
    sentence = re.split(r"[.;\n]", tail, maxsplit=1)[0]
    sentence = re.split(
        r"\b(?:e\s+tera\s+vigencia\s+final|tera\s+vigencia\s+final|tera\s+validade)\b",
        sentence,
        maxsplit=1,
    )[0]
    dates = extract_dates_in_order(sentence)
    return dates[0] if dates else None


def extract_final_vigencia_date(clause: str) -> Optional[str]:
    comparable = normalize_for_match(clause)
    marker = re.search(r"\bvigencia\s+final\s+em\b", comparable)
    if not marker:
        return None
    dates = extract_dates_in_order(comparable[marker.end() : marker.end() + 120])
    return dates[0] if dates else None


def extract_validity_deadline_date(clause: str, start_iso: str) -> Optional[str]:
    comparable = normalize_for_match(clause)
    match = re.search(
        r"\btera\s+validade\s+pelo\s+prazo\s+de\s+"
        r"(?P<count>\d{1,4}|um|uma|dois|duas|tres|trinta|sessenta|noventa)"
        r"(?:\s*\([^)]*\))?\s+(?P<unit>dia|dias|mes|meses|ano|anos)\b",
        comparable,
        flags=re.I,
    )
    if not match or not start_iso:
        return None

    raw_count = match.group("count")
    word_counts = {
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "trinta": 30,
        "sessenta": 60,
        "noventa": 90,
    }
    count = int(raw_count) if raw_count.isdigit() else word_counts.get(raw_count)
    if not count or count <= 0:
        return None

    unit = match.group("unit")
    if unit.startswith("dia"):
        return iso_add_days(start_iso, count)
    if unit.startswith("ano"):
        return iso_add_years(start_iso, count)
    if unit.startswith("mes"):
        try:
            base = date.fromisoformat(start_iso)
        except (TypeError, ValueError):
            return None
        month_index = base.month - 1 + count
        year = base.year + month_index // 12
        month = month_index % 12 + 1
        day = min(base.day, 28)
        return date(year, month, day).isoformat()
    return None


def is_non_ementa_annotation(value: str) -> bool:
    comparable = normalize_for_match(value)
    if not comparable:
        return True
    return comparable.startswith(
        (
            "revogada pela",
            "revogado pela",
            "revogadas pela",
            "revogados pela",
            "alterada pela",
            "alterado pela",
            "redacao dada",
            "nota:",
            "vide ",
            "prazo prorrogado",
        )
    )


def looks_like_signature(value: str) -> bool:
    text = normalize_space(value)
    if len(text) > 90:
        return False
    words = text.split()
    if len(words) < 2 or len(words) > 7:
        return False
    comparable = normalize_for_match(text)
    if re.search(
        r"\b(art|portaria|resolucao|circular|instrucao|dispoe|institui|estabelece|altera|aprova|"
        r"disciplina|define|regulamenta|revoga|publicado|dou|d\.o\.u)\b",
        comparable,
    ):
        return False
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    uppercase_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
    return uppercase_ratio >= 0.65


def is_non_ementa_candidate(value: str) -> bool:
    comparable = normalize_for_match(value)
    if is_non_ementa_annotation(value):
        return True
    if looks_like_signature(value):
        return True
    if comparable.startswith(
        (
            "a diretoria",
            "o diretor",
            "a diretora",
            "considerando",
            "resolve",
            "resolvem",
            "regulamento",
            "ministerio ",
            "departamento nacional",
            "agencia nacional",
            "diretoria-geral",
            "diretoria colegiada",
            "superintendencia",
            "coordenacao",
        )
    ):
        return True
    if "este texto nao substitui" in comparable:
        return True
    if re.search(r"\b(d\.?\s*o\.?\s*u\.?|dou)\b", comparable) and len(value) <= 160:
        return True
    if re.search(r"\bpublicad[oa]s?\b", comparable) and len(value) <= 160:
        return True
    return False


def extract_ementa_from_paragraphs(paragraphs: List[str], title: str, title_index: int) -> str:
    candidates = paragraphs[title_index + 1 :] if title_index >= 0 else paragraphs
    substantive_markers = (
        "dispoe",
        "institui",
        "estabelece",
        "altera",
        "aprova",
        "disciplina",
        "define",
        "regula",
        "regulamenta",
        "fixa",
        "atualiza",
        "delega",
        "outorga",
        "cria",
        "prorroga",
        "determina",
        "autoriza",
    )
    for paragraph in candidates:
        if paragraph == title or is_non_ementa_candidate(paragraph):
            continue
        comparable = normalize_for_match(paragraph)
        if re.match(r"^(Art\.|CAP[IÃ]TULO|Se[cÃ§][aÃ£]o)\b", paragraph, re.I):
            continue
        if re.match(r"^(\d+[\.)]|[\(\[]?Of\.)", paragraph, re.I):
            continue
        if 20 <= len(paragraph) <= 700:
            marker_positions = [
                comparable.find(marker)
                for marker in substantive_markers
                if comparable.find(marker) >= 0
            ]
            if marker_positions and min(marker_positions) <= 90:
                return paragraph

    return ""


def infer_vigencia(paragraphs: List[str], data_publicacao: str, data_assinatura: str) -> Dict[str, str]:
    clause = extract_vigencia_clause(paragraphs)
    if not clause:
        return {
            "data_inicio_vigencia": data_publicacao or data_assinatura,
            "vacatio_dias": "",
            "vigencia_regra": "sem_clausula_detectada",
            "vigencia_trecho": "",
        }

    comparable = normalize_for_match(clause)

    same_day_patterns = (
        "na data de sua publicacao",
        "na data da publicacao",
        "no dia de sua publicacao",
        "no dia da publicacao",
        "a partir da data de sua publicacao",
        "a contar da data de sua publicacao",
        "a contar da publicacao",
    )
    if any(pattern in comparable for pattern in same_day_patterns):
        return {
            "data_inicio_vigencia": data_publicacao,
            "vacatio_dias": "0" if data_publicacao else "",
            "vigencia_regra": "data_publicacao",
            "vigencia_trecho": clause,
        }

    explicit_date = extract_entry_into_force_date(clause)
    if explicit_date:
        vacatio = days_between(data_publicacao, explicit_date) if data_publicacao else None
        return {
            "data_inicio_vigencia": explicit_date,
            "vacatio_dias": str(vacatio) if vacatio is not None and vacatio > 0 else "",
            "vigencia_regra": "data_expressa",
            "vigencia_trecho": clause,
        }

    relative_days = extract_relative_vacatio_days(clause)
    if relative_days is not None and data_publicacao:
        return {
            "data_inicio_vigencia": iso_add_days(data_publicacao, relative_days) or "",
            "vacatio_dias": str(relative_days),
            "vigencia_regra": "prazo_dias_publicacao",
            "vigencia_trecho": clause,
        }

    return {
        "data_inicio_vigencia": data_publicacao or data_assinatura,
        "vacatio_dias": "",
        "vigencia_regra": "clausula_nao_classificada",
        "vigencia_trecho": clause,
    }


def summarize_passive_revocation(events: List[Dict[str, str]]) -> Dict[str, str]:
    passive_revocations = [
        event
        for event in events
        if event.get("evento") == "revogacao" and event.get("direcao") == "passiva"
    ]
    if not passive_revocations:
        return {
            "status_normativo": "vigente",
            "tipo_revogacao": "",
            "revogado_por": "",
            "revogado_por_data": "",
            "data_fim_vigencia": "",
            "tem_revogacao_parcial_dispositivo": "false",
        }

    total_revocations = [
        event
        for event in passive_revocations
        if not normalize_space(event.get("caminho_dispositivo") or "")
    ]
    first = total_revocations[0] if total_revocations else passive_revocations[0]
    refs = []
    try:
        refs = json.loads(first.get("referencias_json") or "[]")
    except json.JSONDecodeError:
        refs = []
    revogado_por = refs[0].get("texto") if refs else ""
    revogado_por_data = parse_any_date(revogado_por or first.get("trecho") or "") or ""

    has_total_revocation = bool(total_revocations)
    has_device_path = any(
        normalize_space(event.get("caminho_dispositivo") or "")
        for event in passive_revocations
    )
    return {
        "status_normativo": "revogado" if has_total_revocation else "parcialmente_revogado",
        "tipo_revogacao": "total" if has_total_revocation else "parcial",
        "revogado_por": revogado_por,
        "revogado_por_data": revogado_por_data,
        "data_fim_vigencia": revogado_por_data if has_total_revocation else "",
        "tem_revogacao_parcial_dispositivo": "true" if has_device_path else "false",
    }


def extract_metadata(ref: ActRef, page_html: str, ato_html: str, pdf_path: Path) -> Dict[str, str]:
    plain = strip_tags(ato_html)
    paragraphs = [strip_tags(p) for p in re.findall(r"<p[^>]*>([\s\S]*?)</p>", ato_html, flags=re.I)]
    paragraphs = [p for p in paragraphs if p]

    title = ""
    title_index = -1
    for index, paragraph in enumerate(paragraphs[:12]):
        normalized = strip_accents(paragraph).upper()
        if ref.tipo.upper() in normalized and re.search(r"\bN[ÂºO]\b|\bNO\b|NÂº", normalized):
            title = paragraph
            title_index = index
            break
    if not title:
        title = ref.list_title or f"{ref.tipo} {int(ref.numero)} {ref.orgao} {ref.ano}"
    title = clean_title(title)

    ementa = extract_ementa_from_paragraphs(paragraphs, title, title_index)

    data_assinatura = parse_pt_date(title)
    html_sha = hashlib.sha1(page_html.encode("utf-8", errors="replace")).hexdigest()
    tracking_events = extract_tracking_events(ref, ato_html)
    data_publicacao = extract_publication_date(ref, title, paragraphs)
    vigencia = infer_vigencia(paragraphs, data_publicacao, data_assinatura or "")
    final_vigencia = extract_final_vigencia_date(vigencia["vigencia_trecho"])
    validity_deadline = extract_validity_deadline_date(
        vigencia["vigencia_trecho"],
        vigencia["data_inicio_vigencia"] or data_publicacao or data_assinatura or "",
    )
    revocation = summarize_passive_revocation(tracking_events)
    data_fim_vigencia = revocation["data_fim_vigencia"] or final_vigencia or validity_deadline or ""

    return {
        "doc_id": pdf_path.name,
        "doc_id_curto": pdf_path.stem,
        "classe_documental": "normativo",
        "tipo_documento": ref.tipo,
        "tipo_norma": ref.tipo,
        "numero_norma": str(int(ref.numero)) if ref.numero.isdigit() else ref.numero,
        "ano_norma": ref.ano,
        "seq_ato": ref.sequencia,
        "orgao": ref.orgao,
        "titulo": title,
        "ementa": ementa,
        "data_assinatura": data_assinatura or "",
        "data_publicacao": data_publicacao,
        "data_inicio_vigencia": vigencia["data_inicio_vigencia"],
        "data_fim_vigencia": data_fim_vigencia,
        "status_normativo": revocation["status_normativo"],
        "tipo_revogacao": revocation["tipo_revogacao"],
        "revogado_por": revocation["revogado_por"],
        "revogado_por_data": revocation["revogado_por_data"],
        "vacatio_dias": vigencia["vacatio_dias"],
        "vigencia_regra": vigencia["vigencia_regra"],
        "vigencia_trecho": vigencia["vigencia_trecho"],
        "data_publicacao_fonte": "html" if data_publicacao else "",
        "tem_revogacao_parcial_dispositivo": revocation["tem_revogacao_parcial_dispositivo"],
        "source_url": ref.public_url(),
        "source_list_url": ref.list_url,
        "source_sha1": html_sha,
        "pdf_path": str(pdf_path),
        "texto_preview": plain[:500],
        "rastreamento_dispositivos_json": json.dumps(tracking_events, ensure_ascii=False),
        "quantidade_eventos_rastreamento": str(len(tracking_events)),
    }


def apply_pdf_publication_fallback(metadata: Dict[str, str], pdf_path: Path, paragraphs: List[str]) -> None:
    if metadata.get("data_publicacao"):
        return
    pdf_text = extract_pdf_text(pdf_path)
    data_publicacao = extract_publication_date_from_text(pdf_text)
    if not data_publicacao:
        return

    metadata["data_publicacao"] = data_publicacao
    metadata["data_publicacao_fonte"] = "pdf"

    vigencia = infer_vigencia(
        paragraphs,
        data_publicacao,
        metadata.get("data_assinatura") or "",
    )
    metadata["data_inicio_vigencia"] = vigencia["data_inicio_vigencia"]
    metadata["vacatio_dias"] = vigencia["vacatio_dias"]
    metadata["vigencia_regra"] = vigencia["vigencia_regra"]
    metadata["vigencia_trecho"] = vigencia["vigencia_trecho"]


def write_rows_csv(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    rows = list(rows)
    if not rows:
        return
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_rows_jsonl(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def collect_tracking_rows(metadata_rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    tracking_rows: List[Dict[str, str]] = []
    for row in metadata_rows:
        raw = row.get("rastreamento_dispositivos_json") or "[]"
        try:
            events = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for event in events:
            event = dict(event)
            event["doc_id"] = row.get("doc_id", "")
            event["titulo"] = row.get("titulo", "")
            event["tipo_norma"] = row.get("tipo_norma", "")
            event["numero_norma"] = row.get("numero_norma", "")
            event["ano_norma"] = row.get("ano_norma", "")
            tracking_rows.append(event)
    return tracking_rows


class AnmLegisDownloader:
    def __init__(self, output_dir: Path, sleep_seconds: float = 0.5, timeout: int = 60):
        self.output_dir = output_dir
        self.html_dir = output_dir / "html"
        self.pdf_dir = output_dir / "pdf"
        self.sleep_seconds = sleep_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)

    def get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return decode_response(response)

    def post_pdf(self, ato_html: str, ref: ActRef) -> Tuple[bytes, str]:
        params = {"acao": "gerarPdfAto", "cod_modulo": ref.cod_modulo, "cod_menu": ref.cod_menu}
        response = self.session.post(
            PDF_ACTION,
            params=params,
            data={"html": ato_html},
            headers={"Referer": ref.public_url()},
            timeout=self.timeout,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not response.content.startswith(b"%PDF"):
            raise RuntimeError(f"Resposta do PDF nao parece PDF: {content_type}")
        return response.content, response.url

    def discover_refs(
        self,
        menu_urls: List[str],
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
    ) -> List[ActRef]:
        refs: Dict[str, ActRef] = {}
        for menu_url in menu_urls:
            menu_html = self.get_text(menu_url)
            menu_title = ""
            title_match = re.search(r'<li[^>]+class=["\']active last["\'][^>]*>[\s\S]*?<a[^>]*>(.*?)</a>', menu_html, re.I)
            if title_match:
                menu_title = strip_tags(title_match.group(1))

            year_urls = extract_year_urls(menu_html, menu_url)
            for year_url in year_urls:
                params = parse_query(year_url)
                year = int(params["ano"]) if params.get("ano", "").isdigit() else None
                if year is not None and year_from is not None and year < year_from:
                    continue
                if year is not None and year_to is not None and year > year_to:
                    continue
                list_html = self.get_text(year_url)
                for ref in extract_act_refs(list_html, year_url, menu_title):
                    refs[ref.key] = ref
                pagination_url = extract_pagination_url(list_html, year_url)
                if pagination_url:
                    stagnant_pages = 0
                    for _ in range(1000):
                        response = self.session.post(
                            pagination_url,
                            data={},
                            headers={
                                "Referer": year_url,
                                "X-Requested-With": "XMLHttpRequest",
                            },
                            timeout=self.timeout,
                        )
                        response.raise_for_status()
                        page_html = decode_response(response)
                        page_refs = extract_act_refs(page_html, year_url, menu_title)
                        if not page_refs:
                            break
                        before = len(refs)
                        for ref in page_refs:
                            refs[ref.key] = ref
                        if len(refs) == before:
                            stagnant_pages += 1
                            if stagnant_pages >= 2:
                                break
                        else:
                            stagnant_pages = 0
                        time.sleep(self.sleep_seconds)
                time.sleep(self.sleep_seconds)
        return list(refs.values())

    def download_one(self, ref: ActRef, skip_existing: bool = True) -> Dict[str, str]:
        effective_ref = ref
        page_url = effective_ref.public_url()
        page_html = self.get_text(page_url)
        ato_html = extract_ato_html(page_html)
        if not ato_html and ref.cod_menu != "8303":
            fallback_ref = ActRef(
                tipo=ref.tipo,
                numero=ref.numero,
                sequencia=ref.sequencia,
                ano=ref.ano,
                orgao=ref.orgao,
                cod_modulo=ref.cod_modulo,
                cod_menu="8303",
                list_url=ref.list_url,
                list_title=ref.list_title,
            )
            fallback_html = self.get_text(fallback_ref.public_url())
            fallback_ato_html = extract_ato_html(fallback_html)
            if fallback_ato_html:
                effective_ref = fallback_ref
                page_url = effective_ref.public_url()
                page_html = fallback_html
                ato_html = fallback_ato_html
        if not ato_html:
            raise RuntimeError("Nao foi encontrado div.ato no HTML do ato.")
        paragraphs = [strip_tags(p) for p in re.findall(r"<p[^>]*>([\s\S]*?)</p>", ato_html, flags=re.I)]
        paragraphs = [p for p in paragraphs if p]

        temp_name = f"{ref.key}.html"
        (self.html_dir / temp_name).write_text(page_html, encoding="utf-8")

        metadata_without_path = extract_metadata(effective_ref, page_html, ato_html, self.pdf_dir / "temp.pdf")
        filename = act_filename_stem(metadata_without_path["titulo"], effective_ref) + ".pdf"
        pdf_path = self.pdf_dir / filename
        if pdf_path.exists() and not skip_existing:
            pdf_path = unique_path(pdf_path)

        pdf_url = ""
        if not pdf_path.exists() or not skip_existing:
            pdf_bytes, pdf_url = self.post_pdf(ato_html, effective_ref)
            pdf_path.write_bytes(pdf_bytes)
            pdf_sha256 = sha256_bytes(pdf_bytes)
        else:
            pdf_sha256 = sha256_bytes(pdf_path.read_bytes())

        html_final_path = self.html_dir / (pdf_path.stem + ".html")
        if html_final_path != self.html_dir / temp_name:
            html_final_path.write_text(page_html, encoding="utf-8")
            try:
                (self.html_dir / temp_name).unlink()
            except OSError:
                pass

        metadata = extract_metadata(effective_ref, page_html, ato_html, pdf_path)
        metadata["source_list_url"] = ref.list_url
        apply_pdf_publication_fallback(metadata, pdf_path, paragraphs)
        metadata["html_path"] = str(html_final_path)
        metadata["pdf_url"] = pdf_url
        metadata["pdf_sha256"] = pdf_sha256
        metadata["downloaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return metadata

    def download_many(self, refs: List[ActRef], limit: Optional[int], skip_existing: bool) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        selected = refs[:limit] if limit else refs
        total = len(selected)
        for index, ref in enumerate(selected, start=1):
            try:
                print(f"[{index}/{total}] {ref.tipo} {ref.numero}/{ref.ano} {ref.orgao}")
                rows.append(self.download_one(ref, skip_existing=skip_existing))
            except Exception as exc:
                rows.append(
                    {
                        "doc_id": "",
                        "tipo_norma": ref.tipo,
                        "numero_norma": ref.numero,
                        "ano_norma": ref.ano,
                        "seq_ato": ref.sequencia,
                        "orgao": ref.orgao,
                        "source_url": ref.public_url(),
                        "erro": str(exc),
                    }
                )
                print(f"  ERRO: {exc}")
            time.sleep(self.sleep_seconds)
        return rows


def load_menu_urls(downloader: AnmLegisDownloader, args: argparse.Namespace) -> List[str]:
    if args.menu_url:
        return args.menu_url
    categories_html = downloader.get_text(args.categories_url)
    menus = extract_menu_urls(categories_html)
    if args.menu_contains:
        needles = [strip_accents(item).lower() for item in args.menu_contains]
        menus = [
            (url, label)
            for url, label in menus
            if any(needle in strip_accents(label).lower() for needle in needles)
        ]
    if args.print_menus:
        for url, label in menus:
            print(f"{label}: {url}")
    return [url for url, _label in menus]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baixa normativos da ANMlegis em PDF do proprio site e extrai metadados do HTML."
    )
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--categories-url", default=CATEGORIES_URL)
    parser.add_argument("--menu-url", action="append", help="URL de menu/resenha especifico. Pode repetir.")
    parser.add_argument(
        "--menu-contains",
        action="append",
        help="Filtra menus descobertos por texto do rotulo, ex.: Resolucao, Portaria.",
    )
    parser.add_argument("--year-from", type=int)
    parser.add_argument("--year-to", type=int)
    parser.add_argument("--limit", type=int, help="Limite para teste.")
    parser.add_argument("--sleep", type=float, default=0.5, help="Pausa entre requisicoes.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-existing", action="store_true", default=True)
    parser.add_argument("--overwrite", action="store_true", help="Baixa novamente PDFs ja existentes.")
    parser.add_argument("--print-menus", action="store_true", help="Mostra os menus descobertos.")
    parser.add_argument("--discover-only", action="store_true", help="Apenas descobre atos e grava manifest.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    downloader = AnmLegisDownloader(args.output_dir, sleep_seconds=args.sleep, timeout=args.timeout)
    menu_urls = load_menu_urls(downloader, args)
    print(f"Menus selecionados: {len(menu_urls)}")

    refs = downloader.discover_refs(menu_urls, year_from=args.year_from, year_to=args.year_to)
    refs = sorted(refs, key=lambda ref: (ref.ano, ref.tipo, ref.numero, ref.sequencia, ref.orgao), reverse=True)
    print(f"Atos descobertos: {len(refs)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    discovered_rows = [
        {
            "tipo_norma": ref.tipo,
            "numero_norma": ref.numero,
            "ano_norma": ref.ano,
            "seq_ato": ref.sequencia,
            "orgao": ref.orgao,
            "source_url": ref.public_url(),
            "source_list_url": ref.list_url,
        }
        for ref in refs
    ]
    write_rows_csv(args.output_dir / "atos_descobertos.csv", discovered_rows)
    write_rows_jsonl(args.output_dir / "atos_descobertos.jsonl", discovered_rows)

    if args.discover_only:
        return 0

    rows = downloader.download_many(
        refs,
        limit=args.limit,
        skip_existing=(args.skip_existing and not args.overwrite),
    )
    write_rows_csv(args.output_dir / "metadados.csv", rows)
    write_rows_jsonl(args.output_dir / "metadados.jsonl", rows)
    tracking_rows = collect_tracking_rows(rows)
    write_rows_csv(args.output_dir / "rastreamento_dispositivos.csv", tracking_rows)
    write_rows_jsonl(args.output_dir / "rastreamento_dispositivos.jsonl", tracking_rows)
    print(f"Metadados: {args.output_dir / 'metadados.csv'}")
    print(f"Rastreamento: {args.output_dir / 'rastreamento_dispositivos.csv'}")
    print(f"PDFs: {downloader.pdf_dir}")
    print(f"HTMLs: {downloader.html_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
