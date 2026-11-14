import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from config import DB_PATH
from rag_knowledge_base import seed_infra_engineering_datasheets


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_dc_battery_count(item: Dict[str, Any], power_kw: float) -> Tuple[int, float]:
    """Извлекает из описания ИБП напряжение шины DC и требуемое число 12В АКБ."""
    text = f"{item.get('product_name', '')} {item.get('specifications', '')} {item.get('model_code', '')}"

    # Серия HIDEN HE310xx (3/1) работает на 16 АКБ (192 В DC), серия HE330xx (3/3) — на 40 АКБ (±240 В DC)
    if re.search(r'he310\d+', text, re.IGNORECASE):
        return 16, 192.0
    if re.search(r'he330\d+', text, re.IGNORECASE):
        return 40, 480.0

    m_cnt = re.search(r'\((\d{1,3})(?:/\d{1,3})*\s*акб', text, re.IGNORECASE)
    if m_cnt:
        cnt = int(m_cnt.group(1))
        if cnt >= 2:
            return cnt, cnt * 12.0

    m_pm = re.search(r'[±]\s*(\d{2,3})\s*v', text, re.IGNORECASE)
    if m_pm:
        total_v = float(m_pm.group(1)) * 2.0
        return max(2, int(round(total_v / 12.0))), total_v

    m_dc = re.search(r'(?:dc|акб)\s*[=:]?\s*(\d{2,3})\s*v', text, re.IGNORECASE)
    if m_dc:
        dc_v = float(m_dc.group(1))
        if dc_v >= 24:
            return max(2, int(round(dc_v / 12.0))), dc_v

    if power_kw <= 1.5:
        return 3, 36.0
    elif power_kw <= 3.0:
        return 6, 72.0
    elif power_kw <= 10.0:
        return 16, 192.0
    else:
        return 40, 480.0


def _get_agm_discharge_efficiency(autonomy_minutes: int) -> float:
    curve = [
        (5, 0.22), (10, 0.32), (15, 0.40), (30, 0.52), (60, 0.63),
        (120, 0.74), (180, 0.81), (300, 0.88), (600, 0.95), (1200, 1.00),
    ]
    if autonomy_minutes <= curve[0][0]:
        return curve[0][1]
    if autonomy_minutes >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t1, k1 = curve[i]
        t2, k2 = curve[i + 1]
        if t1 <= autonomy_minutes <= t2:
            return k1 + ((autonomy_minutes - t1) / (t2 - t1)) * (k2 - k1)
    return 0.55


def _find_ups_for_brand(brand: str, min_kw: float, phases: str) -> Optional[Dict[str, Any]]:
    """Ищет подходящий ИБП строго у конкретного производителя (с учётом фазности 3/3 или 1/1)."""
    conn = _get_conn()
    cur = conn.cursor()

    sql = """
        SELECT * FROM equipment_items
        WHERE (LOWER(brand) LIKE LOWER(?) OR LOWER(product_name) LIKE LOWER(?))
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND (etim_class = 'EC_UPS' OR (etim_class = 'EC_ACCESSORY' AND (product_name LIKE 'ИБП%' OR product_name LIKE 'ибп%')))
          AND product_name NOT LIKE 'Опция%'
          AND product_name NOT LIKE '%плата%'
          AND product_name NOT LIKE '%байпас%'
          AND product_name NOT LIKE '%подставка%'
          AND product_name NOT LIKE '%встроенн% акб%'
          AND product_name NOT LIKE '%встроенные акб%'
          AND power_kw >= ?
    """
    if phases == "3/3":
        # Исключаем 3/1 модели (например, HE31015XL) при запросе трёхфазного выхода 3/3
        sql += " AND model_code NOT LIKE 'HE310%' AND product_name NOT LIKE '%3/1%'"
        sql += " AND (product_name LIKE '%3/3%' OR specifications LIKE '%3:3%' OR model_code LIKE 'HE330%' OR power_kw >= 10.0)"
    else:
        sql += " AND product_name NOT LIKE '%3/3%' AND model_code NOT LIKE 'HE330%'"

    sql += " ORDER BY power_kw ASC, COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1"
    cur.execute(sql, (f"%{brand}%", f"%{brand}%", min_kw))
    row = cur.fetchone()
    conn.close()
    if row:
        d = dict(row)
        if d.get("rrc_price_rub_with_vat") is None and d.get("base_price") is not None:
            d["rrc_price_rub_with_vat"] = float(d["base_price"])
        return d
    return None


