import os
import re
import ssl
import json
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional
import pandas as pd
import numpy as np

try:
    from config import DB_PATH, INBOX_PRICELISTS_DIR as INBOX_DIR
except ImportError:
    DB_PATH = "infra_equipment.db"
    INBOX_DIR = "inbox_pricelists"

try:
    from etl_unified import UniversalSupplierETL
except ImportError:
    UniversalSupplierETL = None


def _ru_stem(word: str) -> str:
    """Обрезает окончания русских и английских слов для морфологически устойчивого поиска."""
    w = word.strip().lower()
    if len(w) <= 4:
        return w
    w = re.sub(
        r"(ями|ами|ого|его|ому|ему|ыми|ими|ая|яя|ое|ее|ые|ие|ой|ей|ый|ий|ов|ев|ам|ям|ах|ях|ью|ии|ия|ь|и|ы|а|я|у|ю|е|о)$",
        "",
        w,
    )
    return w if len(w) >= 3 else word.strip().lower()[:4]


def _ru_match_text(haystack: str, needle_query: str) -> int:
    """Пользовательская SQL-функция для SQLite: регистронезависимый поиск по кириллице с учётом корней слов."""
    if not haystack or not needle_query:
        return 0
    h_low = str(haystack).lower()
    words = [w for w in re.split(r"[\s,;+\-/]+", str(needle_query).lower()) if len(w) >= 2]
    if not words:
        return 1
    for w in words:
        if _ru_stem(w) not in h_low:
            return 0
    return 1


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.create_function("RU_MATCH", 2, _ru_match_text)
    return conn


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def _ensure_columns(cur, table_name: str, required_columns: Dict[str, str]):
    """Автоматическая миграция: добавляет новые колонки, если таблица осталась от старой версии."""
    if not _table_exists(cur, table_name):
        return
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col_name, col_def in required_columns.items():
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
            print(f"   [Миграция БД]: Добавлена колонка '{col_name}' в таблицу '{table_name}'")


