import os
import re
import math
import json
import base64
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional

from config import DB_PATH, DRAWINGS_DIR, get_llm_client


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ==============================================================================
# 1. ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ БАЗЫ ЗНАНИЙ ПАСПОРТОВ И ПРАВИЛ СОВМЕСТИМОСТИ
# ==============================================================================
def init_knowledge_tables():
    """Создает таблицы паспортов оборудования, правил совместимости и чанков RAG."""
    conn = get_db_connection()
    cur = conn.cursor()

    # Структурированные инженерные параметры из PDF-даташитов
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equipment_datasheets (
        datasheet_id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_code TEXT UNIQUE NOT NULL,
        brand TEXT NOT NULL,
        equipment_type TEXT NOT NULL,       -- 'MONOBLOCK_UPS', 'MODULAR_UPS', 'LIFEPO4_CABINET', 'BATTERY_CABINET'
        power_kva_min REAL,
        power_kva_max REAL,
        power_factor REAL DEFAULT 1.0,
        efficiency_online REAL DEFAULT 0.95,
        dc_bus_voltage_v REAL,
        required_battery_count INTEGER,     -- Например, 40 шт. по 12В для HE3300XL
        max_slots INTEGER,                  -- Количество слотов силовых модулей (для HEM)
        module_power_kva REAL,              -- Мощность одного силового модуля (10 или 15 кВА)
        dimensions_mm TEXT,
        weight_kg REAL,
        specs_json TEXT,
        source_pdf TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Правила обязательной конструктивной обвязки (BOM Rules)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equipment_compatibility_rules (
        rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger_series TEXT NOT NULL,
        condition_expr TEXT NOT NULL,
        required_component_code TEXT NOT NULL,
        required_component_name TEXT NOT NULL,
        quantity_formula TEXT NOT NULL,     -- Например: '1', 'ceil(power_kw / 10)', '40'
        reason TEXT
    )
    """)

    # Фрагменты технической документации (Markdown-таблицы + метаданные для RAG)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_code TEXT,
        brand TEXT,
        doc_title TEXT,
        section_title TEXT,
        content_md TEXT NOT NULL,
        metadata_json TEXT
    )
    """)

    conn.commit()
    conn.close()


# ==============================================================================
# 2. ТАБЛИЧНО-ОРИЕНТИРОВАННЫЙ ЧАНКИНГ В MARKDOWN (ПО МЕТОДИКЕ ГЛ. 18 КНИГИ)
# ==============================================================================
def chunk_markdown_document(
    markdown_text: str,
    series_code: str,
    brand: str,
    doc_title: str,
    max_chunk_chars: int = 1400,
) -> List[Dict[str, Any]]:
    """
    Разбивает технический документ на семантические фрагменты с сохранением
    целостности Markdown-таблиц и привязкой метаданных к каждому чанку.
    """
    blocks = re.split(r"\n(?=#{1,3}\s)", markdown_text.strip())
    chunks = []

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        section_title = lines[0].lstrip("#").strip() if lines[0].startswith("#") else "Общие характеристики"

        if len(block) <= max_chunk_chars:
            chunks.append({
                "series_code": series_code,
                "brand": brand,
                "doc_title": doc_title,
                "section_title": section_title,
                "content_md": block.strip(),
                "metadata_json": json.dumps(
                    {"series": series_code, "brand": brand, "section": section_title},
                    ensure_ascii=False,
                ),
            })
        else:
            # Режем по абзацам/строкам, не разрывая строки Markdown-таблицы
            current_lines = []
            current_len = 0
            table_header = []

            for line in lines:
                if line.strip().startswith("|") and len(table_header) < 2:
                    table_header.append(line)

                if current_len + len(line) > max_chunk_chars and current_lines:
                    chunks.append({
                        "series_code": series_code,
                        "brand": brand,
                        "doc_title": doc_title,
                        "section_title": section_title,
                        "content_md": "\n".join(current_lines).strip(),
                        "metadata_json": json.dumps(
                            {"series": series_code, "brand": brand, "section": section_title},
                            ensure_ascii=False,
                        ),
                    })
                    # Если разрыв произошел внутри таблицы, дублируем шапку таблицы в новый чанк
                    if line.strip().startswith("|") and len(table_header) == 2:
                        current_lines = list(table_header)
                        current_len = sum(len(x) for x in current_lines)
                    else:
                        current_lines = []
                        current_len = 0

                current_lines.append(line)
                current_len += len(line) + 1

            if current_lines:
                chunks.append({
                    "series_code": series_code,
                    "brand": brand,
                    "doc_title": doc_title,
                    "section_title": section_title,
                    "content_md": "\n".join(current_lines).strip(),
                    "metadata_json": json.dumps(
                        {"series": series_code, "brand": brand, "section": section_title},
                        ensure_ascii=False,
                    ),
                })
    return chunks