def _find_battery_for_brand(required_ah: float, preferred_brand: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()

    sql = """
        SELECT * FROM equipment_items
        WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND etim_class = 'EC_BATTERY'
          AND voltage_v = 12.0
          AND rated_current_a >= ?
          AND product_name NOT LIKE '%LiFePO4%'
          AND product_name NOT LIKE '%lifepo4%'
          AND product_name NOT LIKE '%LFP%'
          AND product_name NOT LIKE '%ИБП%'
    """
    params: List[Any] = [required_ah * 0.95]
    if preferred_brand:
        sql += " AND LOWER(brand) LIKE LOWER(?)"
        params.append(f"%{preferred_brand}%")

    sql += " ORDER BY rated_current_a ASC, COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1"
    cur.execute(sql, params)
    row = cur.fetchone()
    conn.close()
    if row:
        d = dict(row)
        if d.get("rrc_price_rub_with_vat") is None and d.get("base_price") is not None:
            d["rrc_price_rub_with_vat"] = float(d["base_price"])
        return d
    return None


def _find_cabinet_for_brand(brand: Optional[str], total_bat_count: int, bat_ah: float) -> Tuple[Optional[Dict[str, Any]], int]:
    """Подбирает совместимый батарейный шкаф с проверкой допустимого типоразмера (ёмкости) АКБ."""
    conn = _get_conn()
    cur = conn.cursor()

    sql = """
        SELECT * FROM equipment_items
        WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(rrc_price_rub_with_vat, base_price) >= 10000
          AND COALESCE(price_on_request, 0) = 0
          AND etim_class = 'EC_ACCESSORY'
          AND (
              product_name LIKE '%Шкаф%' OR product_name LIKE '%шкаф%'
              OR product_name LIKE '%Стеллаж%' OR product_name LIKE '%стеллаж%'
              OR product_name LIKE '%Батарейный кабинет%'
          )
          AND product_name NOT LIKE '%Уголок%'
          AND product_name NOT LIKE '%уголков%'
          AND product_name NOT LIKE '%направляющ%'
          AND product_name NOT LIKE '%креплен%'
          AND product_name NOT LIKE '%под литиевые%'
          AND product_name NOT LIKE '%19"%'
    """
    params: List[Any] = []
    if brand:
        sql += " AND (LOWER(brand) LIKE LOWER(?) OR LOWER(product_name) LIKE LOWER(?))"
        params.extend([f"%{brand}%", f"%{brand}%"])

    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    best_cab, best_qty, best_cost = None, 0, float("inf")
    for cab in rows:
        if cab.get("rrc_price_rub_with_vat") is None and cab.get("base_price") is not None:
            cab["rrc_price_rub_with_vat"] = float(cab["base_price"])

        txt = f"{cab.get('model_code', '')} {cab.get('product_name', '')}".lower()

        # Фильтр физической совместимости по габариту АКБ: корпуса 2U/3U под 7-9 Ач не подходят для АКБ > 12 Ач
        if bat_ah > 12.0 and any(tag in txt for tag in ["7 / 9 ач", "7/9 ач", "3u-", "2u-"]):
            continue

        cap = None
        m_grid = re.search(r'\b([1-9]|10)\s*[xх*]\s*([2-9]|1\d|20)\b', txt)
        if m_grid:
            cap = int(m_grid.group(1)) * int(m_grid.group(2))
        elif 's17' in txt:
            cap = 40
        elif 's16' in txt:
            cap = 32
        elif 's12' in txt or 's10' in txt:
            cap = 16
        else:
            m_pcs = re.search(r'(?:до|на)\s*(\d+)\s*(?:акб|шт)', txt)
            if m_pcs:
                cap = int(m_pcs.group(1))

        if cap and cap >= 30 and bat_ah > 45:
            cap = cap // 2

        if cap and cap >= 4:
            needed = max(1, math.ceil(total_bat_count / cap))
            cost = float(cab["rrc_price_rub_with_vat"]) * needed
            if cost < best_cost:
                best_cost, best_cab, best_qty = cost, cab, needed

    return best_cab, best_qty


def _find_snmp_for_brand(brand: str, is_three_phase: bool) -> Optional[Dict[str, Any]]:
    """Ищет именно SNMP-карту бренда, строго исключая датчики температуры/влажности (ДОС)."""
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM equipment_items
        WHERE (LOWER(brand) LIKE LOWER(?) OR LOWER(product_name) LIKE LOWER(?))
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND (product_name LIKE '%SNMP%' OR product_name LIKE '%snmp%')
          AND (product_name LIKE '%карт%' OR product_name LIKE '%Карт%' OR product_name LIKE '%адаптер%' OR product_name LIKE '%card%')
          AND product_name NOT LIKE '%датчик%'
          AND product_name NOT LIKE '%Датчик%'
          AND product_name NOT LIKE '%ДОС%'
          AND product_name NOT LIKE '%температур%'
          AND product_name NOT LIKE '%бокс%'
        ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC
    """, (f"%{brand}%", f"%{brand}%"))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not rows:
        return None
    chosen = rows[0]
    if is_three_phase:
        for r in rows:
            if any(k in r["product_name"].lower() for k in ["3-ф", "da-807", "she8m", "flex33"]):
                chosen = r
                break
    if chosen.get("rrc_price_rub_with_vat") is None and chosen.get("base_price") is not None:
        chosen["rrc_price_rub_with_vat"] = float(chosen["base_price"])
    chosen["quantity"] = 1
    return chosen


def _find_jumpers_for_brand(brand: str, is_three_phase: bool, total_bat_count: int) -> Optional[Dict[str, Any]]:
    """Ищет штатные перемычки строго того же бренда, что и ИБП/шкаф."""
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM equipment_items
        WHERE (LOWER(brand) LIKE LOWER(?) OR LOWER(product_name) LIKE LOWER(?))
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND (product_name LIKE '%Перемыч%' OR product_name LIKE '%перемыч%')
        ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1
    """, (f"%{brand}%", f"%{brand}%"))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None
    item = dict(row)
    if item.get("rrc_price_rub_with_vat") is None and item.get("base_price") is not None:
        item["rrc_price_rub_with_vat"] = float(item["base_price"])
    item["quantity"] = 1 if "комплект" in item["product_name"].lower() else total_bat_count
    return item


