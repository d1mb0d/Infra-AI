import os
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Cm, Pt, RGBColor
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from config import (
    COMPANY_INFO,
    DB_PATH,
    DEFAULT_FINANCE_PARAMS,
    DRAWINGS_DIR,
    OUTPUT_KP_DIR,
    SIGNATURE_IMAGE_PATH,
)


def _set_cell_shading(cell, color_hex: str):
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color_hex}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _set_cell_margins(cell, top=75, bottom=75, left=110, right=110):
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tcPr.append(tcMar)


def _set_table_borders(table, border_color="CBD5E1"):
    tblPr = table._tbl.tblPr
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'<w:top w:val="single" w:sz="6" w:space="0" w:color="{border_color}"/>'
        f'<w:bottom w:val="single" w:sz="8" w:space="0" w:color="1B365D"/>'
        f'<w:left w:val="single" w:sz="4" w:space="0" w:color="{border_color}"/>'
        f'<w:right w:val="single" w:sz="4" w:space="0" w:color="{border_color}"/>'
        f'<w:insideH w:val="single" w:sz="4" w:space="0" w:color="{border_color}"/>'
        f'<w:insideV w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        f'</w:tblBorders>'
    )
    tblPr.append(borders)


def _fmt_rub(val: float) -> str:
    return f"{val:,.2f}".replace(",", " ").replace(".", ",")


def get_item_from_db(sku: str) -> Dict[str, Any]:
    if not sku or not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM equipment_items WHERE sku = ?", (sku,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else {}


def get_live_rates_from_db() -> Dict[str, float]:
    """Читает актуальные курсы валют из таблицы exchange_rates в infra_equipment.db."""
    default_rates = {"RUB": 1.0, "USD": 82.6617, "EUR": 92.6968, "CNY": 12.3907, "CNY_VEDA": 12.3907}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT currency_code, effective_rate_to_rub FROM exchange_rates")
        rows = cur.fetchall()
        conn.close()
        if rows:
            for r in rows:
                default_rates[r["currency_code"]] = float(r["effective_rate_to_rub"])
    except Exception:
        pass
    return default_rates


def extract_real_partner_cost_rub(item: Dict[str, Any]) -> float:
    """Извлекает входную партнерскую себестоимость с НДС 22% в рублях по актуальному курсу БД."""
    if item.get("partner_cost_rub") is not None:
        return round(float(item["partner_cost_rub"]), 2)

    rrc_rub = float(item.get("rrc_price_rub_with_vat") or 0.0)
    tiers_raw = item.get("price_tiers")
    if not tiers_raw:
        return round(rrc_rub * 0.75, 2)
    tiers = json.loads(tiers_raw) if isinstance(tiers_raw, str) else tiers_raw
    curr = item.get("currency_code", "RUB")

    rates = get_live_rates_from_db()
    rate = rates.get(curr, 1.0)
    vat_mult = 1.0 if item.get("vat_included", True) else 1.22

    if tiers.get("partner_50pct_ue_no_vat"):
        return round(float(tiers["partner_50pct_ue_no_vat"]) * rate * vat_mult, 2)
    if tiers.get("partner_rub"):
        return round(float(tiers["partner_rub"]), 2)
    if tiers.get("subdistributor"):
        return round(float(tiers["subdistributor"]) * rate * vat_mult, 2)
    if tiers.get("partner"):
        return round(float(tiers["partner"]) * rate * vat_mult, 2)
    if tiers.get("partner_usd"):
        return round(float(tiers["partner_usd"]) * rate * vat_mult, 2)
    return round(rrc_rub * 0.75, 2)


def calc_client_unit_price_rub(
    cost_rub_with_vat: float,
    target_profitability_pct: float = 35.0,
    delivery_pct: float = 7.0,
) -> float:
    """
    Корпоративная формула расчета отпускной цены ООО «НИЦ «ИНФРА»:
    Цена клиенту (с НДС) = (Себестоимость (с НДС) / (1 - Рентабельность)) * (1 + Доставка)
    """
    margin_ratio = min(max(target_profitability_pct / 100.0, 0.0), 0.85)
    deliv_ratio = max(delivery_pct / 100.0, 0.0)
    return round((cost_rub_with_vat / (1.0 - margin_ratio)) * (1.0 + deliv_ratio), 2)