# ==============================================================================
# 3. ГИБРИДНЫЙ РЕТРИВЕР (BM25 + TF-IDF ВЕКТОРНОЕ СХОДСТВО + МЕТАДАННЫЕ)
# ==============================================================================
class HybridKnowledgeRetriever:
    """
    Реализует паттерн EnsembleRetriever:
    объединяет точный лексический поиск по артикулам/терминам (BM25)
    и семантическое векторное взвешивание (TF-IDF Cosine) + фильтр метаданных.
    """

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        tokens = re.findall(r"[a-zа-яё0-9-]+", text.lower())
        # Легкая нормализация русских окончаний для повышения полноты поиска
        return [t[:-1] if (len(t) > 5 and t[-1] in "ьияеаыоую") else t for t in tokens]

    def search(
        self,
        query: str,
        brand_filter: Optional[str] = None,
        series_filter: Optional[str] = None,
        top_k: int = 4,
    ) -> List[Dict[str, Any]]:
        init_knowledge_tables()
        conn = get_db_connection()
        cur = conn.cursor()

        sql = "SELECT * FROM knowledge_chunks WHERE 1=1"
        params = []
        if brand_filter:
            sql += " AND LOWER(brand) = LOWER(?)"
            params.append(brand_filter)
        if series_filter:
            sql += " AND LOWER(series_code) LIKE LOWER(?)"
            params.append(f"%{series_filter}%")

        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        if not rows:
            return []

        q_tokens = self._tokenize(query)
        if not q_tokens:
            return rows[:top_k]

        docs_tokens = [self._tokenize(f"{r['section_title']} {r['content_md']}") for r in rows]
        N = len(docs_tokens)
        avgdl = sum(len(d) for d in docs_tokens) / max(N, 1)

        # Подсчет DF для BM25 и IDF
        df = Counter()
        for doc in docs_tokens:
            for term in set(doc):
                df[term] += 1

        k1, b = 1.5, 0.75
        scored_docs = []

        for idx, doc in enumerate(docs_tokens):
            doc_len = len(doc)
            tf_counter = Counter(doc)

            # 1. Оценка BM25
            bm25_score = 0.0
            for term in q_tokens:
                if term in tf_counter:
                    idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1.0)
                    tf = tf_counter[term]
                    denom = tf + k1 * (1.0 - b + b * (doc_len / max(avgdl, 1.0)))
                    bm25_score += idf * ((tf * (k1 + 1.0)) / denom)

            # 2. Векторное косинусное сходство TF-IDF
            q_vec = {t: math.log(N / (df[t] + 1) + 1.0) for t in set(q_tokens)}
            d_vec = {t: tf_counter[t] * math.log(N / (df[t] + 1) + 1.0) for t in set(doc)}
            dot = sum(q_vec[t] * d_vec.get(t, 0.0) for t in q_vec)
            norm_q = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0
            norm_d = math.sqrt(sum(v * v for v in d_vec.values())) or 1.0
            cosine_score = dot / (norm_q * norm_d)

            # Гибридный ранг (60% BM25 + 40% Cosine)
            hybrid_score = round(0.6 * bm25_score + 4.0 * cosine_score, 4)
            row_copy = dict(rows[idx])
            row_copy["relevance_score"] = hybrid_score
            scored_docs.append(row_copy)

        scored_docs.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored_docs[:top_k]


