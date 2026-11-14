import glob
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


# ==============================================================================
# 1. БАЗОВЫЕ УТИЛИТЫ НОРМАЛИЗАЦИИ И ТРАНСЛИТЕРАЦИИ SKU
# ==============================================================================
TRANSLIT_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def transliterate_ru(text: str) -> str:
    """Переводит кириллицу в латиницу для формирования чистых системных SKU."""
    res = []
    for ch in str(text).lower():
        if ch in TRANSLIT_MAP:
            res.append(TRANSLIT_MAP[ch])
        elif ch.isalnum() or ch in ('-', '_', '.'):
            res.append(ch)
        else:
            res.append('-')
    cleaned = re.sub(r'-+', '-', ''.join(res)).strip('-')
    return cleaned.upper()


def clean_num(val: Any) -> Optional[float]:
    """Извлекает число (float) из ячейки Excel, очищая пробелы, валюту, 'от' и запятые."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    if isinstance(val, (int, float, np.number)):
        return float(val) if not np.isnan(val) else None
    s = str(val).strip().replace('\xa0', '').replace(' ', '').replace(',', '.')
    if not s or s in ['-', '—', '_', 'нет', 'nan', 'None'] or set(s) <= {'_', '-', '.'}:
        return None
    if 'запрос' in s.lower():
        return None
    m = re.search(r'[-+]?\d+(?:\.\d+)?', s)
    return float(m.group(0)) if m else None


def is_price_on_request(val: Any) -> bool:
    """Проверяет, указано ли в ячейке 'по запросу'."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return False
    return 'запрос' in str(val).lower()


def parse_dimensions(dim_str: Any, order: str = 'LxWxH') -> Optional[Dict[str, Any]]:
    """Парсит габариты вида '438x430x88 (2U)' или '600х900х2000мм' в словарь мм."""
    if dim_str is None or (isinstance(dim_str, float) and np.isnan(dim_str)):
        return None
    s = str(dim_str).replace(',', '.')
    rack_u = None
    m_u = re.search(r'(\d+)\s*[uUЮю]', s)
    if m_u:
        rack_u = int(m_u.group(1))
    nums = [
        float(x)
        for x in re.findall(r'\d+(?:\.\d+)?', re.sub(r'\(\d+\s*[uUЮю]\)', '', s))
    ]
    if len(nums) >= 3:
        if order == 'WxDxH':
            res = {'width_mm': nums[0], 'depth_mm': nums[1], 'height_mm': nums[2]}
        elif order == 'WxHxD':
            res = {'width_mm': nums[0], 'height_mm': nums[1], 'depth_mm': nums[2]}
        elif order == 'HxWxD':
            res = {'height_mm': nums[0], 'width_mm': nums[1], 'depth_mm': nums[2]}
        else:
            res = {'length_mm': nums[0], 'width_mm': nums[1], 'height_mm': nums[2]}
        if rack_u:
            res['rack_units_u'] = rack_u
        return res
    elif len(nums) == 2 and rack_u:
        return {
            'width_mm': nums[0],
            'depth_mm': nums[1],
            'height_mm': round(rack_u * 44.45, 1),
            'rack_units_u': rack_u,
        }
    return None


def decode_veda_model_code(model_code: str) -> Tuple[Optional[float], Optional[float], Optional[float], str, Dict[str, Any]]:
    """Декодирует типовой код VEDA (мощность, ток, напряжение, IP и встроенные опции)."""
    s = str(model_code).strip()
    specs: Dict[str, Any] = {}
    power_kw, current_a, voltage_v, ip_rating = None, None, None, 'IP20'

    m_pk = re.search(r'-P(\d+)K(\d+)-', s)
    m_p0k = re.search(r'-PK(\d+)-', s)
    m_pnum = re.search(r'-P(\d{3,4})-', s)
    if m_pk:
        power_kw = float(f'{m_pk.group(1)}.{m_pk.group(2)}')
    elif m_p0k:
        power_kw = float(f'0.{m_p0k.group(1)}')
    elif m_pnum:
        power_kw = float(int(m_pnum.group(1)))

    m_curr = re.search(r'-P[A-Z0-9]+-(\d{4})-', s)
    if m_curr:
        current_a = float(int(m_curr.group(1)))
    else:
        m_c2 = re.search(r'-(\d+)P(\d+)-', s)
        if m_c2:
            current_a = float(f'{m_c2.group(1)}.{m_c2.group(2)}')

    if '-S2' in s:
        voltage_v, specs['phases'] = 230.0, 1
    elif '-T2' in s:
        voltage_v, specs['phases'] = 230.0, 3
    elif '-T4' in s:
        voltage_v, specs['phases'] = 400.0, 3
    elif '-T6' in s:
        voltage_v, specs['phases'] = 690.0, 3

    m_ip = re.search(r'-(?:E|C)(20|54|55|66)-', s)
    if m_ip:
        ip_rating = f'IP{m_ip.group(1)}'

    if '-A-T' in s:
        specs['overload_type'] = 'normal_120%'
    elif '-U-T' in s:
        specs['overload_type'] = 'heavy_150%'

    m_tail = re.search(r'-(?:E|C)\d{2}-([BSRKTN])-(C2|C3|CH|H|N)(?:-([DN]))?', s)
    if m_tail:
        opt_code, emc_code, choke_code = m_tail.group(1), m_tail.group(2), m_tail.group(3)
        specs['has_brake_chopper'] = opt_code in ['B', 'R', 'K', 'T']
        specs['has_sto'] = opt_code in ['S', 'R', 'T']
        specs['has_disconnect_switch'] = opt_code in ['K', 'T']
        specs['emc_filter'] = emc_code
        if choke_code:
            specs['has_dc_choke'] = choke_code == 'D'

    return power_kw, current_a, voltage_v, ip_rating, specs


