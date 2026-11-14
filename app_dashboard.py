import os
import re
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime
from typing import Any, Dict, List
import pandas as pd
import streamlit as st

from config import (
    BASE_DIR,
    COMPANY_INFO,
    DEFAULT_FINANCE_PARAMS,
    INBOX_PRICELISTS_DIR,
    INBOX_TZ_DIR,
    OUTPUT_KP_DIR,
)
from db_core_and_sysadmin import (
    init_admin_tables,
    sync_cbr_exchange_rates,
    ingest_new_pricelist_file,
    approve_staged_change,
    get_current_rates_dict,
    get_db_connection,
)
from db_tools import stage_price_update
from kp_generator import generate_commercial_proposal_docx
from memory_and_guardrails import SecurityGuardrails
from multi_agent_hierarchy import ConductorSupervisor
from rag_knowledge_base import HybridKnowledgeRetriever, seed_infra_engineering_datasheets
from tz_ingestor import CustomerTZIngestor

# ==============================================================================
# НАСТРОЙКА СТРАНИЦЫ И ФИРМЕННОГО СТИЛЯ ООО «НИЦ «ИНФРА»
# ==============================================================================
st.set_page_config(
    page_title="ИИ-Штаб ООО «НИЦ «ИНФРА» | Система расчёта ТКП",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .main-header {
        background: linear-gradient(90deg, #1B365D 0%, #2B5288 65%, #D97706 100%);
        padding: 1.1rem 1.6rem;
        border-radius: 10px;
        color: white;
        margin-bottom: 0.75rem;
    }
    .main-header h1 {
        color: white !important;
        font-size: 1.55rem !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    .main-header p {
        color: #F1F5F9 !important;
        font-size: 0.9rem !important;
        margin: 0.25rem 0 0 0 !important;
    }
    div[data-testid="stMetric"] {
        background-color: #F8FAFC !important;
        border: 1px solid #CBD5E1 !important;
        padding: 12px 16px !important;
        border-radius: 8px !important;
    }
    div[data-testid="stMetric"] label,
    div[data-testid="stMetric"] [data-testid="stMetricLabel"],
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #0F172A !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

init_admin_tables()
seed_infra_engineering_datasheets()
os.makedirs(INBOX_TZ_DIR, exist_ok=True)
os.makedirs(INBOX_PRICELISTS_DIR, exist_ok=True)
os.makedirs(OUTPUT_KP_DIR, exist_ok=True)

# ==============================================================================
# РОЛЕВАЯ СИСТЕМА ДОСТУПА (РУКОВОДИТЕЛЬ / МЕНЕДЖЕР)
# ==============================================================================
USERS_RBAC: Dict[str, Dict[str, str]] = {
    "director": {
        "password": os.getenv("INFRA_DIRECTOR_PASS", "infra2026"),
        "role": "DIRECTOR",
        "badge": "👑 Руководитель (Полный доступ)",
        "display_name": "Вороной В.В. (Генеральный директор)",
    },
    "manager": {
        "password": os.getenv("INFRA_MANAGER_PASS", "manager2026"),
        "role": "MANAGER",
        "badge": "💼 Менеджер (Расчёт ТКП и прайсы)",
        "display_name": "Менеджер отдела продаж ООО «НИЦ «ИНФРА»",
    },
}

if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

if st.session_state["auth_user"] is None:
    st.markdown(
        f"""
        <div class="main-header">
            <h1>🔐 Вход в корпоративный ИИ-Штаб {COMPANY_INFO['short_name']}</h1>
            <p>Разграничение прав доступа: контур Руководителя и операционный контур Менеджера</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    col_l1, col_l2, col_l3 = st.columns([1.1, 1.4, 1.1])
    with col_l2:
        selected_login = st.selectbox(
            "Выберите учётную запись:",
            list(USERS_RBAC.keys()),
            format_func=lambda k: f"{USERS_RBAC[k]['badge']} — {USERS_RBAC[k]['display_name']}",
        )
        default_hint_pass = USERS_RBAC[selected_login]["password"]
        password_input = st.text_input("Пароль доступа:", type="password", value=default_hint_pass)
        if st.button("🚀 Войти в систему", type="primary", use_container_width=True):
            if password_input == USERS_RBAC[selected_login]["password"]:
                st.session_state["auth_user"] = {"login": selected_login, **USERS_RBAC[selected_login]}
                st.rerun()
            else:
                st.error("❌ Неверный пароль. Проверьте учётные данные.")
    st.stop()

CURRENT_USER = st.session_state["auth_user"]
CURRENT_ROLE = CURRENT_USER["role"]
IS_DIRECTOR = CURRENT_ROLE == "DIRECTOR"

# ==============================================================================
# ИНИЦИАЛИЗАЦИЯ СОСТОЯНИЯ СЕССИИ И БУФЕРА МЕТАДАННЫХ
# ==============================================================================
if "matched_items" not in st.session_state:
    st.session_state["matched_items"] = []
if "not_found_items" not in st.session_state:
    st.session_state["not_found_items"] = []
if "accuracy_pct" not in st.session_state:
    st.session_state["accuracy_pct"] = 0.0
if "agent_logs" not in st.session_state:
    st.session_state["agent_logs"] = []
if "kit_header_title" not in st.session_state:
    st.session_state["kit_header_title"] = None
if "ui_client_name" not in st.session_state:
    st.session_state["ui_client_name"] = "ООО «СибМедСтрой»"
if "ui_client_addr" not in st.session_state:
    st.session_state["ui_client_addr"] = "г. Новокузнецк, просп. Металлургов, 19"
if "ui_project_name" not in st.session_state:
    st.session_state["ui_project_name"] = "Система бесперебойного электроснабжения"

if "pending_ui_meta" in st.session_state and st.session_state["pending_ui_meta"]:
    meta = st.session_state.pop("pending_ui_meta")
    if meta.get("client_name"):
        st.session_state["ui_client_name"] = meta["client_name"]
    if meta.get("client_address"):
        st.session_state["ui_client_addr"] = meta["client_address"]
    if meta.get("project_name"):
        st.session_state["ui_project_name"] = meta["project_name"]


# ==============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================================================================
def safe_save_uploaded_file(uploaded_file, target_dir: str) -> str:
    os.makedirs(target_dir, exist_ok=True)
    primary_path = os.path.join(target_dir, uploaded_file.name)
    try:
        with open(primary_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        return primary_path
    except PermissionError:
        base, ext = os.path.splitext(uploaded_file.name)
        fallback_name = f"{base}_копия_{datetime.now().strftime('%H%M%S')}{ext}"
        fallback_path = os.path.join(target_dir, fallback_name)
        with open(fallback_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        st.warning(
            f"⚠️ Файл **`{uploaded_file.name}`** сейчас открыт в другой программе и заблокирован для перезаписи. "
            f"Создана рабочая копия **`{fallback_name}`**."
        )
        return fallback_path


def apply_fx_buffer_to_items(items: List[Dict[str, Any]], fx_buffer_pct: float) -> List[Dict[str, Any]]:
    """
    Применяет страховой запас к курсу валюты (по умолчанию +5%) для импортных позиций,
    чтобы защитить маржу от скачков доллара/юаня/евро.
    """
    if fx_buffer_pct <= 0:
        return items

    mult = 1.0 + (fx_buffer_pct / 100.0)
    adjusted: List[Dict[str, Any]] = []
    for raw_it in items:
        it = dict(raw_it)
        curr = str(it.get("currency_code") or "RUB").upper()
        if curr != "RUB":
            if it.get("rrc_price_rub_with_vat") is not None:
                it["rrc_price_rub_with_vat"] = round(float(it["rrc_price_rub_with_vat"]) * mult, 2)
            if it.get("partner_cost_rub") is not None:
                it["partner_cost_rub"] = round(float(it["partner_cost_rub"]) * mult, 2)
            elif it.get("price_tiers"):
                tiers = json.loads(it["price_tiers"]) if isinstance(it["price_tiers"], str) else dict(it["price_tiers"])
                for k_tier, v_tier in list(tiers.items()):
                    if isinstance(v_tier, (int, float)) and "rub" not in k_tier.lower():
                        tiers[k_tier] = round(float(v_tier) * mult, 4)
                it["price_tiers"] = json.dumps(tiers, ensure_ascii=False)
        adjusted.append(it)
    return adjusted


def safe_generate_kp_files(
    items: List[Dict[str, Any]],
    client_name: str,
    client_address: str,
    project_name: str,
    rent_percent: float,
    delivery_percent: float,
    bundled_mode: bool = False,
    kit_header_title: str = None,
    include_pnr: bool = True,
    pnr_separate_rub: float = 0.0,
    default_filename: str = "TKP_Dashboard_Current.docx",
) -> Dict[str, Any]:
    try:
        return generate_commercial_proposal_docx(
            items=items,
            client_name=client_name,
            client_address=client_address,
            project_name=project_name,
            markup_over_cost_percent=rent_percent,
            delivery_percent=delivery_percent,
            output_filename=default_filename,
            bundled_mode=bundled_mode,
            kit_header_title=kit_header_title,
            include_pnr_in_price=include_pnr,
            pnr_separate_rub=pnr_separate_rub,
        )
    except PermissionError:
        alt_filename = f"TKP_Dashboard_{datetime.now().strftime('%H%M%S')}.docx"
        st.warning(
            f"⚠️ Файл `{default_filename}` сейчас открыт в MS Word. "
            f"Обновлённый документ сохранён под именем **`{alt_filename}`**."
        )
        return generate_commercial_proposal_docx(
            items=items,
            client_name=client_name,
            client_address=client_address,
            project_name=project_name,
            markup_over_cost_percent=rent_percent,
            delivery_percent=delivery_percent,
            output_filename=alt_filename,
            bundled_mode=bundled_mode,
            kit_header_title=kit_header_title,
            include_pnr_in_price=include_pnr,
            pnr_separate_rub=pnr_separate_rub,
        )


def reject_staged_change(update_id: int, rejected_by: str = "Руководитель") -> Dict[str, Any]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM price_updates_staging WHERE update_id = ? AND status = 'PENDING_APPROVAL'",
        (update_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"status": "ERROR", "message": f"Активная заявка #{update_id} не найдена."}

    cur.execute("UPDATE price_updates_staging SET status = 'REJECTED' WHERE update_id = ?", (update_id,))
    cur.execute(
        """
        INSERT INTO audit_logs (actor, action, sku, old_val, new_val, details)
        VALUES (?, 'REJECTED_STAGING', ?, ?, ?, ?)
        """,
        (rejected_by, row["sku"], row["old_price"], row["new_price"], f"Отклонено: {row['reason']}"),
    )
    conn.commit()
    conn.close()
    return {"status": "REJECTED", "update_id": update_id, "sku": row["sku"]}


def build_clean_project_zip() -> bytes:
    """Упаковывает чистый код проекта (без служебных папок и ключей) в ZIP для передачи руководителю."""
    tmp_dir = tempfile.mkdtemp()
    archive_base = os.path.join(tmp_dir, "NIC_INFRA_AI_System")
    staging_copy = os.path.join(tmp_dir, "NIC_INFRA_AI_System")
    os.makedirs(staging_copy, exist_ok=True)

    ignore_names = {"venv", ".venv", "__pycache__", ".git", ".idea", ".env", "output_kp"}
    for entry in os.listdir(BASE_DIR):
        if entry in ignore_names or entry.endswith(".pyc"):
            continue
        src = os.path.join(BASE_DIR, entry)
        dst = os.path.join(staging_copy, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)

    zip_path = shutil.make_archive(archive_base, "zip", tmp_dir, "NIC_INFRA_AI_System")
    with open(zip_path, "rb") as f:
        data = f.read()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return data


def _sync_llm_metadata_to_session(res_llm: Dict[str, Any]):
    comm_rep = res_llm.get("commercial", {})
    st.session_state["pending_ui_meta"] = {
        "client_name": comm_rep.get("client_name"),
        "client_address": comm_rep.get("client_address"),
        "project_name": comm_rep.get("project_name"),
    }


rates_dict = get_current_rates_dict()

# ==============================================================================
# БОКОВАЯ ПАНЕЛЬ: УЧЁТНАЯ ЗАПИСЬ, ПАРАМЕТРЫ СДЕЛКИ И НАДБАВКА НА КУРС
# ==============================================================================
with st.sidebar:
    st.markdown(f"### 🏢 {COMPANY_INFO['short_name']}")
    st.success(f"**{CURRENT_USER['badge']}**\n\n{CURRENT_USER['display_name']}")
    if st.button("🚪 Сменить пользователя / Выйти", use_container_width=True):
        st.session_state["auth_user"] = None
        st.rerun()

    st.divider()
    st.markdown("#### 💼 Параметры сделки и ТКП")
    client_name_input = st.text_input("Заказчик (Контрагент Битрикс24):", key="ui_client_name")
    client_addr_input = st.text_input("Адрес объекта / доставки:", key="ui_client_addr")
    project_name_input = st.text_input("Объект / Наименование ТЗ:", key="ui_project_name")

    if IS_DIRECTOR:
        rent_slider = st.slider(
            "Целевая рентабельность от выручки (%):",
            min_value=0.0,
            max_value=65.0,
            value=float(DEFAULT_FINANCE_PARAMS["target_profitability_pct"]),
            step=0.5,
            help="Формула расчёта: Цена = (Себестоимость / (1 - Рент.)) * (1 + Доставка)",
        )
        delivery_slider = st.slider(
            "Транспортно-логистические расходы (%):",
            min_value=0.0,
            max_value=20.0,
            value=float(DEFAULT_FINANCE_PARAMS["delivery_pct"]),
            step=0.5,
        )
        fx_buffer_slider = st.slider(
            "Страховой запас на курс валют (%):",
            min_value=0.0,
            max_value=25.0,
            value=5.0,
            step=0.5,
            help="Защита от колебаний валюты: увеличивает расчётный курс USD/EUR/CNY на заданный процент.",
        )
    else:
        rent_slider = float(DEFAULT_FINANCE_PARAMS["target_profitability_pct"])
        delivery_slider = float(DEFAULT_FINANCE_PARAMS["delivery_pct"])
        fx_buffer_slider = 5.0
        st.info(
            f"🔒 Регламент: рентабельность **{rent_slider}%**, логистика **{delivery_slider}%**, "
            f"валютный запас **+{fx_buffer_slider}%**."
        )

    bundled_mode_chk = st.checkbox(
        "Сводная цена комплекта (скрыть цены внутри состава)",
        value=False,
    )
    include_pnr_chk = st.checkbox("Включить ШМР и ПНР в стоимость оборудования", value=True)
    pnr_sep_val = 0.0
    if not include_pnr_chk:
        pnr_sep_val = st.number_input("Ориентировочная стоимость ШМР/ПНР отдельно (₽):", value=150000.0, step=10000.0)

    st.divider()
    st.markdown("#### 🧠 Движок распознавания ТЗ")
    llm_mode = st.selectbox(
        "Режим анализа заявки:",
        [
            "🆓 Облачный ИИ (Google Gemini 4 / 3.8 Flash)",
            "⚡ Встроенный инженерный парсер (без ИИ)",
            "🖥️ Локальная нейросеть на видеокарте",
        ],
    )
    custom_key_input = ""
    ollama_model_input = "qwen3:30b-a3b"
    if "Облачный ИИ" in llm_mode:
        custom_key_input = st.text_input("Ключ доступа к облачному ИИ (если не задан в .env):", type="password")
    elif "Локальная нейросеть" in llm_mode:
        ollama_model_input = st.text_input("Имя локальной модели:", value="qwen3:30b-a3b")


# ==============================================================================
# ШАПКА ДАШБОРДА + ДВУХСТРОЧНАЯ ПЛАШКА КУРСОВ ВАЛЮТ (ЦБ РФ И РАСЧЁТНЫЙ +ЗАПАС)
# ==============================================================================
st.markdown(
    f"""
    <div class="main-header">
        <h1>⚡ Инженерно-коммерческий ИИ-Штаб {COMPANY_INFO['short_name']}</h1>
        <p>Инженерный расчёт ИБП/АКБ • Подбор оборудования • Формирование официальных ТКП • Роль: {CURRENT_USER['badge']}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

fx_mult = 1.0 + (fx_buffer_slider / 100.0)
usd_cbr = rates_dict.get("USD", 82.15)
eur_cbr = rates_dict.get("EUR", 91.03)
cny_cbr = rates_dict.get("CNY", 12.31)
veda_cbr = rates_dict.get("CNY_VEDA", 12.31)

bar_c1, bar_c2 = st.columns([5.2, 1.3])
with bar_c1:
    st.markdown(
        f"""
        <div style="background:#F8FAFC; border:1px solid #CBD5E1; border-radius:8px; padding:9px 18px;
                    color:#0F172A; font-size:0.9rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px; padding-bottom:6px; border-bottom:1px dashed #CBD5E1;">
                <div><span style="color:#64748B; font-weight:600;">USD ЦБ РФ</span>  <b>{usd_cbr:.2f} ₽</b></div>
                <div><span style="color:#64748B; font-weight:600;">EUR ЦБ РФ</span>  <b>{eur_cbr:.2f} ₽</b></div>
                <div><span style="color:#64748B; font-weight:600;">CNY ЦБ РФ</span>  <b>{cny_cbr:.4f} ₽</b></div>
                <div><span style="color:#64748B; font-weight:600;">у.е. VEDA (база)</span>  <b>{veda_cbr:.4f} ₽</b></div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px; padding-top:6px;">
                <div><span style="color:#B45309; font-weight:600;">USD (+{fx_buffer_slider:g}%)</span>  <b style="color:#B45309; font-size:1.02rem;">{usd_cbr * fx_mult:.2f} ₽</b></div>
                <div><span style="color:#B45309; font-weight:600;">EUR (+{fx_buffer_slider:g}%)</span>  <b style="color:#B45309; font-size:1.02rem;">{eur_cbr * fx_mult:.2f} ₽</b></div>
                <div><span style="color:#B45309; font-weight:600;">CNY (+{fx_buffer_slider:g}%)</span>  <b style="color:#B45309; font-size:1.02rem;">{cny_cbr * fx_mult:.4f} ₽</b></div>
                <div><span style="color:#B45309; font-weight:600;">у.е. VEDA (+{fx_buffer_slider:g}%)</span>  <b style="color:#B45309; font-size:1.02rem;">{veda_cbr * fx_mult:.4f} ₽</b></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with bar_c2:
    st.write("")
    if st.button("🔄 Курсы ЦБ РФ", use_container_width=True):
        with st.spinner("Синхронизация с ЦБ РФ..."):
            sync_cbr_exchange_rates(use_live_cbr_api=True)
            st.rerun()

conn_badge = get_db_connection()
cur_badge = conn_badge.cursor()
cur_badge.execute("SELECT COUNT(*) as cnt FROM price_updates_staging WHERE status = 'PENDING_APPROVAL'")
pending_count = cur_badge.fetchone()["cnt"]
conn_badge.close()
staging_badge = f" (🔔 На проверке: {pending_count})" if pending_count > 0 else ""

if IS_DIRECTOR:
    tab1, tab2, tab3, tab4 = st.tabs([
        "📋 Часть 1: Расчёт ТКП и загрузка ТЗ",
        f"⚙️ Часть 2: Управление прайсами, курсами и согласованием{staging_badge}",
        "🛡️ Часть 3: Журнал безопасности, Каталог БД и База паспортов",
        "📖 Часть 4: Архитектура системы и выгрузка проекта",
    ])
else:
    tab1, tab2, tab3 = st.tabs([
        "📋 Часть 1: Расчёт ТКП для клиента",
        f"⚙️ Часть 2: Обновление курсов и загрузка прайсов{staging_badge}",
        "📦 Часть 3: Каталог оборудования и База паспортов",
    ])
    tab4 = None


# ==============================================================================
# ЧАСТЬ 1: РАБОЧЕЕ МЕСТО РАСЧЁТА ТКП И ДЕКОМПОЗИЦИИ ТЗ
# ==============================================================================
with tab1:
    st.markdown("### 1. Подача технического задания (ТЗ) или инженерный расчёт системы")

    input_method = st.radio(
        "Выберите способ загрузки заявки клиента:",
        [
            "✍️ Вставить текст письма / заявки из чата Битрикс24",
            "🔋 Инженерный конструктор ИБП и автономии",
            "📂 Загрузить файл ТЗ (Excel, CSV, Word, TXT)",
        ],
        horizontal=True,
    )

    spec_rows_to_run: List[Dict[str, Any]] = []
    trigger_calculation = False

    if "Инженерный конструктор" in input_method:
        u_c1, u_c2, u_c3, u_c4 = st.columns([1.1, 1.1, 1.5, 0.9])
        load_kw_in = u_c1.number_input("Мощность нагрузки (кВт):", min_value=1.0, max_value=500.0, value=12.0, step=1.0)
        autonomy_min_in = u_c2.number_input("Время автономии (минут):", min_value=5, max_value=1440, value=30, step=15)
        chem_in = u_c3.selectbox(
            "Тип аккумуляторных батарей:",
            [
                "AGM (Свинцово-кислотные 12В в закрытом шкафу)",
                "LIFEPO4 (Высоковольтный литий 512В)",
                "AUTO (Автовыбор инженера по времени автономии)",
            ],
        )
        kits_qty_in = u_c4.number_input("Кол-во комплектов:", min_value=1, max_value=20, value=1, step=1)

        chem_code = chem_in.split(" ")[0]
        if st.button("⚡ Рассчитать энергобаланс и собрать комплект ИБП", type="primary"):
            spec_rows_to_run = [{
                "type": "UPS_SIZING",
                "load_kw": load_kw_in,
                "autonomy_minutes": int(autonomy_min_in),
                "battery_chemistry": chem_code,
                "quantity": int(kits_qty_in),
            }]
            trigger_calculation = True

    elif "Загрузить файл" in input_method:
        uploaded_file = st.file_uploader(
            "Выберите или перетащите ведомость оборудования от заказчика (Excel, CSV, Word или TXT):",
            type=["xlsx", "xls", "csv", "docx", "txt"],
        )
        if uploaded_file is not None:
            saved_path = safe_save_uploaded_file(uploaded_file, INBOX_TZ_DIR)
            parsed_preview = CustomerTZIngestor.load_any_tz(saved_path)
            st.info(f"✅ Файл **`{uploaded_file.name}`** прочитан. Распознано позиций ТЗ: **{len(parsed_preview)} шт.**")
            with st.expander("Показать распознанные позиции из файла"):
                st.dataframe(pd.DataFrame(parsed_preview), use_container_width=True)
            if st.button("▶️ Запустить подбор и рассчитать ТКП по файлу", type="primary"):
                spec_rows_to_run = parsed_preview
                trigger_calculation = True

    else:
        default_email_text = (
            "Заказчик: ООО «СибМедСтрой» (г. Новокузнецк, просп. Металлургов, 19)\n"
            "Задача: Резервирование электропитания диагностического блока (трёхфазный онлайн-ИБП двойного преобразования, "
            "нагрузка 12 кВт, время автономной работы не менее 30 минут, внешние необслуживаемые АКБ AGM в закрытом напольном шкафу, "
            "карта сетевого мониторинга SNMP и комплект силовых перемычек)."
        )
        raw_text_input = st.text_area(
            "Введите текст заявки клиента:",
            value=default_email_text,
            height=170,
        )

        if st.button("▶️ Разобрать заявку и рассчитать ТКП", type="primary"):
            sec_check = SecurityGuardrails.sanitize_incoming_tz(raw_text_input)
            if not sec_check["is_safe"]:
                for alert_msg in sec_check["alerts"]:
                    st.warning(f"🛡️ {alert_msg}")

            clean_user_text = sec_check["clean_text"]

            if "Облачный ИИ" in llm_mode:
                sup_llm = ConductorSupervisor(groq_api_key=custom_key_input if custom_key_input else None)
                res_llm = sup_llm.handle_natural_language_request(clean_user_text)
                eng_rep = res_llm.get("engineering", {})
                st.session_state["matched_items"] = eng_rep.get("matched_items", [])
                st.session_state["not_found_items"] = eng_rep.get("not_found_items", [])
                st.session_state["accuracy_pct"] = eng_rep.get("accuracy_percent", 100.0)
                st.session_state["kit_header_title"] = eng_rep.get("kit_header_title")
                st.session_state["agent_logs"] = eng_rep.get("engineering_notes", []) or [
                    f"Заявка успешно разобрана ({sup_llm.provider_name}).",
                ]
                _sync_llm_metadata_to_session(res_llm)
                st.rerun()
            elif "Локальная нейросеть" in llm_mode and ollama_model_input:
                sup_llm = ConductorSupervisor(local_ollama_model=ollama_model_input, prefer_local_ollama=True)
                res_llm = sup_llm.handle_natural_language_request(clean_user_text)
                eng_rep = res_llm.get("engineering", {})
                st.session_state["matched_items"] = eng_rep.get("matched_items", [])
                st.session_state["not_found_items"] = eng_rep.get("not_found_items", [])
                st.session_state["accuracy_pct"] = eng_rep.get("accuracy_percent", 100.0)
                st.session_state["kit_header_title"] = eng_rep.get("kit_header_title")
                st.session_state["agent_logs"] = eng_rep.get("engineering_notes", []) or [
                    f"Локальный разбор заявки выполнен ({ollama_model_input}).",
                ]
                _sync_llm_metadata_to_session(res_llm)
                st.rerun()
            else:
                spec_rows_to_run = CustomerTZIngestor.parse_from_text(clean_user_text)
                if not spec_rows_to_run:
                    m_kw = re.search(r"(\d+(?:[.,]\d+)?)\s*квт", clean_user_text.lower())
                    m_hr = re.search(r"(\d+(?:[.,]\d+)?)\s*час", clean_user_text.lower())
                    m_min = re.search(r"(\d+)\s*мин", clean_user_text.lower())
                    if m_kw and (m_hr or m_min):
                        mins = int(float(m_hr.group(1).replace(",", ".")) * 60) if m_hr else int(m_min.group(1))
                        spec_rows_to_run = [{
                            "type": "UPS_SIZING",
                            "load_kw": float(m_kw.group(1).replace(",", ".")),
                            "autonomy_minutes": mins,
                            "battery_chemistry": "LIFEPO4" if "life" in clean_user_text.lower() else ("AGM" if "agm" in clean_user_text.lower() else "AUTO"),
                            "quantity": 1,
                        }]
                trigger_calculation = True

    if trigger_calculation and spec_rows_to_run:
        with st.status("🤖 Выполняется инженерный подбор и расчёт комплекта...", expanded=True) as status_box:
            supervisor = ConductorSupervisor()
            eng_report = supervisor.engineer.process_specification_batch(spec_rows_to_run)
            st.session_state["matched_items"] = eng_report["matched_items"]
            st.session_state["not_found_items"] = eng_report["not_found_items"]
            st.session_state["accuracy_pct"] = eng_report["accuracy_percent"]
            st.session_state["kit_header_title"] = eng_report.get("kit_header_title")
            st.session_state["agent_logs"] = eng_report.get("engineering_notes", []) or [
                f"Выполнено {eng_report['total_matched']}/{eng_report['total_requested']} пунктов (точность {eng_report['accuracy_percent']}%).",
            ]
            status_box.update(
                label=f"✅ Подбор и расчёт завершены! Выполнение ТЗ: {eng_report['accuracy_percent']}%",
                state="complete",
                expanded=False,
            )

    if st.session_state["matched_items"]:
        st.divider()
        st.markdown("### 2. Проверка и редактирование подобранной спецификации")

        edit_rows = []
        for idx, item in enumerate(st.session_state["matched_items"], start=1):
            edit_rows.append({
                "Включить в ТКП": True,
                "№": idx,
                "Артикул": item.get("sku") or "",
                "Код модели": item.get("model_code") or "",
                "Наименование оборудования": item.get("product_name") or "",
                "Бренд": item.get("brand") or "",
                "Метод подбора": item.get("match_level") or "Параметрический подбор",
                "Кол-во, шт.": int(item.get("quantity", 1)),
                "_raw_json": json.dumps(item, ensure_ascii=False, default=str),
            })

        df_editor_in = pd.DataFrame(edit_rows)
        edited_df = st.data_editor(
            df_editor_in.drop(columns=["_raw_json"]),
            use_container_width=True,
            hide_index=True,
            disabled=["№", "Артикул", "Код модели", "Наименование оборудования", "Бренд", "Метод подбора"],
            column_config={
                "Включить в ТКП": st.column_config.CheckboxColumn("В ТКП", default=True),
                "Кол-во, шт.": st.column_config.NumberColumn("Кол-во, шт.", min_value=1, max_value=2000, step=1),
            },
        )

        active_items_for_kp = []
        for idx_r, row in edited_df.iterrows():
            if row["Включить в ТКП"]:
                orig_item = json.loads(df_editor_in.loc[idx_r, "_raw_json"])
                orig_item["quantity"] = int(row["Кол-во, шт."])
                active_items_for_kp.append(orig_item)

        if active_items_for_kp:
            # Применяем страховой валютный запас (+5% по умолчанию) к импортным позициям
            items_with_fx_buffer = apply_fx_buffer_to_items(active_items_for_kp, fx_buffer_slider)

            kp_calc = safe_generate_kp_files(
                items=items_with_fx_buffer,
                client_name=client_name_input,
                client_address=client_addr_input,
                project_name=project_name_input,
                rent_percent=rent_slider,
                delivery_percent=delivery_slider,
                bundled_mode=bundled_mode_chk,
                kit_header_title=st.session_state.get("kit_header_title"),
                include_pnr=include_pnr_chk,
                pnr_separate_rub=pnr_sep_val,
                default_filename="TKP_Dashboard_Current.docx",
            )
            actual_docx_path = kp_calc["file_path"]
            actual_xlsx_path = kp_calc["xlsx_path"]
            total_units_count = sum(x["quantity"] for x in active_items_for_kp)

            st.markdown("### 3. Финансовая сводка и выгрузка документов")

            if IS_DIRECTOR:
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Позиций / Единиц", f"{len(active_items_for_kp)} поз. ({total_units_count} шт.)", f"Точность: {st.session_state['accuracy_pct']}%")
                m2.metric("Себестоимость закупки", f"{kp_calc['total_purchase_cost_rub']:,.2f} ₽".replace(",", " "), f"С учётом курса +{fx_buffer_slider:g}%")
                m3.metric("Валовая / Чистая прибыль", f"+{kp_calc['gross_profit_rub']:,.2f} ₽".replace(",", " "), f"Чистая: +{kp_calc['expected_margin_rub']:,.2f} ₽".replace(",", " "))
                m4.metric("ИТОГО ТКП КЛИЕНТУ", f"{kp_calc['total_offer_with_vat_rub']:,.2f} ₽".replace(",", " "), f"Рент. {rent_slider}% + Дост. {delivery_slider}%")
                m5.metric("В т.ч. НДС (22%)", f"{kp_calc['vat_22_amount_rub']:,.2f} ₽".replace(",", " "), f"Доставка: {delivery_slider}%")

                with st.expander("📊 Детальная управленческая калькуляция (РРЦ • Себестоимость • Цена клиенту)", expanded=True):
                    calc_table_rows = [
                        {
                            "№": idx,
                            "Артикул": pos["sku"],
                            "Наименование": pos["product_name"],
                            "Кол-во": pos["quantity"],
                            "Прайсовая РРЦ (с НДС), ₽": pos["rrc_price_rub"],
                            "Себестоимость закупки, ₽": pos["partner_cost_rub"],
                            f"Цена клиенту (Рент. {rent_slider}%), ₽/шт.": pos["client_unit_price_rub"],
                            "Сумма по строке (с НДС), ₽": pos["total_line_rub"],
                        }
                        for idx, pos in enumerate(kp_calc["positions"], start=1)
                    ]
                    df_fin = pd.DataFrame(calc_table_rows)
                    st.dataframe(
                        df_fin.style.format({
                            "Прайсовая РРЦ (с НДС), ₽": "{:,.2f}",
                            "Себестоимость закупки, ₽": "{:,.2f}",
                            f"Цена клиенту (Рент. {rent_slider}%), ₽/шт.": "{:,.2f}",
                            "Сумма по строке (с НДС), ₽": "{:,.2f}",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                m1, m2, m3 = st.columns(3)
                m1.metric("Позиций / Единиц", f"{len(active_items_for_kp)} поз. ({total_units_count} шт.)", f"Точность: {st.session_state['accuracy_pct']}%")
                m2.metric("ИТОГО ТКП КЛИЕНТУ", f"{kp_calc['total_offer_with_vat_rub']:,.2f} ₽".replace(",", " "), "С учётом доставки и ПНР")
                m3.metric("В т.ч. НДС (22%)", f"{kp_calc['vat_22_amount_rub']:,.2f} ₽".replace(",", " "), "Готово к отправке клиенту")

                with st.expander("📊 Спецификация для клиента (отпускные цены с НДС 22%)", expanded=True):
                    client_table_rows = [
                        {
                            "№": idx,
                            "Артикул": pos["sku"],
                            "Наименование": pos["product_name"],
                            "Кол-во": pos["quantity"],
                            "Цена за ед. (с НДС), ₽": pos["client_unit_price_rub"],
                            "Стоимость (с НДС), ₽": pos["total_line_rub"],
                        }
                        for idx, pos in enumerate(kp_calc["positions"], start=1)
                    ]
                    df_cli = pd.DataFrame(client_table_rows)
                    st.dataframe(
                        df_cli.style.format({
                            "Цена за ед. (с НДС), ₽": "{:,.2f}",
                            "Стоимость (с НДС), ₽": "{:,.2f}",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )

            btn_cols = st.columns([2, 2, 2] if IS_DIRECTOR else [2.5, 2.0])
            with btn_cols[0]:
                with open(actual_docx_path, "rb") as docx_file:
                    st.download_button(
                        label="📥 Скачать официальный бланк ТКП (Word)",
                        data=docx_file.read(),
                        file_name=f"TKP_NIC_INFRA_{datetime.now().strftime('%Y%m%d_%H%M')}.docx",
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        type="primary",
                        use_container_width=True,
                    )
            if IS_DIRECTOR:
                with btn_cols[1]:
                    with open(actual_xlsx_path, "rb") as xlsx_file:
                        st.download_button(
                            label="📊 Скачать внутренний расчёт прибыли (Excel)",
                            data=xlsx_file.read(),
                            file_name=f"Raschet_KP_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                        )
            with btn_cols[-1]:
                if st.button("🧹 Очистить текущий расчёт", use_container_width=True):
                    st.session_state["matched_items"] = []
                    st.session_state["kit_header_title"] = None
                    st.rerun()

            with st.expander("🔍 Инженерное обоснование и проверка совместимости оборудования"):
                for log_line in st.session_state["agent_logs"]:
                    st.code(log_line, language="text")


# ==============================================================================
# ЧАСТЬ 2: ПАНЕЛЬ УПРАВЛЕНИЯ ПРАЙС-ЛИСТАМИ И КУРСАМИ
# ==============================================================================
with tab2:
    st.markdown("### ⚙️ Центр управления прайс-листами и валютными курсами")
    if not IS_DIRECTOR:
        st.info(
            "💼 **Режим Менеджера:** Вы можете синхронизировать курсы валют ЦБ РФ, загружать новые прайсы поставщиков "
            "и отправлять заявки на корректировку цен. Все изменения попадают в буфер согласования и вступают в силу "
            "после утверждения Руководителем."
        )

    sys_col_left, sys_col_right = st.columns([1.0, 1.2])

    with sys_col_left:
        st.markdown("#### 1. Синхронизация и корректировка курсов валют")
        if st.button("🔄 Обновить все курсы по ЦБ РФ", use_container_width=True):
            sync_cbr_exchange_rates(use_live_cbr_api=True)
            st.success("Курсы ЦБ РФ и цены в БД обновлены!")
            st.rerun()

        r_c1, r_c2, r_c3 = st.columns([1.1, 1.3, 1.1])
        target_curr = r_c1.selectbox("Валюта:", ["CNY_VEDA", "USD", "EUR", "CNY"])
        new_curr_val = r_c2.number_input(
            "Курс (₽):",
            min_value=1.0,
            max_value=300.0,
            value=float(rates_dict.get(target_curr, 12.3149)),
            step=0.05,
            format="%.4f",
        )
        with r_c3:
            st.write("")
            st.write("")
            if st.button("💾 Задать", use_container_width=True):
                sync_cbr_exchange_rates(use_live_cbr_api=False, custom_overrides={target_curr: new_curr_val})
                st.success(f"{target_curr} = {new_curr_val:.4f} ₽")
                st.rerun()

    with sys_col_right:
        st.markdown("#### 2. Заявка на изменение цены товара")
        conn = get_db_connection()
        df_skus = pd.read_sql_query(
            "SELECT sku, model_code, product_name, rrc_price_rub_with_vat FROM equipment_items WHERE rrc_price_rub_with_vat IS NOT NULL ORDER BY sku LIMIT 500",
            conn,
        )
        conn.close()

        sku_options = [
            f"{r['sku']} | {r['model_code']} (Текущая РРЦ: {r['rrc_price_rub_with_vat']:,.2f} ₽)"
            for _, r in df_skus.iterrows()
        ]
        selected_sku_label = st.selectbox("Выберите артикул:", sku_options)
        selected_sku_clean = selected_sku_label.split(" | ")[0].strip()

        p_c1, p_c2 = st.columns(2)
        new_price_input = p_c1.number_input("Новая цена РРЦ с НДС (₽):", min_value=10.0, value=48500.0, step=500.0)
        reason_input = p_c2.text_input("Причина изменения:", value="Новый прайс от поставщика")

        if st.button("🛡️ Отправить изменение цены на согласование", type="primary", use_container_width=True):
            res_st = stage_price_update(
                sku=selected_sku_clean,
                new_price=new_price_input,
                reason=f"[{CURRENT_USER['display_name']}] {reason_input}",
            )
            if res_st.get("status") == "STAGED":
                st.success(res_st["message"])
                st.rerun()
            else:
                st.error(res_st.get("message", "Ошибка при отправке заявки"))

    st.divider()

    st.markdown("#### 3. Загрузка нового прайс-листа поставщика")
    up_col1, up_col2 = st.columns([1.3, 1.0])
    with up_col1:
        uploaded_pricelist = st.file_uploader(
            "Выберите файл прайс-листа (Excel или CSV):",
            type=["xlsx", "xls", "csv"],
            key="pricelist_uploader",
        )
    with up_col2:
        sup_c1, sup_c2 = st.columns(2)
        sup_name = sup_c1.text_input("Поставщик:", value="VEDA MC")
        brand_name = sup_c2.text_input("Бренд:", value="VEDA")
        sup_c3, sup_c4 = st.columns(2)
        curr_sel = sup_c3.selectbox("Валюта прайса:", ["CNY_VEDA", "RUB", "USD", "EUR"])
        vat_inc_flag = sup_c4.checkbox("Цены уже с НДС 22%", value=False)
        auto_app_flag = (
            st.checkbox("Сразу применить в основную базу (без ожидания проверки)", value=False)
            if IS_DIRECTOR
            else False
        )

    if uploaded_pricelist is not None:
        if st.button("📥 Загрузить и обработать прайс-лист", type="primary"):
            safe_pl_path = safe_save_uploaded_file(uploaded_pricelist, INBOX_PRICELISTS_DIR)
            ing_res = ingest_new_pricelist_file(
                filename=os.path.basename(safe_pl_path),
                supplier_name=sup_name,
                default_brand=brand_name,
                default_currency=curr_sel,
                vat_included=vat_inc_flag,
                auto_approve=auto_app_flag,
            )
            st.success(ing_res.get("message", "Прайс успешно обработан!"))
            st.rerun()

    st.divider()

    st.markdown("#### 4. Буфер ожидания согласования цен")
    conn = get_db_connection()
    df_pending = pd.read_sql_query(
        """
        SELECT update_id AS 'ID',
               COALESCE(action_type, 'UPDATE_PRICE') AS 'Операция',
               sku AS 'Артикул',
               old_price AS 'Старая цена, ₽',
               new_price AS 'Новая цена, ₽',
               COALESCE(currency_code, 'RUB') AS 'Валюта',
               reason AS 'Основание',
               created_at AS 'Дата создания'
        FROM price_updates_staging
        WHERE status = 'PENDING_APPROVAL'
        ORDER BY update_id DESC
        """,
        conn,
    )
    conn.close()

    if df_pending.empty:
        st.success("✅ Очередь согласования пуста — все изменения проверены.")
    else:
        st.warning(f"🔔 В очереди ожидают подтверждения **{len(df_pending)}** изменений.")
        st.dataframe(df_pending, use_container_width=True, hide_index=True)

        if IS_DIRECTOR:
            act_c1, act_c2, act_c3, act_c4 = st.columns([1.5, 1.5, 1.5, 2.0])
            tx_ids = df_pending["ID"].tolist()
            selected_tx_id = act_c1.selectbox("ID заявки:", tx_ids)
            approver_name = act_c2.text_input("Утверждающий:", value=CURRENT_USER["display_name"])

            with act_c3:
                st.write("")
                if st.button("✅ Подтвердить выбранную", type="primary", use_container_width=True):
                    res_app = approve_staged_change(int(selected_tx_id), approved_by=approver_name)
                    st.success(f"Заявка #{selected_tx_id} ({res_app.get('sku')}) применена в основную базу!")
                    st.rerun()

            with act_c4:
                st.write("")
                b_sub1, b_sub2 = st.columns(2)
                if b_sub1.button("❌ Отклонить", use_container_width=True):
                    reject_staged_change(int(selected_tx_id), rejected_by=approver_name)
                    st.warning(f"Заявка #{selected_tx_id} отклонена.")
                    st.rerun()
                if b_sub2.button("🚀 Подтвердить ВСЕ", use_container_width=True):
                    for tid in tx_ids:
                        approve_staged_change(int(tid), approved_by=approver_name)
                    st.success(f"Все {len(tx_ids)} заявок перенесены в основную базу!")
                    st.rerun()
        else:
            st.caption("🔒 Кнопки утверждения и отклонения доступны только в учётной записи **Руководителя**.")


# ==============================================================================
# ЧАСТЬ 3: ЖУРНАЛ БЕЗОПАСНОСТИ (ТОЛЬКО ДИРЕКТОР), КАТАЛОГ БД И БАЗА ПАСПОРТОВ
# ==============================================================================
with tab3:
    if IS_DIRECTOR:
        sub_t1, sub_t2, sub_t3, sub_t4 = st.tabs([
            "📜 Журнал аудита безопасности",
            "📦 Каталог номенклатуры БД + Добавить в ТКП",
            "🔄 Таблица кросс-замен (Danfoss → VEDA)",
            "📚 База знаний технических паспортов",
        ])

        with sub_t1:
            conn = get_db_connection()
            df_audit = pd.read_sql_query("SELECT * FROM audit_logs ORDER BY log_id DESC LIMIT 100", conn)
            conn.close()
            if df_audit.empty:
                st.info("Записей в журнале аудита пока нет.")
            else:
                st.dataframe(df_audit, use_container_width=True, hide_index=True)
    else:
        sub_t1 = None
        sub_t2, sub_t3, sub_t4 = st.tabs([
            "📦 Каталог номенклатуры БД + Добавить в ТКП",
            "🔄 Таблица кросс-замен (Danfoss → VEDA)",
            "📚 База знаний технических паспортов",
        ])

    with sub_t2:
        f_c1, f_c2, f_c3 = st.columns([2.0, 1.2, 1.2])
        search_q = f_c1.text_input("Поиск по артикулу, модели или названию:", placeholder="Например: HEM060, HE33015XL, S17...")
        class_filter = f_c2.selectbox("Категория:", ["Все категории", "EC_VFD_MCD", "EC_UPS", "EC_BATTERY", "EC_INVERTER", "EC_PLC_HMI", "EC_ACCESSORY"])
        brand_filter = f_c3.selectbox("Бренд:", ["Все бренды", "HIDEN", "VEDA", "Kehua", "ABSolite", "STARK Country", "VEKTOR", "Ventura", "Deye"])

        conn = get_db_connection()
        sql_cat = (
            "SELECT sku AS 'Артикул', brand AS 'Бренд', etim_class AS 'Категория', model_code AS 'Модель', "
            "product_name AS 'Наименование', power_kw AS 'Мощность, кВт', voltage_v AS 'Напряжение, В', "
            "currency_code AS 'Валюта', rrc_price_rub_with_vat AS 'РРЦ с НДС, ₽' "
            "FROM equipment_items WHERE 1=1"
        )
        p_cat = []
        if search_q:
            sql_cat += " AND (sku LIKE ? OR model_code LIKE ? OR product_name LIKE ?)"
            p_cat.extend([f"%{search_q}%", f"%{search_q}%", f"%{search_q}%"])
        if class_filter != "Все категории":
            sql_cat += " AND etim_class = ?"
            p_cat.append(class_filter)
        if brand_filter != "Все бренды":
            sql_cat += " AND brand LIKE ?"
            p_cat.append(f"%{brand_filter}%")
        sql_cat += " ORDER BY brand, sku LIMIT 200"

        df_cat = pd.read_sql_query(sql_cat, conn, params=p_cat)
        conn.close()
        st.dataframe(df_cat, use_container_width=True, hide_index=True)

        if not df_cat.empty:
            add_c1, add_c2, add_c3 = st.columns([2.5, 1.0, 1.5])
            sku_to_add = add_c1.selectbox("Добавить товар в текущее ТКП (Вкладка 1):", df_cat["Артикул"].tolist())
            qty_to_add = add_c2.number_input("Кол-во:", min_value=1, max_value=500, value=1, key="cat_add_qty")
            with add_c3:
                st.write("")
                st.write("")
                if st.button("➕ Добавить позицию в ТКП", use_container_width=True):
                    conn = get_db_connection()
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute("SELECT * FROM equipment_items WHERE sku = ?", (sku_to_add,))
                    r_add = dict(cur.fetchone())
                    conn.close()
                    r_add["quantity"] = int(qty_to_add)
                    r_add["match_level"] = "Добавлено из каталога"
                    st.session_state["matched_items"].append(r_add)
                    st.success(f"Товар {sku_to_add} ({qty_to_add} шт.) добавлен в ТКП!")

    with sub_t3:
        conn = get_db_connection()
        df_cross = pd.read_sql_query("SELECT * FROM equipment_cross_references LIMIT 200", conn)
        conn.close()
        st.dataframe(df_cross, use_container_width=True, hide_index=True)

    with sub_t4:
        rag_q = st.text_input("Задайте инженерный вопрос по паспортам оборудования:", value="Сколько АКБ нужно для ИБП HE3300XL?")
        if st.button("🔍 Найти в базе паспортов"):
            retriever = HybridKnowledgeRetriever()
            hits = retriever.search(rag_q, top_k=3)
            for h in hits:
                with st.expander(f"📄 {h['doc_title']} | Серия: {h['series_code']} (Релевантность: {h['relevance_score']})", expanded=True):
                    st.markdown(h["content_md"])


# ==============================================================================
# ЧАСТЬ 4: АРХИТЕКТУРА СИСТЕМЫ И ВЫГРУЗКА КОДА (ТОЛЬКО ДЛЯ РУКОВОДИТЕЛЯ)
# ==============================================================================
if IS_DIRECTOR and tab4 is not None:
    with tab4:
        st.markdown("### 📖 Внутреннее устройство ИИ-Штаба ООО «НИЦ «ИНФРА»")
        st.caption("Раздел для Руководителя: архитектура модулей, формулы расчёта и выгрузка исходного кода в 1 клик.")

        arch_c1, arch_c2 = st.columns([1.3, 1.0])
        with arch_c1:
            st.markdown("#### 1. Структура модулей и поток данных")
            st.markdown(
                """
1. **`app_dashboard.py`** — Веб-интерфейс с ролевой моделью доступа (`Руководитель` / `Менеджер`), защитой от блокировки открытых файлов Excel/Word и визуальной проверкой сметы.
2. **`multi_agent_hierarchy.py`** — Иерархия ИИ-агентов:
   - **Агент-Дирижер:** разбирает свободный текст заявки через нейросеть в структурированный план закупки.
   - **Агент-Инженер:** выполняет параметрический подбор и запускает инженерный конструктор ИБП и АКБ.
   - **Агент-Менеджер:** рассчитывает отпускные цены по корпоративной формуле рентабельности и формирует бланк ТКП (Word) + внутреннюю смету (Excel).
   - **Агент-Сисадмин:** синхронизирует курсы ЦБ РФ и проводит новые прайс-листы через буфер согласования.
3. **`ups_sizing_engine.py`** — Инженерный конструктор ИБП: расчёт шины постоянного тока, ёмкости АКБ по кривым разряда Пейкерта и подбор совместимых шкафов, карт мониторинга и перемычек одного бренда.
4. **`etl_unified.py` + `infra_equipment.db`** — Единая база номенклатуры с поддержкой мультивалютных прайсов (USD, EUR, CNY, RUB), прайс-уровней и остатков.
                """
            )

        with arch_c2:
            st.markdown("#### 2. Выгрузка проекта и быстрый онлайн-доступ")
            st.info(
                "Чтобы открыть дашборд с телефона или передать ссылку коллеге без публикации на постоянный сервер, "
                "запустите в терминале:\n\n`cloudflared tunnel --url http://localhost:8501`"
            )
            if st.button("📦 Сформировать чистый ZIP-архив проекта (без паролей и временных файлов)", type="primary", use_container_width=True):
                zip_bytes = build_clean_project_zip()
                st.download_button(
                    label="⬇️ Скачать архив проекта (ZIP)",
                    data=zip_bytes,
                    file_name=f"NIC_INFRA_AI_System_{datetime.now().strftime('%Y%m%d')}.zip",
                    mime="application/zip",
                    use_container_width=True,
                )