# ==============================================================================
# 4. МУЛЬТИМОДАЛЬНЫЙ ПАРСЕР PDF-ПАСПОРТОВ И ЧЕРТЕЖЕЙ (VLM / TEXT)
# ==============================================================================
def ingest_datasheet_with_vlm(
    file_path: str,
    series_code: str,
    brand: str,
    prefer_local_ollama: bool = False,
) -> Dict[str, Any]:
    """
    Принимает PDF-паспорт или изображение чертежа/таблицы, извлекает через VLM
    структурированные Markdown-таблицы характеристик и индексирует в базу знаний RAG.
    """
    init_knowledge_tables()
    if not os.path.exists(file_path):
        return {"status": "ERROR", "message": f"Файл '{file_path}' не найден."}

    ext = os.path.splitext(file_path)[1].lower()
    extracted_md = ""

    # Если передано изображение чертежа/таблицы (.png, .jpg) — отправляем в мультимодальную VLM
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        client, model_name, prov_name = get_llm_client(prefer_local_ollama=prefer_local_ollama)
        with open(file_path, "rb") as img_f:
            b64_img = base64.b64encode(img_f.read()).decode("utf-8")

        prompt = (
            f"Ты — инженер-проектировщик систем электропитания. Извлеки все технические характеристики, "
            f"размеры (ШхГхВ), напряжение, мощность и комплектность со страницы паспорта/чертежа серии {series_code} ({brand}). "
            f"Оформи результат строго в виде структурированных заголовков и Markdown-таблиц без лишних комментариев."
        )
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                    ],
                }
            ],
            temperature=0.0,
        )
        extracted_md = resp.choices[0].message.content.strip()
    elif ext == ".pdf":
        # Извлекаем текст PDF (если установлен pypdf) и нормализуем в Markdown
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            pages_txt = [f"## Страница {i+1}\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages)]
            extracted_md = "\n\n".join(pages_txt)
        except ImportError:
            return {"status": "ERROR", "message": "Для чтения PDF установите библиотеку: pip install pypdf"}
    else:
        with open(file_path, "r", encoding="utf-8") as f:
            extracted_md = f.read()

    chunks = chunk_markdown_document(extracted_md, series_code, brand, os.path.basename(file_path))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM knowledge_chunks WHERE series_code = ? AND doc_title = ?", (series_code, os.path.basename(file_path)))
    for ch in chunks:
        cur.execute(
            """
            INSERT INTO knowledge_chunks (series_code, brand, doc_title, section_title, content_md, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ch["series_code"], ch["brand"], ch["doc_title"], ch["section_title"], ch["content_md"], ch["metadata_json"]),
        )
    conn.commit()
    conn.close()
    return {"status": "INDEXED", "series_code": series_code, "chunks_added": len(chunks)}


# ==============================================================================
# 5. ЗАГРУЗКА ЭТАЛОННЫХ ПАСПОРТОВ И ПРАВИЛ КОМПЛЕКТАЦИИ ООО «НИЦ «ИНФРА»
# ==============================================================================
def seed_infra_engineering_datasheets():
    """Записывает в БД проверенные инженерные данные из паспортов HIDEN, MNB и шкафа S17."""
    init_knowledge_tables()
    conn = get_db_connection()
    cur = conn.cursor()

    datasheets = [
        (
            "HE3300XL", "HIDEN", "MONOBLOCK_UPS", 10.0, 40.0, 1.0, 0.95, 480.0, 40, None, None,
            "250x660x530..250x839x770", 52.0,
            json.dumps({
                "models": {
                    "HE33010XL": {"kva": 10, "kw": 10, "pf": 1.0, "size": "250x660x530", "weight": 31},
                    "HE33015XL": {"kva": 15, "kw": 15, "pf": 1.0, "size": "250x660x530", "weight": 31},
                    "HE33020XL": {"kva": 20, "kw": 18, "pf": 0.9, "size": "250x689x770", "weight": 52},
                    "HE33030XL": {"kva": 30, "kw": 27, "pf": 0.9, "size": "250x689x770", "weight": 52},
                    "HE33040XL": {"kva": 40, "kw": 36, "pf": 0.9, "size": "250x839x770", "weight": 61},
                },
                "dc_bus": "±240 В DC (40 АКБ по 12 В последовательно)",
                "snmp_slot": "Внутренний интеллектуальный слот (карта HIDEN SHE8M)",
            }, ensure_ascii=False),
            "Technical_description_HIDEN_EXPERT_HE_3300XL_10-40_kVA.pdf",
        ),
        (
            "HEM_10-90X", "HIDEN", "MODULAR_UPS", 20.0, 90.0, 1.0, 0.95, 480.0, 40, 6, 10.0,
            "630x900x1230", 85.0,
            json.dumps({
                "frames": {
                    "HEM020-10X": {"max_kva": 20, "slots": 2, "module": "HEPM10X"},
                    "HEM040-10X": {"max_kva": 40, "slots": 4, "module": "HEPM10X"},
                    "HEM060-10X": {"max_kva": 60, "slots": 6, "module": "HEPM10X", "size": "630x900x1230", "weight": 85},
                    "HEM090-15X": {"max_kva": 90, "slots": 6, "module": "HEPM15X", "size": "630x900x1230", "weight": 85},
                },
                "charger_module": "CHG15A-R-HEM10-90 (15А для емких массивов АКБ и LiFePO4)",
                "supported_batteries": ["AGM VRLA", "GEL", "LiFePO4 512V"],
            }, ensure_ascii=False),
            "Technical_description_HEM_10-90X.pdf",
        ),
        (
            "LFPT-512-200-S", "MNB", "LIFEPO4_CABINET", None, None, 1.0, 0.98, 512.0, 1, None, None,
            "600x900x2000", 970.0,
            json.dumps({
                "nominal_voltage_v": 512,
                "capacity_ah": 200,
                "nominal_energy_kwh": 102.4,
                "usable_energy_kwh": 97.28,
                "max_discharge_a": 200,
                "max_charge_a": 100,
                "cycles": ">3000 циклов @ 0.2C 100% DoD",
                "bms": "Встроенная BMS, протоколы RS485/RS232/CAN",
            }, ensure_ascii=False),
            "Datasheet_MNB_LFPT_512-200-S (new).pdf",
        ),
        (
            "S17", "HIDEN", "BATTERY_CABINET", None, None, 1.0, 1.0, 480.0, 40, 4, None,
            "900x610x1300", 65.0,
            json.dumps({
                "tiers": 4,
                "max_batteries_12v": 40,
                "compatible_ah": "от 18 Ач до 40 Ач (40 шт. по 12 В)",
            }, ensure_ascii=False),
            "Шкаф аккумуляторный S17.pdf",
        ),
    ]

    for ds in datasheets:
        cur.execute("""
        INSERT OR REPLACE INTO equipment_datasheets
        (series_code, brand, equipment_type, power_kva_min, power_kva_max, power_factor,
         efficiency_online, dc_bus_voltage_v, required_battery_count, max_slots, module_power_kva,
         dimensions_mm, weight_kg, specs_json, source_pdf)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ds)

    # Правила обязательной комплектации (BOM)
    cur.execute("DELETE FROM equipment_compatibility_rules")
    rules = [
        ("HE3300XL", "always", "SHE8M", "SNMP карта HIDEN SHE8M для ИБП серии UDC92, HE, HR, HEM", "1", "Обязательный мониторинг в слот ИБП"),
        ("HE3300XL", "external_agm", "JUMPERS_40", "Комплект перемычек для ИБП (на 40 АКБ)", "1", "Соединение последовательной цепи 40 АКБ"),
        ("HE3300XL", "external_agm", "S17", "Шкаф аккумуляторный напольный S17 (ШГВ 90х61х130см)", "1", "Размещение 40 АКБ на 4 ярусах"),
        ("HEM_10-90X", "lifepo4_or_high_ah", "CHG15A-R-HEM10-90", "Модуль зарядного устройства 15А для ИБП серии HEM10-90 (CHG15A-R-HEM10-90)", "1", "Усиленный заряд для LiFePO4 200 Ач"),
    ]
    for r in rules:
        cur.execute("""
        INSERT INTO equipment_compatibility_rules
        (trigger_series, condition_expr, required_component_code, required_component_name, quantity_formula, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """, r)

    # Индексируем Markdown-таблицы из паспортов в knowledge_chunks
    docs_to_index = [
        (
            "HE3300XL", "HIDEN", "Technical_description_HIDEN_EXPERT_HE_3300XL_10-40_kVA.pdf",
            """# Технические характеристики трёхфазных онлайн-ИБП HIDEN EXPERT HE3300XL (10–40 кВА)
| Модель ИБП | Полная мощность | Активная мощность | Коэфф. мощности (PF) | Кол-во внешних АКБ | Шина DC | Габариты (ШхГхВ), мм | Вес, кг |
|---|---|---|---|---|---|---|---|
| HE33010XL | 10 кВА | 10 кВт | 1.0 | 40 шт. (12В) | ±240 В DC | 250x660x530 | 31 |
| HE33015XL | 15 кВА | 15 кВт | 1.0 | 40 шт. (12В) | ±240 В DC | 250x660x530 | 31 |
| HE33020XL | 20 кВА | 18 кВт | 0.9 | 40 шт. (12В) | ±240 В DC | 250x689x770 | 52 |
| HE33030XL | 30 кВА | 27 кВт | 0.9 | 40 шт. (12В) | ±240 В DC | 250x689x770 | 52 |
| HE33040XL | 40 кВА | 36 кВт | 0.9 | 40 шт. (12В) | ±240 В DC | 250x839x770 | 61 |
КПД в режиме работы от электросети: 95%. Интеллектуальный слот для карты SNMP HIDEN SHE8M."""
        ),
        (
            "HEM_10-90X", "HIDEN", "Technical_description_HEM_10-90X.pdf",
            """# Модульные ИБП HIDEN EXPERT HEM 10-90X (PF=1, 3ф/3ф)
| Силовой шкаф (фрейм) | Макс. мощность | Кол-во слотов | Силовой модуль | Поддержка АКБ | Габариты шкафа, мм |
|---|---|---|---|---|---|
| HEM020/10X | 20 кВА / 20 кВт | 2 | HEPM10X (10 кВА) | AGM / GEL / LiFePO4 | 485x697x398 |
| HEM040/10X | 40 кВА / 40 кВт | 4 | HEPM10X (10 кВА) | AGM / GEL / LiFePO4 | 485x699x575 |
| HEM060-10X | 60 кВА / 60 кВт | 6 | HEPM10X (10 кВА) | AGM / GEL / LiFePO4 | 630x900x1230 |
| HEM090/15X | 90 кВА / 90 кВт | 6 | HEPM15X (15 кВА) | AGM / GEL / LiFePO4 | 630x900x1230 |
При использовании емких литиевых батарей LiFePO4 512В устанавливается модуль зарядного устройства CHG15A-R-HEM10-90 (15А)."""
        ),
        (
            "LFPT-512-200-S", "MNB", "Datasheet_MNB_LFPT_512-200-S (new).pdf",
            """# Высоковольтная литий-железофосфатная батарея MNB LFPT-512-200-S (LiFePO4 с BMS)
| Параметр | Значение |
|---|---|
| Номинальное напряжение | 512 В DC (160 ячеек последовательно) |
| Номинальная емкость / Энергия | 200 Ач / 102,4 кВт·ч (полезная энергия 97,28 кВт·ч) |
| Постоянный ток разряда / заряд | 200 А (до 100 кВт) / Стандартный заряд 40 А (макс. 100 А) |
| Ресурс и срок службы | >3000 циклов @ 0.2C (100% DoD), срок службы +15 лет |
| Габариты и масса шкафа | 600x900x2000 мм, вес 970 кг, защита IP50 |"""
        ),
    ]

    cur.execute("DELETE FROM knowledge_chunks")
    for s_code, br, d_title, md_txt in docs_to_index:
        for ch in chunk_markdown_document(md_txt, s_code, br, d_title):
            cur.execute(
                """
                INSERT INTO knowledge_chunks (series_code, brand, doc_title, section_title, content_md, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ch["series_code"], ch["brand"], ch["doc_title"], ch["section_title"], ch["content_md"], ch["metadata_json"]),
            )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed_infra_engineering_datasheets()
    retriever = HybridKnowledgeRetriever()
    print("=== ТЕСТ ГИБРИДНОГО РЕТРИВЕРА БАЗЫ ЗНАНИЙ ПАСПОРТОВ ===")
    res = retriever.search("модульный ИБП 60 кВА зарядное устройство LiFePO4", top_k=2)
    for r in res:
        print(f"\n[Серия: {r['series_code']} | Релевантность: {r['relevance_score']}]\n{r['content_md']}")