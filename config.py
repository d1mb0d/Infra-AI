import os
from typing import Dict, List, Tuple
from openai import OpenAI

# ==============================================================================
# 1. ПУТИ К ФАЙЛАМ И КАТАЛОГАМ ПРОЕКТА
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "infra_equipment.db")
INBOX_PRICELISTS_DIR = os.path.join(BASE_DIR, "inbox_pricelists")
INBOX_TZ_DIR = os.path.join(BASE_DIR, "inbox_tz")
OUTPUT_KP_DIR = os.path.join(BASE_DIR, "output_kp")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
DRAWINGS_DIR = os.path.join(ASSETS_DIR, "drawings")
SIGNATURE_IMAGE_PATH = os.path.join(ASSETS_DIR, "signature_voronoy.png")

for _d in [INBOX_PRICELISTS_DIR, INBOX_TZ_DIR, OUTPUT_KP_DIR, ASSETS_DIR, DRAWINGS_DIR]:
    os.makedirs(_d, exist_ok=True)


# ==============================================================================
# 2. РЕКВИЗИТЫ КОМПАНИИ ООО «НИЦ «ИНФРА» (ДЛЯ БЛАНКОВ ТКП)
# ==============================================================================
COMPANY_INFO = {
    "short_name": "ООО «НИЦ «ИНФРА»",
    "full_name": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «НАУЧНО-ИНЖЕНЕРНЫЙ ЦЕНТР «ИНФРА»",
    "tagline": "Технологический аудит, инжиниринг и сервис систем бесперебойного электроснабжения, АСУ ТП и ЦОД",
    "city": "г. Новосибирск",
    "director_title": "Генеральный директор",
    "director_name": "Вороной Вадим Владимирович",
    "phone": "+7-913-951-81-31",
    "email": "mail@infra-v.ru",
    "requisites_rows": [
        ("Полное наименование", "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «НАУЧНО-ИНЖЕНЕРНЫЙ ЦЕНТР «ИНФРА»"),
        ("Сокращённое наименование", "ООО «НИЦ «ИНФРА»"),
        ("ИНН / КПП", "5404959419 / 540401001"),
        ("ОГРН", "1255400018972"),
        ("Генеральный директор", "ВОРОНОЙ ВАДИМ ВЛАДИМИРОВИЧ"),
        ("Адрес юридический / почтовый", "630029, Новосибирская область, г. Новосибирск, ул. Ясный Берег, д. 12, кв. 44"),
        ("Адрес фактический", "630001, Новосибирская область, г. Новосибирск, Красный проспект 72Б, 3 этаж, офис №4"),
        ("Наименование банка", "ФИЛИАЛ «НОВОСИБИРСКИЙ» АО «АЛЬФА-БАНК»"),
        ("БИК / К/с", "045004774 / 30101810600000000774"),
        ("Расчётный счёт (Р/с)", "40702810423570001349"),
        ("Электронная почта / Телефон", "mail@infra-v.ru  |  +7-913-951-81-31"),
    ],
}


# ==============================================================================
# 3. КОММЕРЧЕСКИЕ ПАРАМЕТРЫ РАСЧЕТА (ПО СТАНДАРТУ EXCEL-КАЛЬКУЛЯЦИИ КОМПАНИИ)
# ==============================================================================
DEFAULT_FINANCE_PARAMS = {
    "target_profitability_pct": 35.0,  # Целевая рентабельность от цены продажи (35%)
    "delivery_pct": 7.0,               # Транспортно-логистические расходы (7%)
    "vat_pct": 22.0,                   # Ставка НДС (22%)
}


# ==============================================================================
# 4. КОНФИГУРАЦИЯ LLM (КАСКАД БЕСПЛАТНЫХ ОБЛАЧНЫХ API + АВТОПОИСК OLLAMA)
# ==============================================================================
import json
import urllib.request


def _load_dotenv_file() -> Dict[str, str]:
    """Читает ключи из diplom/.env и обновляет os.environ при каждом вызове."""
    loaded: Dict[str, str] = {}
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return loaded
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                os.environ[k] = v
                loaded[k] = v
    return loaded


def _clean_api_key(raw_key: str) -> str:
    """Очищает ключ от пробелов, кавычек и случайно скопированного префикса GEMINI_API_KEY=."""
    k = (raw_key or "").strip().strip('"').strip("'")
    for prefix in ("GEMINI_API_KEY=", "GROQ_API_KEY=", "OPENROUTER_API_KEY="):
        if k.startswith(prefix):
            k = k[len(prefix):].strip().strip('"').strip("'")
    return k


_load_dotenv_file()

