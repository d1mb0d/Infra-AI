import math
import json
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
    text = f"{item.get('product_name', '')} {item.get('specifications', '')}"

    m_cnt = re.search(r'\((\d{1,3})(?:/\d{1,3})*\s*акб', text, re.IGNORECASE)
    if m_cnt:
        cnt = int(m_cnt.group(1))
        if cnt >= 2:
            return cnt, cnt * 12.0

    m_pm = re.search(r'[±]\s*(\d{2,3})\s*v', text, re.IGNORECASE)
    if m_pm:
        half_v = float(m_pm.group(1))
        total_v = half_v * 2.0
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
    """Коэффициент эффективной отдачи ёмкости AGM-батареи (доля от C20) по кривой Пейкерта."""
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


def _brand_has_full_agm_ecosystem(brand: str) -> bool:
    """Проверяет, есть ли у бренда в БД собственные шкафы и аксессуары под ИБП."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as cnt FROM equipment_items
        WHERE LOWER(brand) = LOWER(?)
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND (product_name LIKE '%Шкаф%' OR product_name LIKE '%шкаф%' OR product_name LIKE '%Стеллаж%')
    """, (brand,))
    cnt = cur.fetchone()["cnt"]
    conn.close()
    return cnt > 0


def _query_best_ups_from_db(
    min_kw: float,
    phases: Optional[str] = None,
    preferred_brand: Optional[str] = None,
    require_external_battery: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Подбирает из БД ИБП с актуальной ценой, отдавая приоритет брендам,
    у которых в БД есть полная экосистема (шкафы, SNMP, перемычки), чтобы избежать «сборной солянки».
    """
    conn = _get_conn()
    cur = conn.cursor()

    sql = """
        SELECT * FROM equipment_items
        WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND (etim_class = 'EC_UPS' OR (etim_class = 'EC_ACCESSORY' AND (product_name LIKE 'ИБП%' OR product_name LIKE 'ибп%')))
          AND product_name NOT LIKE 'Опция%'
          AND product_name NOT LIKE 'опция%'
          AND product_name NOT LIKE '%плата%'
          AND product_name NOT LIKE '%байпас%'
          AND product_name NOT LIKE '%подставка%'
          AND power_kw >= ?
    """
    params: List[Any] = [min_kw]

    if require_external_battery:
        sql += " AND product_name NOT LIKE '%встроенн% акб%' AND product_name NOT LIKE '%встроенные акб%'"

    if phases == "3/3":
        sql += " AND (product_name LIKE '%3/3%' OR specifications LIKE '%3:3%' OR power_kw >= 10.0)"
    elif phases == "1/1":
        sql += " AND (product_name NOT LIKE '%3/3%' AND specifications NOT LIKE '%3:3%')"

    if preferred_brand:
        sql += " AND LOWER(brand) LIKE LOWER(?)"
        params.append(f"%{preferred_brand}%")

    sql += " ORDER BY power_kw ASC, COALESCE(rrc_price_rub_with_vat, base_price) ASC"
    cur.execute(sql, params)
    candidates = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not candidates:
        return None

    for c in candidates:
        if c.get("rrc_price_rub_with_vat") is None and c.get("base_price") is not None:
            c["rrc_price_rub_with_vat"] = float(c["base_price"])

    # Если бренд не задан жёстко в ТЗ, предпочитаем бренд с полной собственной экосистемой шкафов и аксессуаров
    if not preferred_brand:
        for cand in candidates:
            if _brand_has_full_agm_ecosystem(cand.get("brand", "")):
                return cand

    return candidates[0]


def _query_modular_hiden_hem_system(load_kw: float, modular_redundancy: bool) -> List[Dict[str, Any]]:
    """
    Собирает единую монобрендовую модульную систему HIDEN HEM (шкаф HEM + модули HEPM + ЗУ 15А),
    учитывая, что в БД шкафы HEM и модули HEPM имеют класс EC_ACCESSORY.
    """
    conn = _get_conn()
    cur = conn.cursor()
    items: List[Dict[str, Any]] = []

    modules_for_load = max(1, math.ceil((load_kw * 1.15) / 10.0))
    modules_count = modules_for_load + 1 if (modular_redundancy or load_kw >= 12.0) else modules_for_load
    needed_kva = modules_count * 10

    # 1. Подбираем силовой шкаф HEM020 / HEM040 / HEM060 / HEM090 нужной ёмкости
    cur.execute("""
        SELECT * FROM equipment_items
        WHERE brand = 'HIDEN'
          AND model_code LIKE 'HEM0%'
          AND product_name LIKE 'Силовой шкаф%'
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
        ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC
    """)
    frames = [dict(r) for r in cur.fetchall()]
    chosen_frame = None
    for fr in frames:
        m_kva = re.search(r'\((\d+)\s*kVA', fr["product_name"], re.IGNORECASE)
        max_kva = int(m_kva.group(1)) if m_kva else 60
        if max_kva >= needed_kva:
            chosen_frame = fr
            break
    if not chosen_frame and frames:
        chosen_frame = frames[-1]

    if chosen_frame:
        chosen_frame["quantity"] = 1
        items.append(chosen_frame)

    # 2. Силовые модули HEPM10X
    cur.execute("""
        SELECT * FROM equipment_items
        WHERE brand = 'HIDEN' AND model_code = 'HEPM10X'
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
        LIMIT 1
    """)
    mod_row = cur.fetchone()
    if mod_row:
        mod_item = dict(mod_row)
        mod_item["quantity"] = modules_count
        items.append(mod_item)

    # 3. Зарядный модуль 15А для HEM10-90
    cur.execute("""
        SELECT * FROM equipment_items
        WHERE brand = 'HIDEN' AND model_code LIKE 'CHG15A%'
          AND COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
        LIMIT 1
    """)
    chg_row = cur.fetchone()
    if chg_row:
        chg_item = dict(chg_row)
        chg_item["quantity"] = 1
        items.append(chg_item)

    conn.close()
    return items


def _query_best_agm_battery_from_db(
    required_ah: float,
    preferred_brand: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Подбирает из БД единую модель аккумулятора 12В с ёмкостью >= required_ah."""
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


def _query_lifepo4_from_db(dc_voltage_v: float, required_kwh: float) -> Tuple[Optional[Dict[str, Any]], int]:
    """Подбирает совместимый высоковольтный (512В) или низковольтный (48/51.2В) накопитель LiFePO4."""
    conn = _get_conn()
    cur = conn.cursor()

    if dc_voltage_v >= 300:
        cur.execute("""
            SELECT * FROM equipment_items
            WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
              AND COALESCE(price_on_request, 0) = 0
              AND voltage_v >= 400
              AND (product_name LIKE '%LiFePO4%' OR product_name LIKE '%LFPT%')
            ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        if row:
            item = dict(row)
            unit_kwh = (float(item.get("voltage_v") or 512.0) * float(item.get("rated_current_a") or 200.0)) / 1000.0
            usable_kwh = unit_kwh * 0.95
            qty = max(1, math.ceil(required_kwh / usable_kwh))
            return item, qty
        return None, 1

    cur.execute("""
        SELECT * FROM equipment_items
        WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND etim_class = 'EC_BATTERY'
          AND voltage_v BETWEEN 48.0 AND 52.0
          AND product_name LIKE '%LiFePO4%'
        ORDER BY rated_current_a DESC, COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()
    if row:
        item = dict(row)
        unit_kwh = (float(item.get("voltage_v") or 51.2) * float(item.get("rated_current_a") or 100.0)) / 1000.0
        qty = max(1, math.ceil(required_kwh / (unit_kwh * 0.95)))
        return item, qty
    return None, 1


def _query_cabinet_strict_brand(ups_brand: str, total_bat_count: int, bat_ah: float) -> Tuple[Optional[Dict[str, Any]], int, str]:
    """
    Подбирает батарейный шкаф/стеллаж строго от производителя ИБП (ups_brand).
    Только если у данного бренда шкафов нет — ищет совместимый стеллаж/шкаф с пометкой инженера.
    """
    conn = _get_conn()
    cur = conn.cursor()

    def _fetch_cabs(brand_filter: Optional[str]) -> List[Dict[str, Any]]:
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
        p: List[Any] = []
        if brand_filter:
            sql += " AND LOWER(brand) = LOWER(?)"
            p.append(brand_filter)
        cur.execute(sql, p)
        return [dict(r) for r in cur.fetchall()]

    same_brand_cabs = _fetch_cabs(ups_brand)
    note_suffix = f"родной шкаф производителя ИБП ({ups_brand})"
    rows = same_brand_cabs
    if not rows:
        rows = _fetch_cabs(None)
        note_suffix = "универсальный батарейный шкаф (требуется проверка габаритов АКБ)"

    conn.close()
    if not rows:
        return None, 0, ""

    best_cab = None
    best_qty = 1
    best_total_cost = float("inf")

    for cab in rows:
        if cab.get("rrc_price_rub_with_vat") is None and cab.get("base_price") is not None:
            cab["rrc_price_rub_with_vat"] = float(cab["base_price"])

        txt = f"{cab.get('model_code', '')} {cab.get('product_name', '')}".lower()
        cap = None
        m_grid = re.search(r'\b([1-9]|10)\s*[xх*]\s*([2-9]|1\d|20)\b', txt)
        if m_grid:
            cap = int(m_grid.group(1)) * int(m_grid.group(2))
        else:
            m_pcs = re.search(r'(?:до|на)\s*(\d+)\s*(?:акб|шт)', txt)
            if m_pcs:
                cap = int(m_pcs.group(1))
            elif 's17' in txt:
                cap = 40  # Стандартный 4-ярусный шкаф HIDEN S17 (4 яруса по 10 АКБ до 40 Ач)
            elif 's16' in txt:
                cap = 32
            elif 's12' in txt or 's10' in txt:
                cap = 16

        if cap and cap >= 30 and bat_ah > 45:
            cap = cap // 2

        if cap and cap >= 4:
            needed_cabs = max(1, math.ceil(total_bat_count / cap))
            tot_cost = float(cab["rrc_price_rub_with_vat"]) * needed_cabs
            if tot_cost < best_total_cost:
                best_total_cost = tot_cost
                best_cab = cab
                best_qty = needed_cabs

    return best_cab, best_qty, note_suffix


def _query_snmp_and_jumpers_strict_brand(
    ups_brand: str,
    is_three_phase: bool,
    total_bat_count: int,
) -> List[Dict[str, Any]]:
    """Подбирает SNMP-карту и комплект перемычек строго в рамках бренда ИБП."""
    conn = _get_conn()
    cur = conn.cursor()
    extras: List[Dict[str, Any]] = []

    # 1. SNMP-карта строго того же бренда
    cur.execute("""
        SELECT * FROM equipment_items
        WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
          AND COALESCE(price_on_request, 0) = 0
          AND LOWER(brand) = LOWER(?)
          AND (product_name LIKE '%SNMP%' OR product_name LIKE '%snmp%')
          AND product_name NOT LIKE '%бокс%'
          AND product_name NOT LIKE '%датчик%'
        ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC
    """, (ups_brand,))
    snmp_rows = [dict(r) for r in cur.fetchall()]
    if snmp_rows:
        chosen_snmp = snmp_rows[0]
        if is_three_phase:
            for sr in snmp_rows:
                if any(k in sr["product_name"].lower() for k in ["3-ф", "da-807", "she8m", "flex33"]):
                    chosen_snmp = sr
                    break
        chosen_snmp["quantity"] = 1
        extras.append(chosen_snmp)

    # 2. Перемычки: сначала ищем готовый комплект перемычек бренда ИБП (например, HIDEN на 40 АКБ)
    if total_bat_count >= 4:
        cur.execute("""
            SELECT * FROM equipment_items
            WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
              AND COALESCE(price_on_request, 0) = 0
              AND LOWER(brand) = LOWER(?)
              AND (product_name LIKE '%Перемыч%' OR product_name LIKE '%перемыч%')
            ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1
        """, (ups_brand,))
        brand_j = cur.fetchone()
        if brand_j:
            j_item = dict(brand_j)
            j_item["quantity"] = 1 if "комплект" in j_item["product_name"].lower() else total_bat_count
            extras.append(j_item)
        else:
            # Если у бренда ИБП в прайсе нет отдельных перемычек (идут в комплекте с ИБП/шкафом или берутся поштучно)
            section_filter = (
                "AND (product_name LIKE '%35мм2%' OR product_name LIKE '%25мм2%')"
                if is_three_phase
                else "AND (product_name LIKE '%10мм2%' OR product_name LIKE '%25мм2%')"
            )
            cur.execute(f"""
                SELECT * FROM equipment_items
                WHERE COALESCE(rrc_price_rub_with_vat, base_price) IS NOT NULL
                  AND COALESCE(price_on_request, 0) = 0
                  AND (product_name LIKE '%Перемычка%' OR product_name LIKE '%перемычка%')
                  {section_filter}
                ORDER BY COALESCE(rrc_price_rub_with_vat, base_price) ASC LIMIT 1
            """)
            j_row = cur.fetchone()
            if j_row:
                j_item = dict(j_row)
                j_item["quantity"] = total_bat_count
                extras.append(j_item)

    conn.close()
    return extras


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
    Универсальный конструктор систем гарантированного электропитания с контролем
    моноекосистемной совместимости (ИБП + шкаф + SNMP + перемычки + АКБ одного производителя).
    """
    seed_infra_engineering_datasheets()

    chem = (battery_chemistry or "AUTO").upper()
    if chem == "AUTO":
        chem = "LIFEPO4" if (autonomy_minutes >= 180 and load_kw >= 10.0) else "AGM"

    if not phases:
        phases = "3/3" if load_kw >= 6.0 else "1/1"

    inverter_efficiency = 0.95
    safety_factor = 1.15
    required_ups_kw = round(load_kw * safety_factor, 2)
    p_dc_kw = round(load_kw / inverter_efficiency, 3)
    autonomy_hours = autonomy_minutes / 60.0
    required_energy_kwh = round(load_kw * autonomy_hours, 2)
    required_dc_energy_kwh = round(p_dc_kw * autonomy_hours, 2)

    bom_items: List[Dict[str, Any]] = []
    engineering_notes: List[str] = [
        f"Исходные данные: нагрузка {load_kw:g} кВт ({phases}), автономия {autonomy_minutes} мин ({autonomy_hours:.2f} ч).",
        f"Расчётная мощность ИБП (с запасом 15%): {required_ups_kw} кВт; мощность по шине DC (КПД 95%): {p_dc_kw} кВт.",
    ]

    # ВЕТКА 1: Модульный ИБП HIDEN HEM + LiFePO4 (как в эталонном проекте на 15 кВт / 6 часов)
    if (chem == "LIFEPO4" or modular_redundancy) and phases == "3/3" and (not preferred_brand or preferred_brand.upper() == "HIDEN"):
        hem_parts = _query_modular_hiden_hem_system(load_kw, modular_redundancy)
        if len(hem_parts) >= 2:
            bom_items.extend(hem_parts)
            lfp_item, lfp_qty = _query_lifepo4_from_db(512.0, required_dc_energy_kwh)
            if lfp_item:
                lfp_item["quantity"] = lfp_qty
                bom_items.append(lfp_item)
                total_kva = sum(10 * p["quantity"] for p in hem_parts if "HEPM10X" in p.get("model_code", ""))
                engineering_notes.append(
                    f"Собрана модульная система единой серии HIDEN HEM ({total_kva} кВА) + совместимый шкаф LiFePO4 {lfp_item['model_code']} ({lfp_qty} шт.)."
                )
                return {
                    "status": "SIZED_OK",
                    "kit_header_title": f"ИБП 3ф/3ф {total_kva}кВА, автономия {autonomy_hours:g} ч при нагрузке {load_kw:g} кВт (LFPT).",
                    "chemistry": "LIFEPO4",
                    "load_kw": load_kw,
                    "autonomy_minutes": autonomy_minutes,
                    "dc_power_kw": p_dc_kw,
                    "bom_items": bom_items,
                    "engineering_notes": engineering_notes,
                }

    # ВЕТКА 2: Моноблочный ИБП с моноекосистемной проверкой совместимости
    ups_item = _query_best_ups_from_db(
        min_kw=required_ups_kw,
        phases=phases,
        preferred_brand=preferred_brand,
        require_external_battery=True,
    )
    if not ups_item:
        ups_item = _query_best_ups_from_db(min_kw=load_kw, phases=None, preferred_brand=None, require_external_battery=False)

    if not ups_item:
        return {
            "status": "NO_SUITABLE_UPS_IN_DB",
            "kit_header_title": f"Комплект ИБП {load_kw:g} кВт / {autonomy_minutes} мин",
            "chemistry": chem,
            "load_kw": load_kw,
            "autonomy_minutes": autonomy_minutes,
            "dc_power_kw": p_dc_kw,
            "bom_items": [],
            "engineering_notes": ["В базе данных не найден ИБП с актуальной ценой под заданную мощность."],
        }

    ups_item["quantity"] = 2 if modular_redundancy else 1
    bom_items.append(ups_item)

    bat_per_string, dc_bus_v = _parse_dc_battery_count(ups_item, float(ups_item.get("power_kw") or load_kw))
    ups_brand = ups_item.get("brand") or "HIDEN"
    is_3ph = (phases == "3/3") or (float(ups_item.get("power_kw") or 0) >= 10.0)

    engineering_notes.append(
        f"Подобран ИБП бренда {ups_brand}: {ups_item['product_name']} ({ups_item['quantity']} шт., шина DC {dc_bus_v:g} В = {bat_per_string} АКБ)."
    )

    total_bat_count = 0
    chosen_bat_ah = 0.0

    if chem == "LIFEPO4":
        lfp_item, lfp_qty = _query_lifepo4_from_db(dc_bus_v, required_dc_energy_kwh)
        if lfp_item:
            lfp_item["quantity"] = lfp_qty * ups_item["quantity"]
            bom_items.append(lfp_item)
        else:
            chem = "AGM"

    if chem == "AGM":
        eff_peukert = _get_agm_discharge_efficiency(autonomy_minutes)
        energy_per_block_wh = (required_dc_energy_kwh * 1000.0) / bat_per_string
        raw_required_ah = energy_per_block_wh / (12.0 * eff_peukert)

        parallel_strings = 1
        while (raw_required_ah / parallel_strings) > 200.0 and parallel_strings < 6:
            parallel_strings += 1

        target_single_ah = raw_required_ah / parallel_strings
        # Сначала ищем АКБ того же бренда, что и ИБП (например, HIDEN или STARK), затем — единую серию АКБ
        bat_item = _query_best_agm_battery_from_db(target_single_ah, preferred_brand=ups_brand)
        if not bat_item:
            bat_item = _query_best_agm_battery_from_db(target_single_ah, preferred_brand=None)

        if bat_item:
            chosen_bat_ah = float(bat_item.get("rated_current_a") or target_single_ah)
            total_bat_count = bat_per_string * parallel_strings * ups_item["quantity"]
            bat_item["quantity"] = total_bat_count
            bom_items.append(bat_item)

            actual_kwh = round((total_bat_count * 12.0 * chosen_bat_ah * eff_peukert) / 1000.0, 2)
            engineering_notes.append(
                f"Батарейный массив единой серии ({bat_item['brand']}): {parallel_strings} гр. × {bat_per_string} шт. = {total_bat_count} шт. "
                f"{bat_item['product_name']} (отдача Пейкерта {eff_peukert:.2f}, полезная энергия {actual_kwh} кВт·ч)."
            )

            cab_item, cab_qty, cab_note = _query_cabinet_strict_brand(ups_brand, total_bat_count, chosen_bat_ah)
            if cab_item and cab_qty > 0:
                cab_item["quantity"] = cab_qty
                bom_items.append(cab_item)
                engineering_notes.append(f"Батарейный шкаф ({cab_note}): {cab_item['product_name']} ({cab_qty} шт.).")

    if include_snmp:
        extras = _query_snmp_and_jumpers_strict_brand(ups_brand, is_3ph, total_bat_count)
        bom_items.extend(extras)
        if extras:
            names_ext = ", ".join(f"{x['product_name']} [{x['brand']}] ({x['quantity']} шт.)" for x in extras)
            engineering_notes.append(f"Совместимая обвязка экосистемы {ups_brand}: {names_ext}.")

    kit_title = (
        f"Комплект источника бесперебойного питания (ИБП) Нагрузка {load_kw:g} kW "
        f"при автономной работе не менее {autonomy_minutes} минут ({chem}), в составе:"
    )

    return {
        "status": "SIZED_OK",
        "kit_header_title": kit_title,
        "chemistry": chem,
        "load_kw": load_kw,
        "autonomy_minutes": autonomy_minutes,
        "dc_power_kw": p_dc_kw,
        "bom_items": bom_items,
        "engineering_notes": engineering_notes,
    }