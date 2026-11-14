import os
import re
import json
from typing import Any, Dict, List, Optional
import pandas as pd
from docx import Document

from multi_agent_hierarchy import ConductorSupervisor

INBOX_TZ_DIR = "inbox_tz"


class CustomerTZIngestor:
    """
    Универсальный парсер входящих технических заданий (ТЗ) и опросных листов заказчика.
    Поддерживает:
      1) Таблицы Excel / CSV (.xlsx, .xls, .csv)
      2) Документы Word (.docx — таблицы и нумерованные списки)
      3) Текстовые письма и сообщения из CRM Битрикс24 (str / .txt)
    """

    BRAND_LIST = ["VEDA", "Danfoss", "HIDEN", "Kehua", "ABSolite", "STARK", "Ventura", "VEKTOR", "Deye"]

    @staticmethod
    def parse_engineering_line(text: str, explicit_qty: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Извлекает из одной строки ТЗ структурированный словарь ETIM-параметров для Агента-Инженера.
        """
        clean = str(text).strip()
        if not clean or len(clean) < 3:
            return None

        low = clean.lower()
        # Отсекаем шапки таблиц и строки «Итого»
        skip_exact = {"наименование", "оборудование", "итого", "всего", "примечание", "№ п/п", "позиция", "кол-во"}
        if low in skip_exact or low.startswith("итого ") or low.startswith("всего "):
            return None

        req: Dict[str, Any] = {"raw_text": clean}

        # 1. Количество (из отдельного столбца или из текста вида '— 4 шт.' / '2 компл.')
        if explicit_qty is not None and explicit_qty > 0:
            req["quantity"] = int(explicit_qty)
        else:
            qty_match = re.search(r'(?:[-—–:]|\b)\s*(\d+)\s*(?:шт\.?|ед\.?|компл\.?)\b', low)
            req["quantity"] = int(qty_match.group(1)) if qty_match else 1

        # 2. Заводской код Danfoss (например 132F0001, 132F0020)
        danfoss_match = re.search(r'\b(13\d[A-Z]\d{4})\b', clean, re.IGNORECASE)
        if danfoss_match:
            req["legacy_code"] = danfoss_match.group(1).upper()
            req["etim_class"] = "EC_VFD_MCD"
            return req

        # 3. Прямой артикул (SKU) из нашей базы, если клиент указал его явно
        sku_match = re.search(
            r'\b(VEDA-[A-Z0-9-]+|HIDEN-[А-ЯA-Z0-9-]+|ABSOLITE-\d+|KEHUA-[A-Z0-9-]+|VENTURA-[A-Z0-9-]+|VEKTOR-[A-Z0-9-]+|DEYE-[A-Z0-9-]+)\b',
            clean,
            re.IGNORECASE
        )
        if sku_match:
            req["sku"] = sku_match.group(1)

        # 4. Бренд оборудования
        for b in CustomerTZIngestor.BRAND_LIST:
            if b.lower() in low and b.lower() != "danfoss":
                req["brand"] = b
                break

        # 5. Определение класса ETIM и ключевого слова (приоритет: Опции -> ПЛК -> АКБ -> Инверторы -> ИБП -> УПП -> ПЧ)
        if any(k in low for k in ["дроссел", "синус-фильтр", "синус фильтр", "du/dt", "тормозной резистор", "тормозной модуль", "эмс-фильтр"]):
            req["etim_class"] = "EC_ACCESSORY"
            if "дроссел" in low:
                req["keyword"] = "Дроссель"
            elif "синус" in low:
                req["keyword"] = "Синус-фильтр"
            elif "резистор" in low:
                req["keyword"] = "Тормозной резистор"
            elif "модуль" in low:
                req["keyword"] = "Тормозной модуль"
            else:
                req["keyword"] = "фильтр"
        elif any(k in low for k in ["плк", "контроллер", "панель оператора", "hmi", "процессорн", "модуль расширения"]):
            req["etim_class"] = "EC_PLC_HMI"
            req["keyword"] = "Панель оператора" if ("панель" in low or "hmi" in low) else "Процессорный модуль"
        elif any(k in low for k in ["аккумулятор", "акб", "батаре", "agm", "gel", "lifepo4", "ач", "а·ч", "ah"]):
            req["etim_class"] = "EC_BATTERY"
        elif any(k in low for k in ["инвертор", "гибридн", "deye", "sun-"]):
            req["etim_class"] = "EC_INVERTER"
        elif any(k in low for k in ["ибп", "бесперебойн", "ups", "kr33", "ulps", "smart hs", "country"]):
            req["etim_class"] = "EC_UPS"
        elif any(k in low for k in ["плавного пуска", "упп", "mcd", "vm-"]):
            req["etim_class"] = "EC_VFD_MCD"
            req["keyword"] = "Устройство плавного пуска"
        elif any(k in low for k in ["частот", "пч", "привод", "vfd", "vf-"]):
            req["etim_class"] = "EC_VFD_MCD"
            req["keyword"] = "Преобразователь частоты"

        # 6. Извлечение мощности (кВт / Вт / кВА)
        pwr_kw = re.search(r'(\d+(?:[.,]\d+)?)\s*квт', low)
        if pwr_kw:
            req["power_kw"] = float(pwr_kw.group(1).replace(",", "."))
        else:
            pwr_w = re.search(r'(\d+(?:[.,]\d+)?)\s*вт\b', low)
            if pwr_w and req.get("etim_class") != "EC_BATTERY":
                req["power_kw"] = round(float(pwr_w.group(1).replace(",", ".")) / 1000.0, 2)
            else:
                pwr_kva = re.search(r'(\d+(?:[.,]\d+)?)\s*ква', low)
                if pwr_kva:
                    kva_val = float(pwr_kva.group(1).replace(",", "."))
                    req["power_kw"] = round(kva_val * 0.9, 2) if kva_val >= 5 else kva_val

        # 7. Емкость АКБ (Ач / Ah) или номинальный ток (А)
        ah_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:ач|а·ч|ah)\b', low)
        if ah_match:
            req["rated_current_a"] = float(ah_match.group(1).replace(",", "."))
        else:
            amp_match = re.search(r'(\d+(?:[.,]\d+)?)\s*а\b', low)
            if amp_match and "кв" not in low[max(0, amp_match.start() - 2):amp_match.end() + 2]:
                req["rated_current_a"] = float(amp_match.group(1).replace(",", "."))

        # 8. Напряжение питания (В / V)
        volt_match = re.search(r'(?:3\s*[xх×]\s*)?(\d+(?:[.,]\d+)?)\s*(?:в|v)\b', low)
        if volt_match:
            v_val = float(volt_match.group(1).replace(",", "."))
            if v_val in (380.0, 400.0):
                req["voltage_v"] = 400.0
            elif v_val in (220.0, 230.0):
                req["voltage_v"] = 230.0
            elif v_val in (12.0, 24.0, 48.0, 690.0):
                req["voltage_v"] = v_val

        # 9. Степень защиты корпуса (IP20, IP54, IP65, IP66)
        ip_match = re.search(r'\b(ip\s*\d{2})\b', low)
        if ip_match:
            req["ip_rating"] = ip_match.group(1).upper().replace(" ", "")

        if "etim_class" not in req and "legacy_code" not in req and "sku" not in req:
            return None
        return req

    @classmethod
    def parse_from_text(cls, raw_text: str) -> List[Dict[str, Any]]:
        """Парсит многострочный текст письма, чата или опросного листа."""
        specs = []
        for line in raw_text.strip().splitlines():
            parsed = cls.parse_engineering_line(line)
            if parsed:
                specs.append(parsed)
        return specs

    @classmethod
    def parse_from_excel_or_csv(cls, filepath: str) -> List[Dict[str, Any]]:
        """Парсит таблицу ТЗ заказчика в формате .xlsx, .xls или .csv."""
        if filepath.lower().endswith(".csv"):
            sheets = {"Sheet1": pd.read_csv(filepath)}
        else:
            xls = pd.ExcelFile(filepath)
            sheets = {sh: pd.read_excel(xls, sh) for sh in xls.sheet_names}

        specs = []
        for _, df in sheets.items():
            if df.empty:
                continue

            # Ищем строку шапки, если таблица начинается не с первой строки
            start_row = 0
            for r_idx in range(min(15, len(df))):
                row_str = " ".join(str(v).lower() for v in df.iloc[r_idx].values if pd.notna(v))
                if any(k in row_str for k in ["наименование", "оборудование", "тип", "мощность", "кол-во", "количество"]):
                    df.columns = [str(v).strip() if pd.notna(v) else f"col_{i}" for i, v in enumerate(df.iloc[r_idx].values)]
                    start_row = r_idx + 1
                    break

            qty_col = None
            for col in df.columns:
                if any(k in str(col).lower() for k in ["кол-во", "количество", "qty", "шт"]):
                    qty_col = col
                    break

            sub_df = df.iloc[start_row:] if start_row > 0 else df
            for _, row in sub_df.iterrows():
                explicit_qty = None
                if qty_col and pd.notna(row.get(qty_col)):
                    try:
                        explicit_qty = int(float(str(row[qty_col]).replace(",", ".")))
                    except ValueError:
                        pass

                # Склеиваем текстовые ячейки строки в единое инженерное описание
                text_parts = []
                for col in df.columns:
                    if col == qty_col:
                        continue
                    val = row.get(col)
                    if pd.notna(val):
                        text_parts.append(str(val).strip())
                full_line = " ".join(text_parts)
                parsed = cls.parse_engineering_line(full_line, explicit_qty=explicit_qty)
                if parsed:
                    specs.append(parsed)
        return specs

    @classmethod
    def parse_from_docx(cls, filepath: str) -> List[Dict[str, Any]]:
        """Парсит документ ТЗ заказчика в формате Word (.docx): и таблицы, и списки в тексте."""
        doc = Document(filepath)
        specs = []

        # 1. Сначала извлекаем позиции из всех таблиц документа Word
        for table in doc.tables:
            qty_col_idx = None
            for r_idx, row in enumerate(table.rows):
                cells_txt = [c.text.strip().replace("\n", " ") for c in row.cells]
                joined_low = " ".join(cells_txt).lower()
                if r_idx == 0 and any(k in joined_low for k in ["наименование", "оборудование", "кол-во", "количество"]):
                    for c_i, c_t in enumerate(cells_txt):
                        if any(k in c_t.lower() for k in ["кол-во", "количество", "шт"]):
                            qty_col_idx = c_i
                    continue

                explicit_qty = None
                if qty_col_idx is not None and qty_col_idx < len(cells_txt):
                    m_q = re.search(r'(\d+)', cells_txt[qty_col_idx])
                    if m_q:
                        explicit_qty = int(m_q.group(1))

                line_txt = " ".join(c for i, c in enumerate(cells_txt) if i != qty_col_idx)
                parsed = cls.parse_engineering_line(line_txt, explicit_qty=explicit_qty)
                if parsed:
                    specs.append(parsed)

        # 2. Если таблиц не было (или позиции перечислены списком в абзацах)
        if not specs:
            for p in doc.paragraphs:
                parsed = cls.parse_engineering_line(p.text)
                if parsed:
                    specs.append(parsed)

        return specs

    @classmethod
    def load_any_tz(cls, source_path_or_text: str) -> List[Dict[str, Any]]:
        """Автоматически определяет формат входа (путь к файлу .xlsx/.csv/.docx/.txt или строка текста)."""
        os.makedirs(INBOX_TZ_DIR, exist_ok=True)
        candidate_path = (
            source_path_or_text
            if os.path.exists(source_path_or_text)
            else os.path.join(INBOX_TZ_DIR, source_path_or_text)
        )

        if os.path.isfile(candidate_path):
            ext = os.path.splitext(candidate_path)[1].lower()
            if ext in (".xlsx", ".xls", ".csv"):
                return cls.parse_from_excel_or_csv(candidate_path)
            elif ext == ".docx":
                return cls.parse_from_docx(candidate_path)
            else:
                with open(candidate_path, "r", encoding="utf-8") as f:
                    return cls.parse_from_text(f.read())
        else:
            return cls.parse_from_text(source_path_or_text)


# ==============================================================================
# ТЕСТОВЫЙ СТЕНД: ПРОВЕРКА ЧТЕНИЯ ТЗ ИЗ EXCEL, WORD И СЫРОГО ПИСЬМА КЛИЕНТА
# ==============================================================================
if __name__ == "__main__":
    try:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass

    os.makedirs(INBOX_TZ_DIR, exist_ok=True)
    supervisor = ConductorSupervisor()

    # 1. Создаем тестовый Excel-файл заявки клиента в папке inbox_tz/
    excel_tz_path = os.path.join(INBOX_TZ_DIR, "tz_client_vodokanal.xlsx")
    df_tz = pd.DataFrame([
        {"№": 1, "Наименование оборудования по проекту": "Преобразователь частоты 37 кВт 380В IP20 насосный", "Кол-во, шт.": 2},
        {"№": 2, "Наименование оборудования по проекту": "Преобразователь частоты 15 кВт 380В IP20", "Кол-во, шт.": 3},
        {"№": 3, "Наименование оборудования по проекту": "Устройство плавного пуска 30 кВт 380В", "Кол-во, шт.": 2},
        {"№": 4, "Наименование оборудования по проекту": "Замена частотного привода Danfoss 132F0001", "Кол-во, шт.": 4},
        {"№": 5, "Наименование оборудования по проекту": "ИБП Kehua 18 кВт трехфазный", "Кол-во, шт.": 1},
        {"№": 6, "Наименование оборудования по проекту": "Аккумуляторная батарея Ventura 12В 26Ач", "Кол-во, шт.": 16},
        {"№": 7, "Наименование оборудования по проекту": "Гибридный инвертор Deye 5 кВт", "Кол-во, шт.": 1},
        {"№": 8, "Наименование оборудования по проекту": "Контроллер ПЛК VEDA процессорный модуль", "Кол-во, шт.": 2},
        {"№": 9, "Наименование оборудования по проекту": "Сетевой дроссель для ПЧ", "Кол-во, шт.": 3},
    ])
    df_tz.to_excel(excel_tz_path, index=False)

    print(f"1. Чтение клиентского ТЗ из Excel-файла: {excel_tz_path}")
    parsed_spec = CustomerTZIngestor.load_any_tz(excel_tz_path)
    print(f"   Успешно распознано строк ТЗ: {len(parsed_spec)} шт.")

    # 2. Передаем распознанное ТЗ в Агента-Дирижера и получаем готовый DOCX коммерческого предложения
    supervisor.execute_complex_tender_specification(
        spec_rows=parsed_spec,
        client_name="МУП «Горводоканал г. Новосибирска» (Входящий файл ТЗ)",
        project_name="Автоматический расчет по файлу tz_client_vodokanal.xlsx",
        markup_percent=15.0,
        delivery_percent=2.5,
        output_docx="KP_Auto_From_Excel_TZ.docx"
    )