import json
import sqlite3
from typing import Any, Dict, List, Optional

DB_PATH = "infra_equipment.db"
RATES = {"RUB": 1.0, "USD": 78.69, "CNY_VEDA": 12.6275}


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------------------
# 1. ИНСТРУМЕНТЫ АГЕНТА-ИНЖЕНЕРА (Точный поиск по ТТХ и подбор аналогов)
# ------------------------------------------------------------------------------
def search_equipment(
    etim_class: Optional[str] = None,
    min_power_kw: Optional[float] = None,
    max_power_kw: Optional[float] = None,
    voltage_v: Optional[float] = None,
    ip_rating: Optional[str] = None,
    brand: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Ищет оборудование по техническим параметрам ETIM и ключевым словам (ПЧ, УПП, ИБП, АКБ)."""
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
    SELECT sku, model_code, product_name, etim_class, supplier, brand,
           power_kw, rated_current_a, voltage_v, ip_rating,
           currency_code, vat_included, price_tiers,
           rrc_price_rub_with_vat, specifications
    FROM equipment_items
    WHERE rrc_price_rub_with_vat IS NOT NULL
    """
    params = []

    if etim_class:
        query += " AND etim_class = ?"
        params.append(etim_class)
    if min_power_kw is not None:
        query += " AND power_kw >= ?"
        params.append(min_power_kw)
    if max_power_kw is not None:
        query += " AND power_kw <= ?"
        params.append(max_power_kw)
    if voltage_v is not None:
        query += " AND voltage_v = ?"
        params.append(voltage_v)
    if ip_rating:
        query += " AND ip_rating = ?"
        params.append(ip_rating)
    if brand:
        query += " AND brand LIKE ?"
        params.append(f"%{brand}%")
    if keyword:
        query += " AND (product_name LIKE ? OR model_code LIKE ? OR specifications LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])

    query += " ORDER BY power_kw ASC, rrc_price_rub_with_vat ASC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def find_legacy_replacement(legacy_code: str) -> Dict[str, Any]:
    """Ищет прямую замену устаревшего артикула Danfoss на современный VEDA."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT cr.legacy_brand, cr.legacy_series, cr.legacy_sku, cr.target_veda_sku,
               eq.product_name, eq.power_kw, eq.rrc_price_rub_with_vat
        FROM equipment_cross_references cr
        LEFT JOIN equipment_items eq ON cr.target_veda_sku = eq.sku
        WHERE cr.legacy_sku LIKE ? OR cr.legacy_model_code LIKE ?
        """,
        (f"%{legacy_code}%", f"%{legacy_code}%"),
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        return {"status": "FOUND", "data": dict(row)}
    return {"status": "NOT_FOUND", "message": f"Замена для {legacy_code} не найдена"}


# ------------------------------------------------------------------------------
# 2. ИНСТРУМЕНТЫ АГЕНТА-МЕНЕДЖЕРА (Реальная себестоимость из прайсов + маржа + КП)
# ------------------------------------------------------------------------------
def extract_real_partner_cost_rub(item: Dict[str, Any]) -> float:
    """Извлекает реальную закупочную цену компании из поля price_tiers конкретного поставщика."""
    rrc_rub = float(item.get("rrc_price_rub_with_vat") or 0.0)
    tiers_raw = item.get("price_tiers")
    if not tiers_raw:
        return round(rrc_rub * 0.75, 2)

    tiers = json.loads(tiers_raw) if isinstance(tiers_raw, str) else tiers_raw
    curr = item.get("currency_code", "RUB")
    rate = RATES.get(curr, 1.0)
    vat_mult = 1.0 if item.get("vat_included", True) else 1.22

    # 1. VEDA MC (скидка 50% от прайса у.е. без НДС по листу 'Расчеты')
    if tiers.get("partner_50pct_ue_no_vat"):
        return round(float(tiers["partner_50pct_ue_no_vat"]) * rate * vat_mult, 2)
    # 2. VEKTOR Energy (прямая колонка Партнер в рублях)
    if tiers.get("partner_rub"):
        return round(float(tiers["partner_rub"]), 2)
    # 3. HIDEN (колонка Субдистрибьютор или Партнер)
    if tiers.get("subdistributor"):
        return round(float(tiers["subdistributor"]) * rate * vat_mult, 2)
    if tiers.get("partner"):
        return round(float(tiers["partner"]) * rate * vat_mult, 2)
    # 4. Ventura (оптовая колонка в USD)
    if tiers.get("partner_usd"):
        return round(float(tiers["partner_usd"]) * rate * vat_mult, 2)

    # Если в прайсе только РРЦ (Kehua, ABSolite, STARK) — базовая дилерская скидка 25%
    return round(rrc_rub * 0.75, 2)

def calculate_commercial_totals(
    items: List[Dict[str, Any]],
    markup_over_cost_percent: float = 20.0,
    company_margin_percent: Optional[float] = None,
    delivery_percent: float = 2.5,
) -> Dict[str, Any]:
  """Рассчитывает коммерческое предложение (КП):

  берет реальную закупочную себестоимость из прайса, начисляет наценку (markup)
  и доставку.
  """
  # Поддерживаем оба имени аргумента
  margin_rate = (
      company_margin_percent
      if company_margin_percent is not None
      else markup_over_cost_percent
  )

  total_cost_rub = 0.0
  total_equipment_client_rub = 0.0
  detailed_lines = []

  for item in items:
    qty = int(item.get("quantity", 1))
    rrc_rub = float(item.get("rrc_price_rub_with_vat") or 0.0)
    unit_cost_rub = extract_real_partner_cost_rub(item)

    # Цена продажи клиенту (себестоимость + наценка компании)
    unit_client_rub = round(unit_cost_rub * (1.0 + margin_rate / 100.0), 2)

    line_cost = round(unit_cost_rub * qty, 2)
    line_client = round(unit_client_rub * qty, 2)

    total_cost_rub += line_cost
    total_equipment_client_rub += line_client

    detailed_lines.append({
        "sku": item.get("sku"),
        "product_name": item.get("product_name"),
        "quantity": qty,
        "rrc_price_rub": rrc_rub,
        "cost_price_rub": unit_cost_rub,
        "partner_cost_rub": unit_cost_rub,
        "client_unit_price_rub": unit_client_rub,
        "total_line_rub": line_client,
    })

  delivery_cost_rub = round(
      total_equipment_client_rub * (delivery_percent / 100.0), 2
  )
  final_offer_rub = round(total_equipment_client_rub + delivery_cost_rub, 2)
  net_margin_rub = round(total_equipment_client_rub - total_cost_rub, 2)
  margin_pct = (
      round((net_margin_rub / total_cost_rub) * 100, 1)
      if total_cost_rub > 0
      else 0.0
  )

  return {
      "positions": detailed_lines,
      "total_purchase_cost_rub": round(total_cost_rub, 2),
      "subtotal_equipment_rub": round(total_equipment_client_rub, 2),
      "delivery_rub": delivery_cost_rub,
      "total_offer_with_vat_rub": final_offer_rub,
      "expected_margin_rub": net_margin_rub,
      "margin_percent": margin_pct,
      "margin_percent_over_cost": margin_pct,
  }

# ------------------------------------------------------------------------------
# 3. ИНСТРУМЕНТЫ АГЕНТА-СИСАДМИНА (Staging + Подтверждение изменений человеком)
# ------------------------------------------------------------------------------
def stage_price_update(sku: str, new_price: float, reason: str = "Обновление курса") -> Dict[str, Any]:
    """Записывает изменение цены в буфер (staging) без прямого изменения боевой базы."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT sku, rrc_price_rub_with_vat FROM equipment_items WHERE sku = ?", (sku,))
    item = cursor.fetchone()
    if not item:
        conn.close()
        return {"status": "ERROR", "message": f"Артикул {sku} не найден в базе"}

    old_price = item["rrc_price_rub_with_vat"]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS price_updates_staging (
        update_id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT,
        old_price REAL,
        new_price REAL,
        reason TEXT,
        status TEXT DEFAULT 'PENDING_APPROVAL',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute(
        "INSERT INTO price_updates_staging (sku, old_price, new_price, reason) VALUES (?, ?, ?, ?)",
        (sku, old_price, new_price, reason),
    )
    update_id = cursor.lastrowid
    conn.commit()
    conn.close()

    diff_pct = round(((new_price - old_price) / old_price) * 100, 2)
    return {
        "status": "STAGED",
        "update_id": update_id,
        "sku": sku,
        "old_price": old_price,
        "new_price": new_price,
        "diff_percent": diff_pct,
        "message": f"Транзакция #{update_id}: цена {sku} ({old_price} -> {new_price} руб., {diff_pct:+}%) ожидает подтверждения.",
    }


def approve_staged_update(update_id: int, approved_by: str = "Администратор") -> Dict[str, Any]:
    """Подтверждает транзакцию из буфера (Human-in-the-loop) и переносит цену в основную БД с записью в аудит."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM price_updates_staging WHERE update_id = ? AND status = 'PENDING_APPROVAL'", (update_id,))
    staged = cursor.fetchone()
    if not staged:
        conn.close()
        return {"status": "ERROR", "message": f"Активная транзакция #{update_id} не найдена"}

    # 1. Обновляем цену в боевой таблице
    cursor.execute(
        "UPDATE equipment_items SET rrc_price_rub_with_vat = ? WHERE sku = ?",
        (staged["new_price"], staged["sku"]),
    )
    # 2. Помечаем заявку как выполненную
    cursor.execute("UPDATE price_updates_staging SET status = 'APPROVED' WHERE update_id = ?", (update_id,))
    # 3. Записываем в журнал аудита
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT,
        action TEXT,
        sku TEXT,
        old_val REAL,
        new_val REAL,
        logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute(
        "INSERT INTO audit_logs (actor, action, sku, old_val, new_val) VALUES (?, 'PRICE_UPDATE', ?, ?, ?)",
        (approved_by, staged["sku"], staged["old_price"], staged["new_price"]),
    )
    conn.commit()
    conn.close()

    return {
        "status": "APPROVED_AND_APPLIED",
        "update_id": update_id,
        "sku": staged["sku"],
        "applied_price_rub": staged["new_price"],
        "approved_by": approved_by,
    }