def _find_drawing_for_item(sku: str, model_code: str) -> Optional[str]:
    """Ищет чертеж или фото оборудования в папке assets/drawings/ по SKU или коду модели."""
    if not os.path.exists(DRAWINGS_DIR):
        return None
    candidates = [str(sku or "").lower(), str(model_code or "").lower().replace(" ", "_")]
    for fname in os.listdir(DRAWINGS_DIR):
        f_low = fname.lower()
        if any(c and len(c) > 3 and c in f_low for c in candidates):
            return os.path.join(DRAWINGS_DIR, fname)
    return None


def export_internal_margin_xlsx(
    lines: List[Dict[str, Any]],
    rates: Dict[str, float],
    target_profitability_pct: float,
    delivery_pct: float,
    xlsx_path: str,
) -> str:
    """Создает внутреннюю Excel-таблицу расчета себестоимости, НДС и чистой прибыли по стандарту компании."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Расчет КП"

    hdr_fill = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    sub_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    param_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    white_bold = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    dark_bold = Font(name="Arial", size=10, bold=True, color="1E293B")
    regular_font = Font(name="Arial", size=10, color="1E293B")
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    ws["A1"] = "ТАБЛИЦА ДЛЯ КП (ООО «НИЦ «ИНФРА»)"
    ws["A1"].font = Font(name="Arial", size=12, bold=True, color="1B365D")
    ws["G1"] = "СЛУЖЕБНЫЙ РАСЧЕТ СЕБЕСТОИМОСТИ И ПРИБЫЛИ"
    ws["G1"].font = Font(name="Arial", size=11, bold=True, color="B45309")

    param_headers = ["USD, ₽", "CNY / у.е., ₽", "EUR, ₽", "Рент., %", "Доставка, %"]
    param_vals = [
        rates.get("USD", 82.6617),
        rates.get("CNY_VEDA", 12.3907),
        rates.get("EUR", 92.6968),
        target_profitability_pct / 100.0,
        delivery_pct / 100.0,
    ]
    for idx, (ph, pv) in enumerate(zip(param_headers, param_vals), start=11):
        c_h = ws.cell(row=2, column=idx, value=ph)
        c_h.font = dark_bold
        c_h.fill = sub_fill
        c_h.alignment = Alignment(horizontal="center")
        c_h.border = thin_border

        c_v = ws.cell(row=3, column=idx, value=pv)
        c_v.font = dark_bold
        c_v.fill = param_fill
        c_v.alignment = Alignment(horizontal="center")
        c_v.border = thin_border
        c_v.number_format = "0.0%" if idx >= 14 else "#,##0.0000"

    headers = [
        "№ п/п", "Наименование", "Кол-во", "Цена за ед., руб. с НДС 22%", "Стоимость, руб. с НДС 22%",
        "", "Кол-во", "Себестоимость за ед., руб. с НДС", "Себестоимость, руб. с НДС", "Артикул (SKU)"
    ]
    for c_idx, title in enumerate(headers, start=1):
        if not title:
            continue
        cell = ws.cell(row=4, column=c_idx, value=title)
        cell.font = white_bold
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border

    start_r = 5
    for i, item in enumerate(lines, start=1):
        r = start_r + i - 1
        ws.cell(row=r, column=1, value=i).alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=2, value=item["product_name"]).alignment = Alignment(wrap_text=True)
        ws.cell(row=r, column=3, value=item["quantity"]).alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=4, value=f"=ROUND((H{r}/(1-$N$3))*(1+$O$3), 2)")
        ws.cell(row=r, column=5, value=f"=ROUND(C{r}*D{r}, 2)")

        ws.cell(row=r, column=7, value=f"=C{r}").alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=8, value=item["partner_cost_rub"])
        ws.cell(row=r, column=9, value=f"=ROUND(G{r}*H{r}, 2)")
        ws.cell(row=r, column=10, value=item.get("sku", ""))

        for col_i in [1, 2, 3, 4, 5, 7, 8, 9, 10]:
            cell = ws.cell(row=r, column=col_i)
            cell.font = regular_font
            cell.border = thin_border
            if col_i in [4, 5, 8, 9]:
                cell.number_format = "#,##0.00 ₽"

    end_r = start_r + len(lines) - 1
    tot_r = end_r + 1
    vat_r = end_r + 2

    ws.cell(row=tot_r, column=2, value="Итого руб. с НДС 22%").font = dark_bold
    ws.cell(row=tot_r, column=5, value=f"=SUM(E{start_r}:E{end_r})").font = dark_bold
    ws.cell(row=tot_r, column=5).number_format = "#,##0.00 ₽"
    ws.cell(row=tot_r, column=8, value="Итого себестоимость с НДС").font = dark_bold
    ws.cell(row=tot_r, column=9, value=f"=SUM(I{start_r}:I{end_r})").font = dark_bold
    ws.cell(row=tot_r, column=9).number_format = "#,##0.00 ₽"

    ws.cell(row=vat_r, column=2, value="В том числе НДС 22%").font = regular_font
    ws.cell(row=vat_r, column=5, value=f"=ROUND(E{tot_r}*22/122, 2)").number_format = "#,##0.00 ₽"
    ws.cell(row=vat_r, column=8, value="В том числе НДС закупки").font = regular_font
    ws.cell(row=vat_r, column=9, value=f"=ROUND(I{tot_r}*22/122, 2)").number_format = "#,##0.00 ₽"

    gp_r = vat_r + 2
    vd_r = vat_r + 3
    np_r = vat_r + 4

    ws.cell(row=gp_r, column=8, value="Прибыль валовая").font = dark_bold
    ws.cell(row=gp_r, column=9, value=f"=E{tot_r}-I{tot_r}").font = dark_bold
    ws.cell(row=gp_r, column=9).number_format = "#,##0.00 ₽"

    ws.cell(row=vd_r, column=8, value="НДС к уплате (разница)").font = regular_font
    ws.cell(row=vd_r, column=9, value=f"=E{vat_r}-I{vat_r}").number_format = "#,##0.00 ₽"

    ws.cell(row=np_r, column=8, value="Чистая прибыль по сделке").font = Font(name="Arial", size=10, bold=True, color="15803D")
    c_np = ws.cell(row=np_r, column=9, value=f"=I{gp_r}-I{vd_r}")
    c_np.font = Font(name="Arial", size=10, bold=True, color="15803D")
    c_np.number_format = "#,##0.00 ₽"

    col_widths = {1: 8, 2: 52, 3: 10, 4: 22, 5: 22, 6: 3, 7: 10, 8: 24, 9: 24, 10: 22, 11: 12, 12: 14, 13: 12, 14: 11, 15: 13}
    for c_idx, width in col_widths.items():
        ws.column_dimensions[openpyxl.utils.get_column_letter(c_idx)].width = width

    wb.save(xlsx_path)
    return xlsx_path


def generate_commercial_proposal_docx(
    items: List[Dict[str, Any]],
    client_name: str = "Заказчик (по сделке Битрикс24)",
    project_name: str = "Система бесперебойного электроснабжения",
    markup_over_cost_percent: Optional[float] = None,
    delivery_percent: Optional[float] = None,
    output_filename: str = "KP_NIC_INFRA.docx",
    client_address: str = "г. Новосибирск",
    tkp_number: Optional[str] = None,
    bundled_mode: bool = False,
    kit_header_title: Optional[str] = None,
    include_pnr_in_price: bool = True,
    pnr_separate_rub: float = 0.0,
    payment_terms: str = "Аванс 50% от общей стоимости договора; 50% стоимости Оборудования — в течение 10 календарных дней с момента уведомления о готовности к отгрузке.",
    lead_time: str = "2–3 недели с момента заключения договора и при условии своевременного получения авансового платежа.",
) -> Dict[str, Any]:
    """
    Генерирует официальное ТКП ООО «НИЦ «ИНФРА» в формате DOCX + парную внутреннюю Excel-калькуляцию (XLSX).
    Поддерживает корпоративную формулу рентабельности от выручки, построчный и комплектный режимы ТКП,
    а также автоматическую вставку чертежей/фото оборудования из assets/drawings/.
    """
    target_rent = (
        float(markup_over_cost_percent)
        if markup_over_cost_percent is not None
        else DEFAULT_FINANCE_PARAMS["target_profitability_pct"]
    )
    deliv_pct = (
        float(delivery_percent)
        if delivery_percent is not None
        else DEFAULT_FINANCE_PARAMS["delivery_pct"]
    )
    doc_num = tkp_number or f"{datetime.now().strftime('%d%m%y')}-1/1"

    total_cost_rub = 0.0
    total_offer_rub = 0.0
    detailed_lines = []

    for entry in items:
        sku = entry.get("sku")
        qty = int(entry.get("quantity", 1))
        db_item = get_item_from_db(sku) if sku else {}
        source = {**db_item, **{k: v for k, v in entry.items() if v is not None}}

        rrc_rub = float(source.get("rrc_price_rub_with_vat") or 0.0)
        unit_cost_rub = extract_real_partner_cost_rub(source)
        unit_client_rub = calc_client_unit_price_rub(unit_cost_rub, target_rent, deliv_pct)

        line_cost = round(unit_cost_rub * qty, 2)
        line_client = round(unit_client_rub * qty, 2)

        total_cost_rub += line_cost
        total_offer_rub += line_client

        specs_raw = source.get("specifications")
        specs = json.loads(specs_raw) if isinstance(specs_raw, str) and specs_raw else {}

        detailed_lines.append({
            "sku": source.get("sku") or "",
            "model_code": source.get("model_code") or "",
            "product_name": source.get("product_name") or str(sku),
            "etim_class": source.get("etim_class"),
            "power_kw": source.get("power_kw"),
            "rated_current_a": source.get("rated_current_a"),
            "voltage_v": source.get("voltage_v"),
            "ip_rating": source.get("ip_rating"),
            "specs": specs,
            "quantity": qty,
            "rrc_price_rub": rrc_rub,
            "partner_cost_rub": unit_cost_rub,
            "client_unit_price_rub": unit_client_rub,
            "total_line_rub": line_client,
        })

    total_cost_rub = round(total_cost_rub, 2)
    total_offer_rub = round(total_offer_rub, 2)
    vat_22_rub = round(total_offer_rub * 22.0 / 122.0, 2)
    vat_cost_rub = round(total_cost_rub * 22.0 / 122.0, 2)
    gross_profit_rub = round(total_offer_rub - total_cost_rub, 2)
    net_profit_rub = round(gross_profit_rub - (vat_22_rub - vat_cost_rub), 2)

    # --- СОЗДАНИЕ ОФИЦИАЛЬНОГО БЛАНКА WORD (.DOCX) ---
    doc = Document()
    for section in doc.sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.6)
        section.right_margin = Cm(1.6)

    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(9.5)
    style.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)

    # 1. Фирменная шапка ООО «НИЦ «ИНФРА»
    hdr_tbl = doc.add_table(rows=1, cols=2)
    hdr_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr_tbl.autofit = False
    hdr_tbl.columns[0].width = Cm(10.8)
    hdr_tbl.columns[1].width = Cm(7.0)

    c0, c1 = hdr_tbl.rows[0].cells[0], hdr_tbl.rows[0].cells[1]
    _set_cell_shading(c0, "1B365D")
    _set_cell_shading(c1, "D97706")
    _set_cell_margins(c0, top=160, bottom=160, left=180, right=140)
    _set_cell_margins(c1, top=160, bottom=160, left=140, right=180)

    p0 = c0.paragraphs[0]
    r_brand = p0.add_run(f"{COMPANY_INFO['short_name']}\n")
    r_brand.bold = True
    r_brand.font.size = Pt(16)
    r_brand.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    r_tag = p0.add_run(
        "Научно-инженерный центр • ИБП • АСУ ТП • ПЛК • ЧРП • SCADA\n"
        f"{COMPANY_INFO['tagline']}"
    )
    r_tag.font.size = Pt(8)
    r_tag.font.color.rgb = RGBColor(0xE2, 0xE8, 0xF0)

    p1 = c1.paragraphs[0]
    p1.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r_doc_id = p1.add_run(
        f"ТКП № {doc_num}\n"
        f"от {datetime.now().strftime('%d.%m.%Y')} г.\n"
        f"{COMPANY_INFO['city']}"
    )
    r_doc_id.bold = True
    r_doc_id.font.size = Pt(10)
    r_doc_id.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    doc.add_paragraph()

    # 2. Заголовок и реквизиты Заказчика
    p_h = doc.add_paragraph()
    p_h.paragraph_format.space_after = Pt(2)
    r_h1 = p_h.add_run(f"ТЕХНИКО-КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ № {doc_num}\n")
    r_h1.bold = True
    r_h1.font.size = Pt(13)
    r_h1.font.color.rgb = RGBColor(0x1B, 0x36, 0x5D)
    r_h2 = p_h.add_run(f"«{project_name}»\n")
    r_h2.bold = True
    r_h2.font.size = Pt(11)
    r_h3 = p_h.add_run(f"Заказчик: {client_name} ({client_address})")
    r_h3.font.size = Pt(9.5)

    # 3. Таблица реквизитов компании ООО «НИЦ «ИНФРА»
    p_req_title = doc.add_paragraph()
    p_req_title.paragraph_format.space_before = Pt(6)
    p_req_title.paragraph_format.space_after = Pt(3)
    r_rt = p_req_title.add_run("Реквизиты компании")
    r_rt.bold = True
    r_rt.font.size = Pt(10)
    r_rt.font.color.rgb = RGBColor(0x1B, 0x36, 0x5D)

    req_rows = COMPANY_INFO["requisites_rows"]
    req_table = doc.add_table(rows=len(req_rows), cols=2)
    req_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_borders(req_table)
    for idx_r, (k_txt, v_txt) in enumerate(req_rows):
        row = req_table.rows[idx_r]
        row.cells[0].width = Cm(5.8)
        row.cells[1].width = Cm(12.0)
        bg = "F8FAFC" if idx_r % 2 == 0 else "FFFFFF"
        for c_i, txt in enumerate([k_txt, v_txt]):
            _set_cell_shading(row.cells[c_i], bg)
            _set_cell_margins(row.cells[c_i], top=45, bottom=45, left=90, right=90)
            p = row.cells[c_i].paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run(txt)
            run.font.size = Pt(8)
            if c_i == 0:
                run.bold = True

    doc.add_paragraph()

    # 4. Таблица коммерческого предложения
    p_kp_t = doc.add_paragraph()
    p_kp_t.paragraph_format.space_after = Pt(4)
    r_kpt = p_kp_t.add_run("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ")
    r_kpt.bold = True
    r_kpt.font.size = Pt(11.5)
    r_kpt.font.color.rgb = RGBColor(0x1B, 0x36, 0x5D)

    widths = [Cm(1.2), Cm(9.8), Cm(1.6), Cm(2.6), Cm(2.6)]
    headers = ["№ п/п", "Наименование", "Кол-во", "Цена за ед.,\nруб. с НДС 22%", "Стоимость,\nруб. с НДС 22%"]

    kp_table = doc.add_table(rows=1, cols=5)
    kp_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_borders(kp_table)

    for i, h_text in enumerate(headers):
        cell = kp_table.rows[0].cells[i]
        cell.width = widths[i]
        _set_cell_shading(cell, "1B365D")
        _set_cell_margins(cell, top=80, bottom=80, left=80, right=80)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if i != 1 else WD_ALIGN_PARAGRAPH.LEFT
        r = p.add_run(h_text)
        r.bold = True
        r.font.size = Pt(8.5)
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    if bundled_mode and kit_header_title:
        hdr_row = kp_table.add_row().cells
        hdr_row[0].merge(hdr_row[4])
        _set_cell_shading(hdr_row[0], "E2E8F0")
        _set_cell_margins(hdr_row[0], top=70, bottom=70, left=90, right=90)
        p_kh = hdr_row[0].paragraphs[0]
        r_kh = p_kh.add_run(kit_header_title)
        r_kh.bold = True
        r_kh.font.size = Pt(8.5)

    for idx, line in enumerate(detailed_lines, start=1):
        row = kp_table.add_row().cells
        bg = "FFFFFF" if idx % 2 == 1 else "F8FAFC"

        if bundled_mode:
            unit_str = _fmt_rub(total_offer_rub) if idx == 1 else ""
            tot_str = _fmt_rub(total_offer_rub) if idx == 1 else ""
        else:
            unit_str = _fmt_rub(line["client_unit_price_rub"])
            tot_str = _fmt_rub(line["total_line_rub"])

        vals = [str(idx), line["product_name"], str(line["quantity"]), unit_str, tot_str]
        for i, val in enumerate(vals):
            row[i].width = widths[i]
            _set_cell_shading(row[i], bg)
            _set_cell_margins(row[i], top=65, bottom=65, left=80, right=80)
            row[i].vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = row[i].paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if i in [0, 2] else (
                WD_ALIGN_PARAGRAPH.RIGHT if i in [3, 4] else WD_ALIGN_PARAGRAPH.LEFT
            )
            r = p.add_run(val)
            r.font.size = Pt(8.5)
            if i == 4 and val:
                r.bold = True

    for lbl, val_str, is_bold in [
        ("Итого руб. с НДС 22%", _fmt_rub(total_offer_rub), True),
        ("В том числе НДС 22%", _fmt_rub(vat_22_rub), False),
    ]:
        r_tot = kp_table.add_row().cells
        r_tot[0].merge(r_tot[3])
        bg_t = "E2E8F0" if is_bold else "F8FAFC"
        _set_cell_shading(r_tot[0], bg_t)
        _set_cell_shading(r_tot[4], bg_t)
        _set_cell_margins(r_tot[0], top=70, bottom=70, left=90, right=90)
        _set_cell_margins(r_tot[4], top=70, bottom=70, left=80, right=80)

        p_l = r_tot[0].paragraphs[0]
        p_l.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        rl = p_l.add_run(lbl)
        rl.bold = is_bold
        rl.font.size = Pt(9 if is_bold else 8.5)

        p_v = r_tot[4].paragraphs[0]
        p_v.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        rv = p_v.add_run(val_str)
        rv.bold = is_bold
        rv.font.size = Pt(9.5 if is_bold else 8.5)

    doc.add_paragraph()

    # 5. Коммерческие условия и блок подписи
    pnr_note = (
        f"в указанную стоимость оборудования входит ПНР, упаковка и транспортировка оборудования до площадки Заказчика ({client_address});"
        if include_pnr_in_price
        else f"в указанную стоимость включена упаковка и транспортировка до площадки Заказчика ({client_address}); стоимость ШМР и ПНР не включена и составляет ориентировочно {_fmt_rub(pnr_separate_rub)} рублей."
    )

    conditions = [
        ("Обращаем Ваше внимание:", pnr_note),
        ("Сроки поставки оборудования и выполнения работ:", lead_time),
        ("Гарантийный срок:", "Гарантийный срок на поставляемое оборудование составляет 12 месяцев с момента ввода в эксплуатацию, но не более 18 месяцев с момента поставки оборудования; на выполняемые работы — 18 месяцев."),
        ("Условия оплаты:", payment_terms),
        ("Срок действия предложения:", "30 календарных дней с даты направления предложения. Предлагаемые условия не являются окончательными и могут быть согласованы дополнительно."),
    ]
    for title_c, body_c in conditions:
        p_c = doc.add_paragraph()
        p_c.paragraph_format.space_after = Pt(3)
        r1 = p_c.add_run(title_c + " ")
        r1.bold = True
        r1.font.size = Pt(8.5)
        r2 = p_c.add_run(body_c)
        r2.font.size = Pt(8.5)

    doc.add_paragraph()
    p_sig = doc.add_paragraph()
    r_s1 = p_sig.add_run(
        f"С уважением,\n{COMPANY_INFO['director_title']} {COMPANY_INFO['short_name']}                                          "
        f"{COMPANY_INFO['director_name']}\n"
    )
    r_s1.bold = True
    r_s1.font.size = Pt(9.5)
    r_s2 = p_sig.add_run(f"Тел. {COMPANY_INFO['phone']}  |  E-mail: {COMPANY_INFO['email']}")
    r_s2.font.size = Pt(8.5)

    if os.path.exists(SIGNATURE_IMAGE_PATH):
        p_img_sig = doc.add_paragraph()
        p_img_sig.add_run().add_picture(SIGNATURE_IMAGE_PATH, width=Cm(4.2))

    # 6. Приложение с чертежами и фото оборудования (если найдены в assets/drawings/)
    found_drawings = []
    for idx, line in enumerate(detailed_lines, start=1):
        img_p = _find_drawing_for_item(line.get("sku", ""), line.get("model_code", ""))
        if img_p and img_p not in [x[1] for x in found_drawings]:
            found_drawings.append((f"Рис. {len(found_drawings)+1}. {line['product_name']}", img_p))

    if found_drawings:
        doc.add_page_break()
        p_app = doc.add_paragraph()
        r_app = p_app.add_run("Приложение. Внешний вид и габаритные чертежи предлагаемого оборудования")
        r_app.bold = True
        r_app.font.size = Pt(11)
        r_app.font.color.rgb = RGBColor(0x1B, 0x36, 0x5D)

        for caption_txt, img_path in found_drawings:
            p_pic = doc.add_paragraph()
            p_pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_pic.add_run().add_picture(img_path, width=Cm(13.5))
            p_cap = doc.add_paragraph()
            p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            rc = p_cap.add_run(caption_txt)
            rc.bold = True
            rc.font.size = Pt(9)

    final_docx_path = (
        output_filename
        if os.path.dirname(output_filename)
        else os.path.join(OUTPUT_KP_DIR, output_filename)
    )
    doc.save(final_docx_path)

    # Параллельно сохраняем внутренний Excel-файл калькуляции маржи (.xlsx)
    xlsx_filename = os.path.splitext(os.path.basename(final_docx_path))[0] + "_Raschet.xlsx"
    final_xlsx_path = os.path.join(OUTPUT_KP_DIR, xlsx_filename)
    rates = get_live_rates_from_db()
    export_internal_margin_xlsx(detailed_lines, rates, target_rent, deliv_pct, final_xlsx_path)

    return {
        "status": "DOCX_AND_XLSX_GENERATED",
        "client_name": client_name,
        "client_address": client_address,
        "project_name": project_name,
        "file_path": final_docx_path,
        "xlsx_path": final_xlsx_path,
        "positions": detailed_lines,
        "total_purchase_cost_rub": total_cost_rub,
        "subtotal_equipment_rub": total_offer_rub,
        "delivery_rub": round(total_offer_rub * (deliv_pct / (100.0 + deliv_pct)), 2),
        "total_offer_with_vat_rub": total_offer_rub,
        "vat_22_amount_rub": vat_22_rub,
        "expected_margin_rub": net_profit_rub,
        "gross_profit_rub": gross_profit_rub,
        "margin_percent": target_rent,
    }