# ==============================================================================
# 1. ИНИЦИАЛИЗАЦИЯ И МИГРАЦИЯ ТАБЛИЦ КУРСОВ ВАЛЮТ, STAGING И АУДИТА
# ==============================================================================
def init_admin_tables():
    """Создаёт и мигрирует таблицы equipment_items, exchange_rates, staging и audit_logs."""
    os.makedirs(INBOX_DIR, exist_ok=True)
    conn = get_db_connection()
    cur = conn.cursor()

    # 0. Основная таблица каталога оборудования (если создаётся с нуля)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equipment_items (
        sku TEXT PRIMARY KEY,
        supplier TEXT,
        brand TEXT,
        etim_class TEXT,
        model_code TEXT,
        product_name TEXT,
        base_price REAL,
        currency_code TEXT DEFAULT 'RUB',
        vat_included INTEGER DEFAULT 1,
        price_on_request INTEGER DEFAULT 0,
        lifecycle_status TEXT,
        price_tiers TEXT,
        power_kw REAL,
        rated_current_a REAL,
        voltage_v REAL,
        ip_rating TEXT,
        weight_kg REAL,
        dimensions_mm TEXT,
        specifications TEXT,
        rrc_price_rub_with_vat REAL
    )
    """)
    _ensure_columns(cur, "equipment_items", {
        "price_on_request": "INTEGER DEFAULT 0",
        "lifecycle_status": "TEXT",
        "price_tiers": "TEXT",
        "rrc_price_rub_with_vat": "REAL",
    })

    # Таблицы кросс-замен и комплектов BOM
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equipment_cross_references (
        legacy_brand TEXT,
        legacy_series TEXT,
        legacy_sku TEXT,
        legacy_model_code TEXT,
        target_veda_sku TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equipment_kits_bom (
        supplier TEXT,
        kit_sheet TEXT,
        kit_name TEXT,
        component_model TEXT,
        quantity INTEGER,
        rrc_usd REAL,
        discount REAL,
        partner_usd REAL
    )
    """)

    # 1. Таблица актуальных курсов валют
    cur.execute("""
    CREATE TABLE IF NOT EXISTS exchange_rates (
        currency_code TEXT PRIMARY KEY,
        currency_name TEXT NOT NULL,
        cbr_rate REAL NOT NULL,
        company_coefficient REAL DEFAULT 1.0,
        effective_rate_to_rub REAL NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    default_rates = [
        ("RUB", "Российский рубль", 1.0, 1.0, 1.0),
        ("USD", "Доллар США (ЦБ РФ)", 82.6617, 1.0, 82.6617),
        ("EUR", "Евро (ЦБ РФ)", 92.6968, 1.0, 92.6968),
        ("CNY", "Китайский юань (ЦБ РФ)", 12.3907, 1.0, 12.3907),
        ("CNY_VEDA", "Внутренний у.е. VEDA MC (привязан к CNY)", 12.3907, 1.0, 12.3907),
    ]
    for code, name, cbr, coeff, eff in default_rates:
        cur.execute("""
        INSERT OR IGNORE INTO exchange_rates 
        (currency_code, currency_name, cbr_rate, company_coefficient, effective_rate_to_rub)
        VALUES (?, ?, ?, ?, ?)
        """, (code, name, cbr, coeff, eff))

    # 2. Буферная таблица Staging (для цен и новых товаров, включая позиции 'по запросу')
    cur.execute("""
    CREATE TABLE IF NOT EXISTS price_updates_staging (
        update_id INTEGER PRIMARY KEY AUTOINCREMENT,
        action_type TEXT DEFAULT 'UPDATE_PRICE',
        sku TEXT NOT NULL,
        old_price REAL,
        new_price REAL,
        currency_code TEXT DEFAULT 'RUB',
        item_payload_json TEXT,
        reason TEXT,
        status TEXT DEFAULT 'PENDING_APPROVAL',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    _ensure_columns(cur, "price_updates_staging", {
        "action_type": "TEXT DEFAULT 'UPDATE_PRICE'",
        "currency_code": "TEXT DEFAULT 'RUB'",
        "item_payload_json": "TEXT",
    })

    # 3. Таблица журнала безопасности и аудита
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT,
        action TEXT,
        sku TEXT,
        old_val REAL,
        new_val REAL,
        details TEXT,
        logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    _ensure_columns(cur, "audit_logs", {
        "details": "TEXT",
    })

    conn.commit()
    conn.close()


def _fetch_cbr_rates_live() -> Dict[str, float]:
    """
    Загружает актуальные курсы USD, EUR, CNY:
      1) Официальный XML-шлюз ЦБ РФ (напрямую в обход системного VPN/прокси);
      2) Зеркало JSON ЦБ РФ (cbr-xml-daily.ru);
      3) Международный API open.er-api.com (если cbr.ru заблокирован зарубежным VPN).
    """
    ssl_ctx = ssl._create_unverified_context()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/xml,text/xml,application/json,*/*",
    }
    today_req = datetime.now().strftime("%d/%m/%Y")

    no_proxy_handler = urllib.request.ProxyHandler({})
    https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
    direct_opener = urllib.request.build_opener(no_proxy_handler, https_handler)

    # 1. Официальный XML-шлюз Банка России с параметром сегодняшней даты
    for cbr_url in [
        f"https://www.cbr.ru/scripts/XML_daily.asp?date_req={today_req}",
        f"http://www.cbr.ru/scripts/XML_daily.asp?date_req={today_req}",
        "https://www.cbr.ru/scripts/XML_daily.asp",
    ]:
        for opener_fn in (direct_opener.open, urllib.request.urlopen):
            try:
                req = urllib.request.Request(cbr_url, headers=headers)
                with opener_fn(req, timeout=5) as resp:
                    xml_bytes = resp.read()
                    root = ET.fromstring(xml_bytes)
                    rates = {}
                    for valute in root.findall("Valute"):
                        char_code = valute.find("CharCode").text
                        if char_code in ("USD", "EUR", "CNY"):
                            nominal = float(valute.find("Nominal").text.replace(",", "."))
                            value = float(valute.find("Value").text.replace(",", "."))
                            rates[char_code] = round(value / nominal, 4)
                    if rates:
                        if "CNY" in rates:
                            rates["CNY_VEDA"] = rates["CNY"]
                        return rates
            except Exception:
                continue

    # 2. Резервное JSON-зеркало ЦБ РФ
    for mirror_url in [
        "https://www.cbr-xml-daily.ru/daily_json.js",
        "https://www.cbr-xml-daily.ru/latest.js",
    ]:
        try:
            req = urllib.request.Request(mirror_url, headers=headers)
            with urllib.request.urlopen(req, timeout=5, context=ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                rates = {}
                if "Valute" in data:
                    valute = data["Valute"]
                    for code in ("USD", "EUR", "CNY"):
                        if code in valute:
                            rates[code] = round(float(valute[code]["Value"]) / float(valute[code]["Nominal"]), 4)
                elif "rates" in data:
                    r_map = data["rates"]
                    for code in ("USD", "EUR", "CNY"):
                        if code in r_map and float(r_map[code]) > 0:
                            rates[code] = round(1.0 / float(r_map[code]), 4)
                if rates:
                    if "CNY" in rates:
                        rates["CNY_VEDA"] = rates["CNY"]
                    return rates
        except Exception:
            continue

    # 3. Международный резервный API (работает при включённом зарубежном VPN)
    try:
        req = urllib.request.Request("https://open.er-api.com/v6/latest/RUB", headers=headers)
        with urllib.request.urlopen(req, timeout=5, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            r_map = data.get("rates", {})
            rates = {
                c: round(1.0 / float(r_map[c]), 4)
                for c in ("USD", "EUR", "CNY")
                if c in r_map and float(r_map[c]) > 0
            }
            if rates:
                if "CNY" in rates:
                    rates["CNY_VEDA"] = rates["CNY"]
                return rates
    except Exception:
        pass

    return {}


def sync_cbr_exchange_rates(
    use_live_cbr_api: bool = True,
    custom_overrides: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Синхронизирует таблицу exchange_rates с ЦБ РФ и пересчитывает рублёвые цены в БД.
    """
    init_admin_tables()
    conn = get_db_connection()
    cur = conn.cursor()

    updated_currencies = {}
    if use_live_cbr_api:
        updated_currencies = _fetch_cbr_rates_live()
        if not updated_currencies:
            print("   [i] Внешний шлюз ЦБ недоступен, используются текущие курсы из таблицы exchange_rates.")

    if custom_overrides:
        for k, v in custom_overrides.items():
            updated_currencies[k.upper()] = float(v)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for code, cbr_val in updated_currencies.items():
        cur.execute("SELECT company_coefficient FROM exchange_rates WHERE currency_code = ?", (code,))
        row = cur.fetchone()
        coeff = float(row["company_coefficient"]) if row else 1.0
        eff_rate = round(cbr_val * coeff, 4)
        cur.execute("""
        UPDATE exchange_rates
        SET cbr_rate = ?, effective_rate_to_rub = ?, updated_at = ?
        WHERE currency_code = ?
        """, (cbr_val, eff_rate, now_str, code))

    # Мгновенный пересчёт витринной рублёвой колонки rrc_price_rub_with_vat по актуальным курсам
    cur.execute("""
    UPDATE equipment_items
    SET rrc_price_rub_with_vat = ROUND(
        base_price * COALESCE(
            (SELECT effective_rate_to_rub FROM exchange_rates WHERE exchange_rates.currency_code = equipment_items.currency_code),
            1.0
        ) * (CASE WHEN vat_included = 1 THEN 1.0 ELSE 1.22 END),
        2
    )
    WHERE base_price IS NOT NULL
    """)

    conn.commit()
    cur.execute("SELECT currency_code, cbr_rate, effective_rate_to_rub, updated_at FROM exchange_rates")
    current_table = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "status": "RATES_SYNCED",
        "exchange_rates": current_table,
        "message": "Курсы валют в таблице exchange_rates и рублёвые цены в БД успешно актуализированы.",
    }


def get_current_rates_dict() -> Dict[str, float]:
    """Возвращает словарь актуальных курсов из таблицы exchange_rates."""
    init_admin_tables()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT currency_code, effective_rate_to_rub FROM exchange_rates")
    rates = {r["currency_code"]: float(r["effective_rate_to_rub"]) for r in cur.fetchall()}
    conn.close()
    return rates


# ==============================================================================
# 2. УНИВЕРСАЛЬНЫЙ ИМПОРТ НОВЫХ И СУЩЕСТВУЮЩИХ ПРАЙС-ЛИСТОВ (ЧЕРЕЗ UNIVERSAL ETL)
# ==============================================================================
def ingest_new_pricelist_file(
    filename: str,
    supplier_name: str = "Новый поставщик",
    default_brand: str = "GENERIC",
    default_currency: str = "RUB",
    vat_included: bool = True,
    auto_approve: bool = False,
) -> Dict[str, Any]:
    """
    Универсальный парсер любого прайс-листа (Excel .xlsx/.xls или .csv) из папки inbox_pricelists/:
      - Использует UniversalSupplierETL v2.0 (многолистовой разбор, автопоиск шапок, генерация SKU,
        учёт позиций 'По запросу' и 'Снято с производства / складские остатки');
      - Сверяет с текущей БД: новые товары помещает в Staging как INSERT_NEW_ITEM,
        изменившиеся цены — как UPDATE_PRICE;
      - Попутно обновляет таблицы кросс-замен (Danfoss -> VEDA) и комплектов (BOM).
    """
    init_admin_tables()
    filepath = os.path.join(INBOX_DIR, filename) if not os.path.exists(filename) else filename
    if not os.path.exists(filepath):
        return {"status": "ERROR", "message": f"Файл '{filepath}' не найден в папке '{INBOX_DIR}'."}

    temp_xlsx_path = None
    parse_target_path = filepath
    if filepath.lower().endswith(".csv"):
        try:
            df_csv = pd.read_csv(filepath, sep=None, engine="python")
        except Exception:
            df_csv = pd.read_csv(filepath)
        temp_xlsx_path = filepath + ".tmp_etl.xlsx"
        df_csv.to_excel(temp_xlsx_path, index=False)
        parse_target_path = temp_xlsx_path

    try:
        if UniversalSupplierETL is None:
            return {"status": "ERROR", "message": "Модуль UniversalSupplierETL не найден в etl_unified.py."}

        etl = UniversalSupplierETL(
            supplier_name=supplier_name,
            default_brand=default_brand,
            default_currency=default_currency,
            default_vat_included=vat_included,
        )
        parsed_items, parsed_cross_refs, parsed_kits = etl.parse_excel_workbook(parse_target_path)
    finally:
        if temp_xlsx_path and os.path.exists(temp_xlsx_path):
            try:
                os.remove(temp_xlsx_path)
            except Exception:
                pass

    if not parsed_items and not parsed_cross_refs and not parsed_kits:
        return {
            "status": "EMPTY",
            "file": filename,
            "message": f"В файле '{filename}' не обнаружено товарных таблиц с ценами.",
        }

    dedup_items: Dict[str, Dict[str, Any]] = {}
    for it in parsed_items:
        if it["sku"] not in dedup_items:
            dedup_items[it["sku"]] = it

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT sku, base_price, rrc_price_rub_with_vat, price_on_request FROM equipment_items")
    existing_skus = {r["sku"]: dict(r) for r in cur.fetchall()}
    rates = get_current_rates_dict()

    new_items_staged = 0
    price_updates_staged = 0
    on_request_count = 0
    staged_ids: List[int] = []

    for sku_str, item in dedup_items.items():
        curr_code = str(item.get("currency_code") or default_currency).upper()
        rate = rates.get(curr_code, 1.0)
        item_vat_inc = bool(item.get("vat_included", vat_included))
        vat_mult = 1.0 if item_vat_inc else 1.22

        base_p = item.get("base_price")
        is_on_req = bool(item.get("price_on_request", False))
        if is_on_req:
            on_request_count += 1

        rrc_rub = round(float(base_p) * rate * vat_mult, 2) if base_p is not None else None
        item["rrc_price_rub_with_vat"] = rrc_rub

        if sku_str in existing_skus:
            old_rrc = existing_skus[sku_str]["rrc_price_rub_with_vat"]
            if rrc_rub is not None and (old_rrc is None or abs(rrc_rub - float(old_rrc)) > 0.01):
                cur.execute("""
                INSERT INTO price_updates_staging 
                (action_type, sku, old_price, new_price, currency_code, item_payload_json, reason)
                VALUES ('UPDATE_PRICE', ?, ?, ?, ?, ?, ?)
                """, (
                    sku_str,
                    old_rrc,
                    rrc_rub,
                    curr_code,
                    json.dumps(item, ensure_ascii=False),
                    f"Обновление цены из файла {filename}",
                ))
                staged_ids.append(cur.lastrowid)
                price_updates_staged += 1
        else:
            cur.execute("""
            INSERT INTO price_updates_staging 
            (action_type, sku, old_price, new_price, currency_code, item_payload_json, reason)
            VALUES ('INSERT_NEW_ITEM', ?, NULL, ?, ?, ?, ?)
            """, (
                sku_str,
                rrc_rub,
                curr_code,
                json.dumps(item, ensure_ascii=False),
                f"Новый товар из прайса {filename} ({supplier_name})",
            ))
            staged_ids.append(cur.lastrowid)
            new_items_staged += 1

    if parsed_cross_refs:
        for cr in parsed_cross_refs:
            cur.execute("""
            INSERT INTO equipment_cross_references
            (legacy_brand, legacy_series, legacy_sku, legacy_model_code, target_veda_sku)
            VALUES (?, ?, ?, ?, ?)
            """, (
                cr.get("legacy_brand"),
                cr.get("legacy_series"),
                cr.get("legacy_sku"),
                cr.get("legacy_model_code"),
                cr.get("target_veda_sku"),
            ))

    if parsed_kits:
        for kt in parsed_kits:
            cur.execute("""
            INSERT INTO equipment_kits_bom
            (supplier, kit_sheet, kit_name, component_model, quantity, rrc_usd, discount, partner_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                kt.get("supplier"),
                kt.get("kit_sheet"),
                kt.get("kit_name"),
                kt.get("component_model"),
                kt.get("quantity"),
                kt.get("rrc_usd"),
                kt.get("discount"),
                kt.get("partner_usd"),
            ))

    conn.commit()
    conn.close()

    if auto_approve and staged_ids:
        for uid in staged_ids:
            approve_staged_change(uid, approved_by="Авто-импорт Сисадмина (Universal ETL)")

    return {
        "status": "INGESTED_TO_STAGING" if not auto_approve else "INGESTED_AND_APPLIED",
        "file": filename,
        "total_parsed_unique": len(dedup_items),
        "new_items_count": new_items_staged,
        "price_updates_count": price_updates_staged,
        "on_request_count": on_request_count,
        "cross_refs_added": len(parsed_cross_refs),
        "kits_rows_added": len(parsed_kits),
        "staged_transaction_ids": staged_ids,
        "message": (
            f"Файл '{filename}' обработан универсальным ETL: всего распознано {len(dedup_items)} поз. "
            f"(новых товаров — {new_items_staged} шт., обновлений цен — {price_updates_staged} шт., "
            f"со статусом 'по запросу' — {on_request_count} шт.)."
        ),
    }


def approve_staged_change(update_id: int, approved_by: str = "Администратор") -> Dict[str, Any]:
    """
    Применяет транзакцию из буфера Staging в основную БД equipment_items:
      - INSERT_NEW_ITEM: добавляет новую карточку товара со всеми полями (включая lifecycle_status и price_on_request);
      - UPDATE_PRICE: обновляет цену и ценовые уровни существующей позиции.
    """
    init_admin_tables()
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM price_updates_staging WHERE update_id = ? AND status = 'PENDING_APPROVAL'", (update_id,))
    staged = cur.fetchone()
    if not staged:
        conn.close()
        return {"status": "ERROR", "message": f"Активная транзакция #{update_id} не найдена."}

    action_type = staged["action_type"] or "UPDATE_PRICE"
    sku = staged["sku"]

    cur.execute("PRAGMA table_info(equipment_items)")
    valid_db_cols = {row[1] for row in cur.fetchall()}

    if action_type == "INSERT_NEW_ITEM" and staged["item_payload_json"]:
        raw_item = json.loads(staged["item_payload_json"])
        filtered_item = {k: v for k, v in raw_item.items() if k in valid_db_cols}
        cols = list(filtered_item.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        cur.execute(
            f"INSERT OR REPLACE INTO equipment_items ({col_names}) VALUES ({placeholders})",
            tuple(filtered_item.values()),
        )
    else:
        if staged["item_payload_json"]:
            raw_item = json.loads(staged["item_payload_json"])
            cur.execute(
                """
                UPDATE equipment_items
                SET base_price = COALESCE(?, base_price),
                    price_tiers = COALESCE(?, price_tiers),
                    price_on_request = COALESCE(?, price_on_request),
                    lifecycle_status = COALESCE(?, lifecycle_status),
                    rrc_price_rub_with_vat = ?
                WHERE sku = ?
                """,
                (
                    raw_item.get("base_price"),
                    raw_item.get("price_tiers"),
                    raw_item.get("price_on_request"),
                    raw_item.get("lifecycle_status"),
                    staged["new_price"],
                    sku,
                ),
            )
        else:
            cur.execute(
                "UPDATE equipment_items SET rrc_price_rub_with_vat = ? WHERE sku = ?",
                (staged["new_price"], sku),
            )

    cur.execute("UPDATE price_updates_staging SET status = 'APPROVED' WHERE update_id = ?", (update_id,))
    cur.execute("""
    INSERT INTO audit_logs (actor, action, sku, old_val, new_val, details)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (approved_by, action_type, sku, staged["old_price"], staged["new_price"], staged["reason"]))

    conn.commit()
    conn.close()
    return {
        "status": "APPROVED",
        "update_id": update_id,
        "action_type": action_type,
        "sku": sku,
        "new_price_rub": staged["new_price"],
    }


if __name__ == "__main__":
    try:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass

    print("1. Синхронизация таблицы exchange_rates с ЦБ РФ...")
    res_rates = sync_cbr_exchange_rates(use_live_cbr_api=True)
    for r in res_rates["exchange_rates"]:
        print(f"   • {r['currency_code']}: курс ЦБ = {r['cbr_rate']} -> рабочий курс = {r['effective_rate_to_rub']} ₽")

    print("\n2. Создание тестового прайса с новыми позициями (вкл. 'по запросу' и 'остатки')...")
    sample_new_df = pd.DataFrame([
        {
            "Артикул": "VF500-NEW-90KW",
            "Модель": "VF-500-P90K-0180-T4",
            "Наименование": "Преобразователь частоты VEDA VF-500 90 кВт (Новинка 2026)",
            "Мощность, кВт": 90.0,
            "Ток, А": 180.0,
            "Напряжение, В": 400,
            "Цена РРЦ, у.е. без НДС": 34500.0,
            "Примечание": "",
        },
        {
            "Артикул": "NEW-CAB-S24",
            "Модель": "Шкаф аккумуляторный S24 (на 64 АКБ)",
            "Наименование": "Шкаф аккумуляторный напольный S24",
            "Мощность, кВт": None,
            "Ток, А": None,
            "Напряжение, В": None,
            "Цена РРЦ, у.е. без НДС": "по запросу",
            "Примечание": "Снято с производства, складские остатки",
        },
    ])
    test_file = os.path.join(INBOX_DIR, "new_supplier_delivery_2026.xlsx")
    sample_new_df.to_excel(test_file, index=False)

    print("3. Парсинг нового прайса через UniversalSupplierETL и запись в БД...")
    ingest_res = ingest_new_pricelist_file(
        filename="new_supplier_delivery_2026.xlsx",
        supplier_name="VEDA MC",
        default_brand="VEDA",
        default_currency="CNY_VEDA",
        vat_included=False,
        auto_approve=True,
    )
    print(json.dumps(ingest_res, ensure_ascii=False, indent=2))