# ==============================================================================
# 2. УНИВЕРСАЛЬНЫЙ АДАПТИВНЫЙ ПАРСЕР EXCEL-ПРАЙСОВ ЛЮБОГО ПОСТАВЩИКА
# ==============================================================================
class UniversalSupplierETL:
    """
    Универсальный движок извлечения, классификации и нормализации прайс-листов:
      1) Автоматически определяет назначение каждого листа в книге (пропускает оглавления и калькуляторы);
      2) Динамически находит заголовки таблиц (включая многоуровневые шапки и несколько таблиц на одном листе);
      3) Поддерживает блоки без шапки (например, 'Дополнительное оборудование:' в прайсах);
      4) Генерирует уникальные транслитерированные SKU при отсутствии артикулов;
      5) Выделяет статус жизненного цикла (актуальный товар vs 'Снято с производства / складские остатки');
      6) Сохраняет многоуровневые цены (Субдистрибьютор / Интегратор / Партнер / РРЦ) и позиции 'По запросу'.
    """

    SKIP_SHEET_KEYWORDS = [
        'оглавление', 'расчет', 'расчёт', 'коды валют', 'валюта из списка',
        'изменения', 'всп', 'инструкция', 'титул'
    ]

    def __init__(
        self,
        supplier_name: str,
        default_brand: str,
        default_currency: str = 'RUB',
        default_vat_included: bool = True,
    ):
        self.supplier = supplier_name
        self.default_brand = default_brand
        self.default_currency = default_currency
        self.default_vat_included = default_vat_included
        self._dop_counters: Dict[str, int] = {}

    def _next_generated_sku(self, brand: str, sheet_name: str, item_name: str, is_dop: bool = False) -> str:
        """
        Генерирует понятный и уникальный SKU при отсутствии артикула поставщика:
        Формат: {BRAND}-{SHEET_TRANSLIT}[-DOP]-{SHORT_NAME_TRANSLIT}-{001}
        """
        sh_tr = transliterate_ru(sheet_name)[:22] or 'CATALOG'
        words = [w for w in re.findall(r'[A-Za-zА-Яа-я0-9]+', str(item_name)) if len(w) > 1][:3]
        nm_tr = transliterate_ru('-'.join(words))[:24] or 'ITEM'

        prefix = f"{brand.upper()}-{sh_tr}{'-DOP' if is_dop else ''}"
        self._dop_counters[prefix] = self._dop_counters.get(prefix, 0) + 1
        seq = self._dop_counters[prefix]
        return f"{prefix}-{nm_tr}-{seq:03d}"[:95]

    def _make_sku(
        self,
        brand: str,
        sheet_name: str,
        code_val: Any = None,
        model_val: Any = None,
        name_val: Any = None,
        is_dop: bool = False,
    ) -> str:
        if code_val is not None and pd.notna(code_val):
            c_str = re.sub(r'\.0$', '', str(code_val).strip())
            if c_str and c_str.lower() not in ['-', 'nan', 'none', 'нет', 'new', 'новый']:
                return f"{brand.upper()}-{c_str}"[:95]

        if model_val is not None and pd.notna(model_val):
            m_str = str(model_val).strip().split('\n')[0].strip()
            if m_str and len(m_str) <= 55 and m_str.lower() not in ['-', 'nan', 'none']:
                m_tr = transliterate_ru(m_str)
                if m_tr and len(m_tr) >= 2:
                    return f"{brand.upper()}-{m_tr}"[:95]

        return self._next_generated_sku(brand, sheet_name, str(name_val or model_val or 'item'), is_dop=is_dop)

    def _infer_brand_for_sheet(self, sheet_name: str) -> str:
        s = sheet_name.lower()
        if 'deye' in s:
            return 'Deye'
        if 'must' in s:
            return 'MUST'
        if 'avanbatt' in s:
            return 'AVANBATT'
        if 'zetwey' in s or 'zetway' in s:
            return 'ZetWey'
        return self.default_brand

    def _classify_etim_and_title(
        self,
        raw_name: str,
        model_code: str,
        sheet_name: str,
        desc: str = '',
    ) -> Tuple[str, str, Optional[str]]:
        """Определяет класс ETIM, нормализованное наименование и подтип (например, battery_cabinet)."""
        full_ctx = f"{sheet_name} {raw_name} {model_code} {desc}".lower()
        sub_type = None

        if any(w in full_ctx for w in ['шкаф', 'кабинет', 'стеллаж', 'rack 3u', 'battery cabinet', 'вбм', 'батарейный модуль']):
            sub_type = 'battery_cabinet'
            e_class = 'EC_ACCESSORY'
        elif any(w in full_ctx for w in [
            'балансир', 'колпач', 'перемыч', 'кабель', 'клемм', 'amphenol',
            'snmp', 'плата', 'адаптер', 'мониторинг', 'рельс', 'опци', 'выезд специалиста'
        ]):
            e_class = 'EC_ACCESSORY'
        elif any(w in full_ctx for w in ['инвертор', 'deye', 'must', 'зарядная станция', 'vl-hc']):
            e_class = 'EC_INVERTER'
        elif any(w in full_ctx for w in [
            'аккумулятор', 'акб', 'батаре', 'lifepo4', 'lfp', 'agm', 'gel', 'opzv',
            'moto', 'gp ', 'gpl ', 'hr ', 'hrl ', 'vrc', 'ft-'
        ]) and 'без акб' not in full_ctx and 'ибп' not in full_ctx and 'ups' not in full_ctx:
            e_class = 'EC_BATTERY'
        elif any(w in full_ctx for w in ['плк', 'панель оператора', 'hmi', 'vh-', 'vc-', 'vs-']):
            e_class = 'EC_PLC_HMI'
        elif any(w in full_ctx for w in ['преобразователь частоты', 'регулятор скорости', 'устройство плавного пуска', 'упп', 'vfd', 'mcd', 'vf-', 'vcd', 'vm-']):
            e_class = 'EC_VFD_MCD'
        elif any(w in full_ctx for w in ['ибп', 'ups', 'бесперебойн', 'источник бесперебойного', 'силовой шкаф hem', 'силовой модуль hepm']):
            e_class = 'EC_UPS'
        else:
            e_class = 'EC_ACCESSORY'

        clean_title = str(raw_name).strip().replace('\n', ' ')
        if sub_type == 'battery_cabinet' and not any(w in clean_title.lower() for w in ['шкаф', 'кабинет', 'стеллаж', 'rack', 'вбм', 'модуль']):
            clean_title = f"Аккумуляторный шкаф {clean_title}"
        elif e_class == 'EC_BATTERY' and not any(w in clean_title.lower() for w in ['аккумулятор', 'акб', 'батаре']):
            clean_title = f"Аккумуляторная батарея {clean_title}"
        elif e_class == 'EC_VFD_MCD' and not any(w in clean_title.lower() for w in ['преобразователь', 'регулятор', 'устройство', 'упп']):
            prefix = "Устройство плавного пуска" if any(k in model_code.upper() for k in ['VM-', 'MCD', 'VCD']) else "Преобразователь частоты"
            clean_title = f"{prefix} {clean_title}"

        return e_class, clean_title, sub_type

    def _detect_header_mapping(self, df: pd.DataFrame, row_idx: int) -> Optional[Dict[str, Any]]:
        """
        Проверяет, является ли строка row_idx заголовком товарной таблицы (с учётом двухстрочных шапок).
        Возвращает карту индексов колонок или None.
        """
        n_cols = df.shape[1]
        combined_headers = []
        for c in range(n_cols):
            v1 = str(df.iloc[row_idx, c]).strip().lower().replace('\n', ' ') if pd.notna(df.iloc[row_idx, c]) else ''
            v2 = ''
            if row_idx + 1 < len(df):
                nxt = df.iloc[row_idx + 1, c]
                if pd.notna(nxt) and isinstance(nxt, str) and clean_num(nxt) is None:
                    nxt_low = nxt.strip().lower().replace('\n', ' ')
                    if any(k in nxt_low for k in ['длина', 'ширина', 'высота', 'usd', 'руб', 'с ндс', 'без ндс', 'ач', 'квт']):
                        v2 = nxt_low
            combined_headers.append(f"{v1} {v2}".strip())

        joined = ' | '.join(combined_headers)

        # Обязательное условие таблицы прайса: есть колонка идентификации товара И колонка цены
        has_item_col = any(
            k in joined
            for k in ['модель', 'типовой код', 'наименование', 'код заказа', 'заказной код', 'тип']
        )
        has_price_col = any(
            k in joined
            for k in ['цена', 'ррц', 'мрц', 'партнер', 'партнёр', 'субдистрибьютор', 'интегратор', 'стоимость']
        )
        if not (has_item_col and has_price_col):
            return None

        mapping: Dict[str, Any] = {'header_row': row_idx, 'raw_headers': combined_headers}

        for idx, h in enumerate(combined_headers):
            if not h:
                continue
            if any(k in h for k in ['код 1с', 'код 1 с', 'код заказа', 'заказной код', 'артикул']) and 'code' not in mapping:
                mapping['code'] = idx
            elif any(k in h for k in ['типовой код', 'модель / наименование', 'модель', 'тип']) and 'model' not in mapping:
                mapping['model'] = idx
            elif 'полное наименование' in h:
                mapping['full_name'] = idx
            elif 'наименование' in h and 'name' not in mapping:
                mapping['name'] = idx
            elif any(k in h for k in ['описание', 'характерист', 'состав']) and 'desc' not in mapping:
                mapping['desc'] = idx
            elif 'примечан' in h and 'note' not in mapping:
                mapping['note'] = idx
            elif any(k in h for k in ['субдистр', 'дистриб']) and 'price_subdist' not in mapping:
                mapping['price_subdist'] = idx
            elif 'интегратор' in h and 'price_integrator' not in mapping:
                mapping['price_integrator'] = idx
            elif any(k in h for k in ['партнер', 'партнёр', 'опт', 'спец']) and 'price_partner' not in mapping:
                mapping['price_partner'] = idx
            elif any(k in h for k in ['ррц', 'мрц', 'розн', 'конечн']) and 'price_rrc' not in mapping:
                mapping['price_rrc'] = idx
            elif any(k in h for k in ['цена', 'стоимость']) and 'price_generic' not in mapping:
                mapping['price_generic'] = idx
            elif 'мощност' in h and 'power' not in mapping:
                mapping['power'] = idx
            elif any(k in h for k in ['емкост', 'ёмкост']) and 'capacity' not in mapping:
                mapping['capacity'] = idx
            elif 'ток' in h and 'заряд' not in h and 'current' not in mapping:
                mapping['current'] = idx
            elif 'напряж' in h and 'voltage' not in mapping:
                mapping['voltage'] = idx
            elif 'длина' in h and 'dim_l' not in mapping:
                mapping['dim_l'] = idx
            elif 'ширина' in h and 'dim_w' not in mapping:
                mapping['dim_w'] = idx
            elif 'высота' in h and 'dim_h' not in mapping:
                mapping['dim_h'] = idx
            elif any(k in h for k in ['габарит', 'размер', 'вхшхг', 'шхгхв', 'шхвхг', 'дхшхв']) and 'dims' not in mapping:
                mapping['dims'] = idx
            elif any(k in h for k in ['вес', 'масса']) and 'weight' not in mapping:
                mapping['weight'] = idx

        # Определяем валюту и НДС из текста заголовков цен
        price_cols = [
            mapping[k]
            for k in ['price_rrc', 'price_partner', 'price_subdist', 'price_integrator', 'price_generic']
            if k in mapping
        ]
        p_hdr_txt = ' '.join(combined_headers[i] for i in price_cols)
        if 'у.е' in p_hdr_txt:
            mapping['currency'] = 'CNY_VEDA' if self.supplier == 'VEDA MC' else 'USD'
        elif 'usd' in p_hdr_txt or '$' in p_hdr_txt:
            mapping['currency'] = 'USD'
        elif 'eur' in p_hdr_txt or '€' in p_hdr_txt:
            mapping['currency'] = 'EUR'
        elif 'руб' in p_hdr_txt or '₽' in p_hdr_txt:
            mapping['currency'] = 'RUB'
        else:
            mapping['currency'] = self.default_currency

        if 'без ндс' in p_hdr_txt:
            mapping['vat_included'] = False
        elif 'с ндс' in p_hdr_txt or 'включая ндс' in p_hdr_txt:
            mapping['vat_included'] = True
        else:
            mapping['vat_included'] = self.default_vat_included

        # Проверяем, есть ли сразу справа от основной цены безымянная колонка (как в Ventura)
        if price_cols:
            max_p_col = max(price_cols)
            if max_p_col + 1 < n_cols and combined_headers[max_p_col + 1] == '':
                mapping['unlabeled_right_col'] = max_p_col + 1

        return mapping

    def _parse_veda_code_price_and_crossrefs(self, df: pd.DataFrame) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Специализированный разбор сводного листа VEDA 'код-цена' (все SKU + все таблицы Danfoss FC-51/101/102/202)."""
        items, cross_refs = [], []
        current_fc_series = 'VLT Micro Drive FC-51'

        for _, row in df.iterrows():
            # 1. Извлечение всех таблиц кросс-замен Danfoss (колонки E-F и H-L)
            if len(row) > 5 and pd.notna(row.iloc[4]) and pd.notna(row.iloc[5]):
                c4, c5 = str(row.iloc[4]).strip(), str(row.iloc[5]).strip()
                if 'старый' not in c4.lower() and re.match(r'^[A-Z0-9]{5,12}$', c5):
                    cross_refs.append({
                        'legacy_brand': 'Danfoss',
                        'legacy_series': 'VLT Micro Drive FC-51',
                        'legacy_sku': c4,
                        'legacy_model_code': None,
                        'target_veda_sku': f'VEDA-{c5}',
                    })

            if len(row) > 9 and pd.notna(row.iloc[7]):
                c7 = str(row.iloc[7]).strip()
                if 'старый код' in c7.lower():
                    current_fc_series = c7.replace('Старый код', '').strip()
                elif pd.notna(row.iloc[9]):
                    c8 = str(row.iloc[8]).strip() if pd.notna(row.iloc[8]) else None
                    c9 = str(row.iloc[9]).strip()
                    if re.match(r'^[A-Z0-9]{5,12}$', c9):
                        cross_refs.append({
                            'legacy_brand': 'Danfoss',
                            'legacy_series': current_fc_series,
                            'legacy_sku': c7,
                            'legacy_model_code': c8,
                            'target_veda_sku': f'VEDA-{c9}',
                        })

            # 2. Извлечение всех строк прайса из колонок A, B, C
            if len(row) < 3:
                continue
            sku_raw, mod_raw, price_raw = row.iloc[0], row.iloc[1], row.iloc[2]
            if pd.isna(sku_raw):
                continue
            sku_str = str(sku_raw).strip()
            if not re.match(r'^[A-Z0-9]{5,12}$', sku_str):
                continue

            price_val = clean_num(price_raw)
            on_req = is_price_on_request(price_raw)
            if price_val is None and not on_req:
                continue

            mod_str = str(mod_raw).strip() if pd.notna(mod_raw) else sku_str
            dec_kw, dec_a, dec_v, dec_ip, dec_specs = decode_veda_model_code(mod_str)
            e_class, clean_title, sub_type = self._classify_etim_and_title(mod_str, mod_str, 'код-цена')
            if sub_type:
                dec_specs['sub_type'] = sub_type

            items.append({
                'supplier': 'VEDA MC',
                'brand': 'VEDA',
                'etim_class': e_class,
                'sku': f'VEDA-{sku_str}',
                'model_code': mod_str,
                'product_name': clean_title,
                'base_price': price_val,
                'currency_code': 'CNY_VEDA',
                'vat_included': False,
                'price_on_request': on_req,
                'lifecycle_status': None,
                'price_tiers': json.dumps(
                    {
                        'list_price_ue_no_vat': price_val,
                        'partner_50pct_ue_no_vat': round(price_val * 0.5, 2) if price_val else None,
                    },
                    ensure_ascii=False,
                ),
                'power_kw': dec_kw,
                'rated_current_a': dec_a,
                'voltage_v': dec_v,
                'ip_rating': dec_ip,
                'weight_kg': None,
                'dimensions_mm': None,
                'specifications': json.dumps(dec_specs, ensure_ascii=False),
            })

        return items, cross_refs

    def _parse_kits_sheet(self, df: pd.DataFrame, sheet_name: str) -> List[Dict[str, Any]]:
        """Разбирает листы готовых комплектов ИБП+АКБ (BOM)."""
        kits = []
        current_kit = None
        for _, row in df.iterrows():
            if len(row) < 4:
                continue
            col1 = row.iloc[1] if len(row) > 1 else None
            qty = clean_num(row.iloc[2]) if len(row) > 2 else None
            rrc = clean_num(row.iloc[3]) if len(row) > 3 else None
            disc = clean_num(row.iloc[4]) if len(row) > 4 else None
            part_p = clean_num(row.iloc[5]) if len(row) > 5 else None

            if pd.notna(col1) and qty is None and rrc is None:
                txt = str(col1).strip()
                if any(w in txt.lower() for w in ['нагрузка', 'автономия', 'stark', 'комплект']):
                    current_kit = txt
            elif pd.notna(col1) and qty is not None and rrc is not None and current_kit:
                kits.append({
                    'supplier': self.supplier,
                    'kit_sheet': sheet_name,
                    'kit_name': current_kit,
                    'component_model': str(col1).strip(),
                    'quantity': int(qty),
                    'rrc_usd': rrc,
                    'discount': disc,
                    'partner_usd': part_p,
                })
        return kits

    def parse_excel_workbook(self, filepath: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Главная универсальная точка входа: сканирует все листы книги Excel,
        возвращает (список_товаров, кросс_замены, комплекты_BOM).
        """
        xls = pd.ExcelFile(filepath)
        all_items: List[Dict[str, Any]] = []
        all_cross_refs: List[Dict[str, Any]] = []
        all_kits: List[Dict[str, Any]] = []
        veda_master_prices: Dict[str, Dict[str, Any]] = {}

        for sheet in xls.sheet_names:
            sheet_low = sheet.strip().lower()
            if any(skip in sheet_low for skip in self.SKIP_SHEET_KEYWORDS):
                continue

            df = pd.read_excel(xls, sheet, header=None)
            if df.empty:
                continue

            # Спецобработка сводного листа VEDA 'код-цена'
            if self.supplier == 'VEDA MC' and 'код-цена' in sheet_low:
                vp_items, vp_cross = self._parse_veda_code_price_and_crossrefs(df)
                all_cross_refs.extend(vp_cross)
                for it in vp_items:
                    veda_master_prices[it['sku']] = it
                continue

            # Спецобработка листов комплектов ИБП+АКБ
            if 'комплект' in sheet_low and any(k in sheet_low for k in ['line-interactive', 'online', 'inv']):
                all_kits.extend(self._parse_kits_sheet(df, sheet))
                continue

            sheet_brand = self._infer_brand_for_sheet(sheet)
            active_map: Optional[Dict[str, Any]] = None
            is_dop_section = False
            is_discontinued_section = False
            last_dim_str: Optional[str] = None
            last_weight_val: Optional[float] = None

            row_idx = 0
            while row_idx < len(df):
                row = df.iloc[row_idx]
                row_texts = [str(v).strip() for v in row.values if pd.notna(v) and str(v).strip()]
                row_joined = ' '.join(row_texts)
                row_joined_low = row_joined.lower()

                # 1. Проверяем, не является ли текущая строка новой шапкой таблицы
                new_map = self._detect_header_mapping(df, row_idx)
                if new_map is not None:
                    active_map = new_map
                    is_dop_section = False
                    last_dim_str = None
                    last_weight_val = None
                    row_idx += 1
                    continue

                # 2. Проверяем секционные баннеры (например, "СКЛАДСКИЕ ОСТАТКИ" в ABSolite или "Дополнительное оборудование:" в VEKTOR)
                if 'складские остатки' in row_joined_low or 'снято с производства' in row_joined_low:
                    is_discontinued_section = True
                if 'дополнительное оборудование' in row_joined_low:
                    is_dop_section = True
                    row_idx += 1
                    continue

                # Если шапка таблицы еще не найдена — пропускаем вводную шапку листа
                if active_map is None:
                    row_idx += 1
                    continue

                # 3. Извлекаем значения из текущей строки по динамической карте колонок
                def _get(key: str) -> Any:
                    c_i = active_map.get(key)
                    return row.iloc[c_i] if (c_i is not None and c_i < len(row)) else None

                raw_code = _get('code')
                raw_model = _get('model')
                raw_name = _get('full_name') or _get('name') or raw_model
                raw_desc = _get('desc')
                raw_note = _get('note')

                # Если в блоке без шапки ('Дополнительное оборудование') название сдвинулось в соседнюю колонку
                if pd.isna(raw_name) and is_dop_section:
                    for c_scan in range(min(4, len(row))):
                        if pd.notna(row.iloc[c_scan]) and isinstance(row.iloc[c_scan], str) and len(row.iloc[c_scan].strip()) > 5:
                            raw_name = row.iloc[c_scan]
                            break

                if pd.isna(raw_name) and pd.isna(raw_model):
                    row_idx += 1
                    continue

                name_str = str(raw_name if pd.notna(raw_name) else raw_model).strip().replace('\n', ' ')
                model_str = str(raw_model if pd.notna(raw_model) else name_str).strip().replace('\n', ' ')

                # Фильтруем подзаголовки серий и служебные строки
                if (
                    not name_str
                    or name_str.lower() in ['nan', '-', 'модель', 'наименование', 'тип']
                    or name_str.lower().startswith(('конфигурация ', 'универсальный ибп серии', 'внешние батарейные модули', 'подробнее о серии'))
                ):
                    row_idx += 1
                    continue

                # Извлекаем все ценовые уровни
                p_rrc = clean_num(_get('price_rrc'))
                p_part = clean_num(_get('price_partner'))
                p_sub = clean_num(_get('price_subdist'))
                p_int = clean_num(_get('price_integrator'))
                p_gen = clean_num(_get('price_generic'))

                # В блоках без шапки ('Дополнительное оборудование') цена может стоять в колонке партнёрской или соседней цены
                if p_rrc is None and p_part is None and p_gen is None and is_dop_section:
                    for c_scan in range(2, len(row)):
                        cand_p = clean_num(row.iloc[c_scan])
                        if cand_p is not None and cand_p >= 50:
                            p_gen = cand_p
                            break

                on_req = any(
                    is_price_on_request(_get(k))
                    for k in ['price_rrc', 'price_partner', 'price_subdist', 'price_generic']
                )

                main_price = p_rrc or p_gen or p_part or p_sub or p_int
                if main_price is None and not on_req:
                    row_idx += 1
                    continue

                # Определяем валюту строки (с учетом особенностей HIDEN, где опции бывают в рублях > 50000)
                curr = active_map['currency']
                if self.supplier == 'HIDEN' and main_price and main_price > 50000:
                    curr = 'RUB'

                # Формируем JSON ценовых уровней
                tiers: Dict[str, Any] = {}
                if p_sub is not None:
                    tiers['subdistributor'] = p_sub
                if p_int is not None:
                    tiers['integrator'] = p_int
                if p_part is not None:
                    tiers['partner_rub' if curr == 'RUB' else 'partner_usd'] = p_part
                if p_rrc is not None:
                    tiers['rrc_rub' if curr == 'RUB' else 'rrc_usd'] = p_rrc
                if p_gen is not None and not tiers:
                    tiers['rrc_rub' if curr == 'RUB' else 'rrc_usd'] = p_gen
                if self.supplier == 'VEDA MC' and main_price is not None:
                    tiers['list_price_ue_no_vat'] = main_price
                    tiers['partner_50pct_ue_no_vat'] = round(main_price * 0.5, 2)
                if 'unlabeled_right_col' in active_map:
                    unl_val = clean_num(row.iloc[active_map['unlabeled_right_col']])
                    if unl_val is not None:
                        tiers['unlabeled_ref_col'] = unl_val

                # Статус жизненного цикла (актуальный = None; остатки/снято с производства = явная метка)
                note_str = str(raw_note).strip().replace('\n', ' ') if pd.notna(raw_note) else ''
                lifecycle = None
                if is_discontinued_section or 'снято с производства' in note_str.lower() or 'остатк' in note_str.lower():
                    lifecycle = 'Снято с производства (складские остатки)'

                # Извлечение технических характеристик (мощность, ток/емкость, напряжение, габариты, вес)
                desc_str = str(raw_desc).strip().replace('\n', ' ') if pd.notna(raw_desc) else ''
                pwr_raw = _get('power')
                power_kw = None
                if pwr_raw is not None and pd.notna(pwr_raw):
                    s_p = str(pwr_raw).replace(',', '.')
                    m_both = re.search(r'(\d+(?:\.\d+)?)\s*кв[аa]\s*/\s*(\d+(?:\.\d+)?)\s*квт', s_p, re.IGNORECASE)
                    if m_both:
                        power_kw = float(m_both.group(2))
                    else:
                        p_num = clean_num(s_p)
                        if p_num is not None:
                            # Если мощность в Вт (например, в таблице Deye: 5000 Вт -> 5.0 кВт)
                            h_pwr = active_map['raw_headers'][active_map['power']]
                            power_kw = round(p_num / 1000.0, 3) if ('вт' in h_pwr and 'квт' not in h_pwr and p_num >= 100) else p_num

                if power_kw is None:
                    m_kw_txt = re.search(r'(\d+(?:[.,]\d+)?)\s*квт', f"{name_str} {desc_str}", re.IGNORECASE)
                    if m_kw_txt:
                        power_kw = float(m_kw_txt.group(1).replace(',', '.'))

                cap_val = clean_num(_get('capacity'))
                curr_val = clean_num(_get('current'))
                rated_a = cap_val if cap_val is not None else curr_val

                volt_val = clean_num(_get('voltage'))
                if volt_val == 3.0:
                    volt_val = 400.0

                # Габариты (поддержка объединенных ячеек в VEDA и отдельных колонок Длина/Ширина/Высота в Ventura)
                if active_map.get('dim_l') is not None:
                    dims = {
                        'length_mm': clean_num(_get('dim_l')),
                        'width_mm': clean_num(_get('dim_w')),
                        'height_mm': clean_num(_get('dim_h')),
                    }
                else:
                    raw_d = _get('dims')
                    if pd.notna(raw_d):
                        last_dim_str = str(raw_d).strip()
                    dims = parse_dimensions(last_dim_str, order='HxWxD' if self.supplier == 'VEDA MC' else 'WxDxH')

                w_val = clean_num(_get('weight'))
                if w_val is not None:
                    last_weight_val = w_val

                # Декодирование типовых кодов VEDA при наличии
                ip_rat = 'IP20'
                extra_specs: Dict[str, Any] = {'sheet_category': sheet.strip()}
                if self.supplier == 'VEDA MC':
                    d_kw, d_a, d_v, d_ip, d_sp = decode_veda_model_code(model_str)
                    power_kw = power_kw or d_kw
                    rated_a = rated_a or d_a
                    volt_val = volt_val or d_v
                    ip_rat = d_ip
                    extra_specs.update(d_sp)

                e_class, clean_title, sub_type = self._classify_etim_and_title(name_str, model_str, sheet, desc_str)
                if sub_type:
                    extra_specs['sub_type'] = sub_type
                if desc_str:
                    extra_specs['description'] = desc_str
                if note_str:
                    extra_specs['note'] = note_str

                sku_final = self._make_sku(
                    brand=sheet_brand,
                    sheet_name=sheet,
                    code_val=raw_code,
                    model_val=model_str,
                    name_val=clean_title,
                    is_dop=is_dop_section,
                )

                all_items.append({
                    'supplier': self.supplier,
                    'brand': sheet_brand,
                    'etim_class': e_class,
                    'sku': sku_final,
                    'model_code': model_str[:85],
                    'product_name': clean_title,
                    'base_price': main_price,
                    'currency_code': curr,
                    'vat_included': active_map['vat_included'],
                    'price_on_request': on_req,
                    'lifecycle_status': lifecycle,
                    'price_tiers': json.dumps(tiers, ensure_ascii=False),
                    'power_kw': power_kw,
                    'rated_current_a': rated_a,
                    'voltage_v': volt_val,
                    'ip_rating': ip_rat,
                    'weight_kg': last_weight_val,
                    'dimensions_mm': json.dumps(dims, ensure_ascii=False) if dims else None,
                    'specifications': json.dumps(extra_specs, ensure_ascii=False),
                })
                row_idx += 1

        # Если для VEDA есть сводный лист 'код-цена', объединяем его со всеми детальными карточками
        if veda_master_prices:
            detailed_by_sku = {it['sku']: it for it in all_items}
            merged_veda = []
            for sku_k, master_it in veda_master_prices.items():
                if sku_k in detailed_by_sku:
                    det = detailed_by_sku.pop(sku_k)
                    det['base_price'] = master_it['base_price']
                    det['price_tiers'] = master_it['price_tiers']
                    merged_veda.append(det)
                else:
                    merged_veda.append(master_it)
            # Добавляем позиции, которые были на профильных листах, но отсутствовали в 'код-цена'
            merged_veda.extend(detailed_by_sku.values())
            all_items = merged_veda

        return all_items, all_cross_refs, all_kits