def _try_assemble_single_supplier_package(
    brand: str,
    load_kw: float,
    autonomy_minutes: int,
    phases: str,
    modular_redundancy: bool,
    include_snmp: bool,
) -> Tuple[int, List[Dict[str, Any]], List[str]]:
    """
    Проходит по конкретному поставщику/бренду и собирает совместимую конфигурацию
    без подмешивания несовместимых перемычек и корпусов чужих производителей.
    """
    inverter_efficiency = 0.95
    required_ups_kw = round(load_kw * 1.15, 2)
    p_dc_kw = round(load_kw / inverter_efficiency, 3)
    autonomy_hours = autonomy_minutes / 60.0
    required_dc_energy_kwh = round(p_dc_kw * autonomy_hours, 2)

    ups = _find_ups_for_brand(brand, required_ups_kw, phases)
    if not ups:
        return 0, [], []

    ups["quantity"] = 2 if modular_redundancy else 1
    bat_per_string, dc_bus_v = _parse_dc_battery_count(ups, float(ups.get("power_kw") or load_kw))
    is_3ph = (phases == "3/3")

    eff_peukert = _get_agm_discharge_efficiency(autonomy_minutes)
    energy_per_block_wh = (required_dc_energy_kwh * 1000.0) / bat_per_string
    raw_required_ah = energy_per_block_wh / (12.0 * eff_peukert)

    parallel_strings = 1
    while (raw_required_ah / parallel_strings) > 200.0 and parallel_strings < 6:
        parallel_strings += 1
    target_single_ah = raw_required_ah / parallel_strings

    score = 10
    bom: List[Dict[str, Any]] = [ups]
    notes: List[str] = [
        f"Исходные данные: нагрузка {load_kw:g} кВт ({phases}), автономия {autonomy_minutes} мин ({autonomy_hours:.2f} ч).",
        f"Подобран ИБП {ups.get('brand', brand)}: {ups['product_name']} ({ups['quantity']} шт., шина DC {dc_bus_v:g} В = {bat_per_string} АКБ).",
    ]

    # 1. АКБ (сначала родной бренд, затем единая серия совместимых AGM АКБ)
    bat = _find_battery_for_brand(target_single_ah, preferred_brand=brand)
    if bat:
        score += 5
    else:
        bat = _find_battery_for_brand(target_single_ah, preferred_brand=None)
        if bat:
            score += 3

    total_bat_count = 0
    chosen_bat_ah = target_single_ah
    if bat:
        chosen_bat_ah = float(bat.get("rated_current_a") or target_single_ah)
        total_bat_count = bat_per_string * parallel_strings * ups["quantity"]
        bat["quantity"] = total_bat_count
        bom.append(bat)
        actual_kwh = round((total_bat_count * 12.0 * chosen_bat_ah * eff_peukert) / 1000.0, 2)
        notes.append(
            f"Батарейный массив ({bat['brand']}): {total_bat_count} шт. {bat['product_name']} "
            f"(полезная энергоотдача {actual_kwh} кВт·ч при коэфф. Пейкерта {eff_peukert:.2f})."
        )

    # 2. Батарейный шкаф (сначала родной бренд, затем совместимый напольный шкаф под габарит АКБ)
    cab, cab_qty = _find_cabinet_for_brand(brand, total_bat_count, chosen_bat_ah)
    if cab and cab_qty > 0:
        score += 5
    else:
        cab, cab_qty = _find_cabinet_for_brand(None, total_bat_count, chosen_bat_ah)
        if cab and cab_qty > 0:
            score += 2
    if cab and cab_qty > 0:
        cab["quantity"] = cab_qty
        bom.append(cab)
        notes.append(f"Батарейный шкаф ({cab['brand']}): {cab['product_name']} ({cab_qty} шт.).")

    # 3. SNMP-карта (строго родная бренда ИБП)
    if include_snmp:
        snmp = _find_snmp_for_brand(brand, is_3ph)
        if snmp:
            score += 5
            bom.append(snmp)
            notes.append(f"Карта сетевого мониторинга ({snmp['brand']}): {snmp['product_name']} (1 шт.).")

    # 4. Перемычки (ТОЛЬКО родные бренда ИБП/шкафа — чужие перемычки другого производителя не подмешиваются!)
    if total_bat_count >= 4:
        jumpers = _find_jumpers_for_brand(brand, is_3ph, total_bat_count)
        if jumpers:
            score += 5
            bom.append(jumpers)
            notes.append(f"Штатный комплект перемычек ({jumpers['brand']}): {jumpers['product_name']} ({jumpers['quantity']} шт.).")
        else:
            notes.append(
                f"Отдельные перемычки бренда {brand} в каталоге отсутствуют (входят в комплект батарейного шкафа либо заказываются по спецификации производителя)."
            )

    return score, bom, notes