GEMINI_API_KEY = _clean_api_key(os.getenv("GEMINI_API_KEY", ""))
GROQ_API_KEY = _clean_api_key(os.getenv("GROQ_API_KEY", ""))
OPENROUTER_API_KEY = _clean_api_key(os.getenv("OPENROUTER_API_KEY", ""))
LOCAL_OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434/v1")

FREE_CLOUD_PROVIDERS: List[Dict[str, str]] = [
    {
        "name": "Google AI Studio (Gemini 4 Hydrogen — Free Tier)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_var": "GEMINI_API_KEY",
        "api_key": GEMINI_API_KEY,
        "model": "gemini-4-hydrogen",
        "vlm_model": "gemini-4-hydrogen",
    },
    {
        "name": "Google AI Studio (Gemini 3.8 Flash — Free Tier)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_var": "GEMINI_API_KEY",
        "api_key": GEMINI_API_KEY,
        "model": "gemini-3.8-flash",
        "vlm_model": "gemini-3.8-flash",
    },
    {
        "name": "Google AI Studio (Gemini 3.5 Flash-Lite — Free Tier)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_var": "GEMINI_API_KEY",
        "api_key": GEMINI_API_KEY,
        "model": "gemini-3.5-flash-lite",
        "vlm_model": "gemini-3.5-flash-lite",
    },
    {
        "name": "Groq Cloud (Qwen3-32B)",
        "base_url": "https://api.groq.com/openai/v1",
        "env_var": "GROQ_API_KEY",
        "api_key": GROQ_API_KEY,
        "model": "qwen/qwen3-32b",
        "vlm_model": "",
    },
    {
        "name": "OpenRouter Free (Qwen3-32B)",
        "base_url": "https://openrouter.ai/api/v1",
        "env_var": "OPENROUTER_API_KEY",
        "api_key": OPENROUTER_API_KEY,
        "model": "qwen/qwen3-32b:free",
        "vlm_model": "qwen/qwen2.5-vl-72b-instruct:free",
    },
]


def get_active_cloud_candidates(custom_api_key: str = "") -> List[Dict[str, str]]:
    """
    Собирает полный упорядоченный список доступных облачных моделей
    с учётом актуального .env и ключа, введённого в интерфейсе Streamlit.
    """
    _load_dotenv_file()
    ui_key = _clean_api_key(custom_api_key)
    gemini_k = ui_key if ui_key.startswith("AIza") else _clean_api_key(os.getenv("GEMINI_API_KEY", ""))
    openrouter_k = ui_key if ui_key.startswith("sk-or-") else _clean_api_key(os.getenv("OPENROUTER_API_KEY", ""))
    groq_k = ui_key if (ui_key and not ui_key.startswith(("AIza", "sk-or-"))) else _clean_api_key(os.getenv("GROQ_API_KEY", ""))

    key_by_env = {
        "GEMINI_API_KEY": gemini_k,
        "OPENROUTER_API_KEY": openrouter_k,
        "GROQ_API_KEY": groq_k,
    }

    active: List[Dict[str, str]] = []
    for prov in FREE_CLOUD_PROVIDERS:
        actual_k = key_by_env.get(prov["env_var"], "")
        if actual_k:
            item = dict(prov)
            item["api_key"] = actual_k
            active.append(item)
    return active


def _detect_installed_ollama_model(preferred: str = "qwen3:30b-a3b") -> str:
    """Проверяет список реально скачанных моделей в локальной Ollama."""
    try:
        tags_url = LOCAL_OLLAMA_URL.replace("/v1", "/api/tags")
        with urllib.request.urlopen(tags_url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            installed = [m.get("name", "") for m in data.get("models", [])]
            if preferred in installed:
                return preferred
            if installed:
                return installed[0]
    except Exception:
        pass
    return preferred


def get_llm_client(
    prefer_local_ollama: bool = False,
    ollama_model: str = "qwen3:30b-a3b",
    custom_api_key: str = "",
) -> Tuple[OpenAI, str, str]:
    """Возвращает (OpenAI-клиент, имя_модели, название_провайдера)."""
    if prefer_local_ollama:
        actual_ollama = _detect_installed_ollama_model(ollama_model)
        return OpenAI(base_url=LOCAL_OLLAMA_URL, api_key="ollama"), actual_ollama, f"Local Ollama ({actual_ollama})"

    candidates = get_active_cloud_candidates(custom_api_key)
    if candidates:
        first = candidates[0]
        return OpenAI(base_url=first["base_url"], api_key=first["api_key"]), first["model"], first["name"]

    actual_ollama = _detect_installed_ollama_model(ollama_model)
    return OpenAI(base_url=LOCAL_OLLAMA_URL, api_key="ollama"), actual_ollama, f"Local Ollama ({actual_ollama})"