# ==============================================================================
# 3. ФУНКЦИЯ ПОЛНОЙ СБОРКИ И ОБНОВЛЕНИЯ БАЗЫ ДАННЫХ
# ==============================================================================
def _find_file_by_prefix(prefix: str) -> Optional[str]:
    matches = sorted(glob.glob(f"{prefix}*.xls*"))
    return matches[0] if matches else None


def rebuild_master_database(db_path: str = 'infra_equipment.db') -> pd.DataFrame:
    """Прогоняет все прайс-листы поставщиков через универсальный ETL и обновляет SQLite + CSV."""
    suppliers_config = [
        ('Ventura', 'Ventura', 'USD', True, '2026-01 Прайслист Ventura'),
        ('VEKTOR Energy', 'VEKTOR', 'RUB', True, 'Прайс-лист общий VEKTOR'),
        ('STARK UPS', 'STARK Country', 'USD', True, 'STARK UPS Прайс'),
        ('HIDEN', 'HIDEN', 'USD', True, 'Прайс_лист_СУБДИСТРИБЬЮТОР_HIDEN'),
        ('Kehua', 'Kehua', 'USD', True, 'прайс-лист ИБП Kehua'),
        ('ABSolite', 'ABSolite', 'USD', True, '20260129_ABSolite_UPS_price'),
        ('VEDA MC', 'VEDA', 'CNY_VEDA', False, 'Прайс-лист ВЕДА МК'),
    ]

    all_items: List[Dict[str, Any]] = []
    all_cross: List[Dict[str, Any]] = []
    all_kits: List[Dict[str, Any]] = []

    for sup_name, brand_name, curr, vat_inc, file_prefix in suppliers_config:
        fpath = _find_file_by_prefix(file_prefix)
        if not fpath:
            print(f"  [!] Файл по шаблону '{file_prefix}*' не найден, пропуск.")
            continue
        print(f"  [ETL] Обработка {sup_name} -> '{os.path.basename(fpath)}'...")
        etl = UniversalSupplierETL(
            supplier_name=sup_name,
            default_brand=brand_name,
            default_currency=curr,
            default_vat_included=vat_inc,
        )
        items, cross, kits = etl.parse_excel_workbook(fpath)
        print(f"        Извлечено: {len(items)} товаров, {len(cross)} кросс-замен, {len(kits)} строк комплектов.")
        all_items.extend(items)
        all_cross.extend(cross)
        all_kits.extend(kits)

    df_master = pd.DataFrame(all_items)
    df_master = df_master.drop_duplicates(subset=['sku'], keep='first').reset_index(drop=True)

    # Пересчет рублевых цен с НДС 22% (по актуальным курсам ЦБ РФ / БД)
    RATES = {'RUB': 1.0, 'USD': 82.6617, 'EUR': 92.6968, 'CNY_VEDA': 12.3907}
    if os.path.exists(db_path):
        try:
            conn_r = sqlite3.connect(db_path)
            cur_r = conn_r.cursor()
            cur_r.execute("SELECT currency_code, effective_rate_to_rub FROM exchange_rates")
            for c_code, eff_r in cur_r.fetchall():
                RATES[c_code] = float(eff_r)
            conn_r.close()
        except Exception:
            pass

    df_master['rrc_price_rub_with_vat'] = df_master.apply(
        lambda r: (
            round(
                float(r['base_price'])
                * RATES.get(r['currency_code'], 1.0)
                * (1.0 if r['vat_included'] else 1.22),
                2,
            )
            if pd.notna(r['base_price'])
            else np.nan
        ),
        axis=1,
    )

    df_kits = pd.DataFrame(all_kits)
    df_cross = pd.DataFrame(all_cross).drop_duplicates()

    df_master.to_csv('infra_equipment_master_db.csv', index=False, encoding='utf-8-sig')
    df_kits.to_csv('infra_kits_bom.csv', index=False, encoding='utf-8-sig')
    df_cross.to_csv('infra_cross_references.csv', index=False, encoding='utf-8-sig')

    conn = sqlite3.connect(db_path)
    df_master.to_sql('equipment_items', conn, if_exists='replace', index=False)
    df_cross.to_sql('equipment_cross_references', conn, if_exists='replace', index=False)
    df_kits.to_sql('equipment_kits_bom', conn, if_exists='replace', index=False)
    conn.close()

    return df_master