def calculate_ups_system_bom(
    load_kw: float,
    autonomy_minutes: int,
    battery_chemistry: str = "AUTO",
    modular_redundancy: bool = False,
    include_snmp: bool = True,
    phases: Optional[str] = None,
    preferred_brand: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Последовательно обходит поставщиков в БД (HIDEN -> STARK -> ABSolite -> Kehua),
    выбирая конфигурацию с максимальной внутрибрендовой совместимостью (ИБП + АКБ + Шкаф + SNMP + Перемычки).
    """
    seed_infra_engineering_datasheets()

    chem = (battery_chemistry or "AUTO").upper()
    if chem == "AUTO":
        chem = "LIFEPO4" if (autonomy_minutes >= 180 and load_kw >= 10.0) else "AGM"
    if not phases:
        phases = "3/3" if load_kw >= 6.0 else "1/1"

    p_dc_kw = round(load_kw / 0.95, 3)

    supplier_order = [preferred_brand] if preferred_brand else ["HIDEN", "STARK", "ABSolite", "Kehua"]
    best_score = -1
    best_bom: List[Dict[str, Any]] = []
    best_notes: List[str] = []

    for sup_brand in supplier_order:
        if not sup_brand:
            continue
        sc, bom, notes = _try_assemble_single_supplier_package(
            brand=sup_brand,
            load_kw=load_kw,
            autonomy_minutes=autonomy_minutes,
            phases=phases,
            modular_redundancy=modular_redundancy,
            include_snmp=include_snmp,
        )
        if sc > best_score and bom:
            best_score = sc
            best_bom = bom
            best_notes = notes
        if best_score >= 28:
            break

    kit_title = (
        f"Комплект источника бесперебойного питания (ИБП) Нагрузка {load_kw:g} kW "
        f"при автономной работе не менее {autonomy_minutes} минут ({chem}), в составе:"
    )
    return {
        "status": "SIZED_OK" if best_bom else "NOT_FOUND",
        "kit_header_title": kit_title,
        "chemistry": chem,
        "load_kw": load_kw,
        "autonomy_minutes": autonomy_minutes,
        "dc_power_kw": p_dc_kw,
        "bom_items": best_bom,
        "engineering_notes": best_notes or ["Подходящий комплект не найден в базе данных."],
    }