import json
import os
import time

import requests
from dotenv import load_dotenv

from src.config import PROJECT_ENV, USER_ENV

# =====================================================
# CONFIG DE AMBIENTE (CARREGADA EM TEMPO DE USO)
# =====================================================

DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_BASE_URL = "https://lia-api.cgu.gov.br/api/resources/DiretoGpt"
DEFAULT_LLM_MODEL = "gpt-5.4"
DISABLED_LLM_MODELS = set()
DEFAULT_EMBED_MODEL = "text-embedding-3-small"
MIN_O3_API_VERSION = "2024-12-01-preview"
MIN_GPT5_API_VERSION = "2025-04-01-preview"


class LIAClientError(Exception):
    def __init__(self, message, status_code=None, response_text="", details=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text or ""
        self.details = details or {}


class ContentFilterError(LIAClientError):
    pass


def _load_runtime_config():

    if USER_ENV.exists():
        load_dotenv(USER_ENV, override=True)
    elif PROJECT_ENV.exists():
        load_dotenv(PROJECT_ENV, override=True)
    else:
        load_dotenv(override=True)

    api_key = (os.getenv("LIA_API_KEY") or "").strip()

    if not api_key:
        raise ValueError("LIA_API_KEY nao definida no .env")

    return {
        "api_version": os.getenv("LIA_API_VERSION", DEFAULT_API_VERSION),
        "base_url": os.getenv("LIA_ENDPOINT", DEFAULT_BASE_URL),
        "llm_model": os.getenv("LIA_LLM_MODEL", DEFAULT_LLM_MODEL),
        "embed_model": os.getenv("LIA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        "headers": {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    }


def resolve_llm_model(llm_model=None, disabled_models=None):
    cfg = _load_runtime_config()
    model_name = str(llm_model or cfg["llm_model"]).strip() or DEFAULT_LLM_MODEL
    disabled_models = set(disabled_models or DISABLED_LLM_MODELS)

    if model_name in disabled_models:
        return DEFAULT_LLM_MODEL

    return model_name


# =====================================================
# EMBEDDING EM BATCH
# =====================================================

def get_embedding_batch(texts, max_retries=5):

    cfg = _load_runtime_config()
    url = (
        f"{cfg['base_url']}/openai/deployments/{cfg['embed_model']}"
        f"/embeddings?api-version={cfg['api_version']}"
    )

    body = {
        "input": [t.replace("\n", " ") for t in texts]
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(
                url,
                headers=cfg["headers"],
                json=body,
                timeout=120,
            )

            if response.status_code == 200:
                data = response.json()["data"]
                return [item["embedding"] for item in data]

            if response.status_code == 404:
                raise Exception(
                    "Endpoint nao encontrado (404).\n"
                    "Verifique LIA_ENDPOINT no .env.\n"
                    f"URL usada: {url}"
                )

            if response.status_code >= 500:
                wait = 2 ** attempt
                print(f"Erro {response.status_code}. Retry em {wait}s...")
                time.sleep(wait)
                continue

            raise Exception(
                f"Erro embedding batch: {response.status_code} - {response.text}"
            )

        except requests.exceptions.RequestException:
            wait = 2 ** attempt
            print(f"Erro de rede. Retry em {wait}s...")
            time.sleep(wait)

    raise Exception("Falha apos multiplas tentativas de embedding batch.")


# =====================================================
# CHAT COMPLETION
# =====================================================

def _supports_temperature(model_name):

    model = str(model_name or "").strip().lower()
    return not (model.startswith("o3") or model.startswith("gpt-5"))


def _extract_version_date(version_text):

    raw = str(version_text or "").strip()

    if len(raw) >= 10:
        candidate = raw[:10]
        if (
            candidate[4:5] == "-"
            and candidate[7:8] == "-"
            and candidate.replace("-", "").isdigit()
        ):
            return candidate

    return ""


def _resolve_chat_api_version(model_name, configured_api_version):

    model = str(model_name or "").strip().lower()
    configured = str(configured_api_version or "").strip()

    if model.startswith("gpt-5"):
        configured_date = _extract_version_date(configured)
        min_date = _extract_version_date(MIN_GPT5_API_VERSION)
        if configured_date and min_date and configured_date >= min_date:
            return configured
        return MIN_GPT5_API_VERSION

    if not model.startswith("o3"):
        return configured or DEFAULT_API_VERSION

    configured_date = _extract_version_date(configured)
    min_date = _extract_version_date(MIN_O3_API_VERSION)

    if configured_date and min_date and configured_date >= min_date:
        return configured

    return MIN_O3_API_VERSION


def _parse_error_payload(response):

    raw_text = str(getattr(response, "text", "") or "")

    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(raw_text)
        except Exception:
            payload = {}

    if not isinstance(payload, dict):
        payload = {}

    return payload, raw_text


def _is_content_filter_block(status_code, payload, raw_text):

    if int(status_code or 0) != 400:
        return False

    detail = str(payload.get("detail", "")).lower()
    error_obj = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    error_code = str(error_obj.get("code", "")).lower()
    error_message = str(error_obj.get("message", "")).lower()
    full_text = str(raw_text or "").lower()

    signals = (
        "content management policy",
        "content filter",
        "filtered due to",
        "responsibleai",
        "content_filter",
    )

    combined = " | ".join([detail, error_code, error_message, full_text])
    return any(token in combined for token in signals)


def chat_completion(
    messages,
    temperature=0.0,
    max_retries=5,
    llm_model=None,
    api_version=None,
    allow_only_entraid=None,
):

    cfg = _load_runtime_config()
    model_name = resolve_llm_model(llm_model)
    api_version = _resolve_chat_api_version(model_name, api_version or cfg["api_version"])
    url = (
        f"{cfg['base_url']}/openai/deployments/{model_name}"
        f"/chat/completions?api-version={api_version}"
    )
    if allow_only_entraid is not None:
        flag = "true" if bool(allow_only_entraid) else "false"
        url += f"&allow_only_entraid={flag}"

    body = {
        "messages": messages
    }

    if _supports_temperature(model_name):
        body["temperature"] = float(temperature)

    for attempt in range(max_retries):
        last_status_code = None
        last_response_text = ""

        try:
            response = requests.post(
                url,
                headers=cfg["headers"],
                json=body,
                timeout=120,
            )

            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]

            last_status_code = response.status_code
            last_response_text = response.text

            if response.status_code == 404:
                raise LIAClientError(
                    "Endpoint nao encontrado (404).\n"
                    "Verifique LIA_ENDPOINT no .env.\n"
                    f"URL usada: {url}",
                    status_code=404,
                    response_text=response.text,
                )

            if response.status_code >= 500:
                if attempt >= max_retries - 1:
                    raise LIAClientError(
                        f"Erro LLM: {response.status_code} - {response.text}",
                        status_code=response.status_code,
                        response_text=response.text,
                    )

                wait = 2 ** attempt
                print(f"Erro {response.status_code}. Retry em {wait}s...")
                time.sleep(wait)
                continue

            payload, raw_text = _parse_error_payload(response)

            if _is_content_filter_block(response.status_code, payload, raw_text):
                detail = str(payload.get("detail", "")).strip()
                message = (
                    "Erro LLM: chamada bloqueada pela politica de content filter do provedor."
                )
                if detail:
                    message += f" Detalhe: {detail}"

                raise ContentFilterError(
                    message,
                    status_code=response.status_code,
                    response_text=raw_text,
                    details=payload,
                )

            raise LIAClientError(
                f"Erro LLM: {response.status_code} - {raw_text}",
                status_code=response.status_code,
                response_text=raw_text,
                details=payload,
            )

        except requests.exceptions.RequestException as exc:
            if attempt >= max_retries - 1:
                raise LIAClientError(
                    f"Erro de rede na chamada LLM: {exc}",
                    status_code=last_status_code,
                    response_text=last_response_text,
                )

            wait = 2 ** attempt
            print(f"Erro de rede. Retry em {wait}s...")
            time.sleep(wait)

    raise LIAClientError("Falha apos multiplas tentativas de chat.")


def get_runtime_llm_model(llm_model=None):

    return resolve_llm_model(llm_model)
