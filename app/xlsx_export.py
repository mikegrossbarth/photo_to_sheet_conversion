from __future__ import annotations

import html
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


ILLEGAL_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
GRADE_WORDS_RE = re.compile(
    r"\b(?:GEM[-\s]?MT|MINT|NM[-\s]?MT\+?|NM|EX[-\s]?MT|EX|VG[-\s]?EX|VG|GOOD|FR|PR|HALF[-\s]?POINT)\b",
    re.IGNORECASE,
)
NUMERIC_GRADE_RE = re.compile(r"\d+(?:\.\d+)?")
EXCLUDED_DESCRIPTION_ATTRIBUTE_PATTERNS = [
    re.compile(r"^(CENTERING|CORNERS|EDGES|SURFACE)\s*[:\-]?\s*\d+(?:\.\d+)?$", re.IGNORECASE),
    re.compile(r"^(OVERALL|SUBGRADE|SUBGRADES)\b", re.IGNORECASE),
]
EXCLUDED_DESCRIPTION_ATTRIBUTES = {
    "MINT",
    "GEM MINT",
    "GEM-MT",
    "PRISTINE",
    "NEAR MINT",
    "NM",
    "NM-MT",
    "NM-MT+",
    "EX-MT",
    "EX",
    "VG-EX",
    "VG",
    "GOOD",
    "FR",
    "PR",
    "HALF-POINT",
    "EXCELLENT",
    "VERY GOOD",
}

EXPORT_KEYS = [
    "cert_number",
    "purchase_price",
    "card_description",
    "card_number",
    "player",
    "year",
    "set",
    "subset",
    "parallel",
    "grading_company",
    "grade",
    "category",
    "confidence",
    "is_graded_slab",
    "label_text",
    "position",
    "source_file",
    "quality",
]

EXPORT_HEADERS = [
    "Certification Number",
    "Purchase Price",
    "Card Description",
    "Card Number",
    "Player / Subject",
    "Year",
    "Set",
    "Subset",
    "Parallel",
    "Grading Company",
    "Grade",
    "Sport",
    "Confidence",
    "Is Graded Slab",
    "Raw Label Text",
    "Position",
    "Source Photo",
    "Quality Score",
]


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return ILLEGAL_XML_CHARS.sub("", text)


def _clean_part(value) -> str:
    return re.sub(r"\s+", " ", _clean(value)).strip()


def _description_attributes(value) -> str:
    kept = []
    seen = set()
    for part in re.split(r"[;\n\r]+", _clean(value)):
        item = _clean_part(part)
        if not item:
            continue
        marker = item.upper()
        if marker in EXCLUDED_DESCRIPTION_ATTRIBUTES:
            continue
        if any(pattern.match(item) for pattern in EXCLUDED_DESCRIPTION_ATTRIBUTE_PATTERNS):
            continue
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(item)
    return " ".join(kept)


def clean_grade(value) -> str:
    text = GRADE_WORDS_RE.sub(" ", str(value or ""))
    numbers = NUMERIC_GRADE_RE.findall(text)
    return numbers[-1] if numbers else ""


def build_card_description(row: dict) -> str:
    parts = []
    seen = set()
    for key in ("year", "set", "player", "parallel", "subset", "attributes", "grading_company", "grade"):
        if key == "attributes":
            value = _description_attributes(row.get(key, ""))
        elif key == "grade":
            value = clean_grade(row.get(key, ""))
        else:
            value = _clean_part(row.get(key, ""))
        if not value:
            continue
        marker = value.upper()
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(value)
    return " ".join(parts)


def build_export_rows(rows: Iterable[dict]) -> list[dict]:
    export_rows = []
    for row in rows:
        export_row = {key: row.get(key, "") for key in EXPORT_KEYS}
        export_row["grade"] = clean_grade(export_row.get("grade", ""))
        export_row["card_description"] = build_card_description(row)
        export_rows.append(export_row)
    return export_rows


def _cell_ref(row_index: int, col_index: int) -> str:
    letters = ""
    n = col_index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row_index + 1}"


def _sheet_xml(headers: list[str], rows: list[dict], keys: list[str]) -> str:
    all_rows: list[list[str]] = [headers]
    for row in rows:
        all_rows.append([_clean(row.get(key, "")) for key in keys])

    xml_rows = []
    for row_idx, values in enumerate(all_rows):
        cells = []
        for col_idx, value in enumerate(values):
            escaped = html.escape(_clean(value), quote=True)
            cells.append(
                f'<c r="{_cell_ref(row_idx, col_idx)}" t="inlineStr"><is><t>{escaped}</t></is></c>'
            )
        xml_rows.append(f'<row r="{row_idx + 1}">{"".join(cells)}</row>')

    widths = []
    for col_idx, header in enumerate(headers):
        max_len = len(header)
        for row in all_rows[1:]:
            if col_idx < len(row):
                max_len = max(max_len, len(str(row[col_idx])))
        width = min(max(max_len + 2, 10), 48)
        widths.append(f'<col min="{col_idx + 1}" max="{col_idx + 1}" width="{width}" customWidth="1"/>')

    dimension = f"A1:{_cell_ref(max(len(all_rows) - 1, 0), len(headers) - 1)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="{dimension}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'<cols>{"".join(widths)}</cols>'
        f'<sheetData>{"".join(xml_rows)}</sheetData>'
        '<autoFilter ref="' + dimension + '"/>'
        '</worksheet>'
    )


def write_xlsx(path: Path, rows: Iterable[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_export_rows(rows)
    keys = EXPORT_KEYS
    headers = EXPORT_HEADERS

    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Cards" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>'
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>OCR Photos to Spreadsheet</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        '</cp:coreProperties>'
    )
    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>OCR Photos to Spreadsheet</Application></Properties>'
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(headers, rows, keys))
        zf.writestr("xl/styles.xml", styles)
        zf.writestr("docProps/core.xml", core)
        zf.writestr("docProps/app.xml", app)
    return path