if __name__ == '__main__':
    try:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass

    print("=== ЗАПУСК УНИВЕРСАЛЬНОГО ETL-КОНВЕЙЕРА ООО «НИЦ «ИНФРА» v2.0 ===")
    df_res = rebuild_master_database('infra_equipment.db')

    cabinets = df_res[df_res['specifications'].str.contains('battery_cabinet', na=False)]
    discontinued = df_res[df_res['lifecycle_status'].notna()]
    on_request = df_res[df_res['price_on_request'] == True]

    print("\n" + "=" * 70)
    print("ИТОГОВАЯ СВОДКА ОБНОВЛЁННОЙ БАЗЫ ДАННЫХ:")
    print(f"• Всего уникальных позиций (SKU): {len(df_res)}")
    print(f"• Аккумуляторных шкафов / кабинетов / стеллажей: {len(cabinets)} шт.")
    print(f"• Позиций со статусом 'Снято с производства (складские остатки)': {len(discontinued)} шт.")
    print(f"• Позиций со статусом цены 'По запросу': {len(on_request)} шт.")
    print("• Распределение по поставщикам:", df_res['supplier'].value_counts().to_dict())
    print("• Распределение по классам ETIM:", df_res['etim_class'].value_counts().to_dict())
    print("=" * 70)