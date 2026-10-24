"""Excel (.xlsx) export for the bike call-center bridge.

Pure standard library (zipfile + XML): builds a minimal but valid
Office Open XML workbook, so it runs inside the bridge container and in
unit tests without extra dependencies. Mirrors the legacy Kinghav
system's "导出 Excel" buttons (service records, voicemail list, call
details), which the gap analysis flagged as a real missing feature.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")

# (column header, width in characters)
TICKET_COLUMNS = [
    ("工单号", 10), ("标题", 42), ("工单类型", 12), ("服务类型", 12),
    ("状态", 10), ("优先级", 8), ("分组", 10), ("客户电话", 14),
    ("单车编号", 12), ("服务地点", 20), ("通话ID", 34), ("录音地址", 30),
    ("投诉等级", 10), ("创建时间", 20), ("更新时间", 20),
]

CALL_COLUMNS = [
    ("CallID", 34), ("主叫", 14), ("被叫", 14), ("方向", 8),
    ("是否接通", 10), ("通话时长(秒)", 12), ("等待时长(秒)", 12),
    ("开始时间", 20), ("关联工单", 10),
]

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(value) -> str:
    """Strip characters that are illegal in XML 1.0."""
    return _CTRL_RE.sub("", str(value))


def _sheet_name(name: str) -> str:
    name = re.sub(r'[\[\]:*?/\\]', "_", name).strip() or "Sheet"
    return name[:31]


def format_ts(epoch) -> str:
    """Unix epoch -> 'YYYY-MM-DD HH:MM:SS' in Asia/Shanghai (for humans)."""
    try:
        ts = int(epoch)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")


def ticket_row(t: dict, state_map: dict, priority_map: dict,
               group_map: dict, phone_map: dict) -> list:
    """Flatten one Zammad ticket dict into an export row.

    Custom bike fields (call_id, recording_url, bike_no, ticket_type,
    fault_type, complaint_level, service_address) are read straight off
    the ticket object, matching the attributes created by
    zammad-setup/setup_bike_fields.py.
    """
    t = t or {}
    phone = ""
    title = str(t.get("title") or "")
    # Bridge-created call tickets are titled "电话工单 <caller> <type>".
    if title.startswith("电话工单 "):
        parts = title.split()
        if len(parts) > 1:
            phone = parts[1]
    if not phone:
        phone = str(phone_map.get(t.get("customer_id"), "") or "")
    return [
        t.get("id"), title,
        t.get("ticket_type") or "", t.get("fault_type") or "",
        state_map.get(t.get("state_id"), t.get("state_id")),
        priority_map.get(t.get("priority_id"), t.get("priority_id")),
        group_map.get(t.get("group_id"), t.get("group_id")),
        phone,
        t.get("bike_no") or "", t.get("service_address") or "",
        t.get("call_id") or "", t.get("recording_url") or "",
        t.get("complaint_level") or "",
        t.get("created_at") or "", t.get("updated_at") or "",
    ]


def call_row(r: tuple) -> list:
    """Flatten one SQLite calls row:
    (call_id, caller, callee, direction, answered, duration_sec,
     wait_sec, start_epoch, ticket_id)."""
    (call_id, caller, callee, direction, answered,
     duration_sec, wait_sec, start_epoch, ticket_id) = r
    return [
        call_id or "", caller or "", callee or "",
        "呼入" if direction == "in" else ("呼出" if direction == "out" else direction or ""),
        "是" if answered else "否",
        duration_sec or 0, wait_sec or 0,
        format_ts(start_epoch), ticket_id or "",
    ]


# ---------------------------------------------------------------- xlsx writer

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxml-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
%s
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>%s</sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<stylesheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/></patternFill>
</fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color auto="1"/></left><right style="thin"><color auto="1"/></right><top style="thin"><color auto="1"/></top><bottom style="thin"><color auto="1"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center" vertical="center"/></xf>
</cellXfs>
</stylesheet>"""


def _col_letter(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def build_xlsx(sheets: list) -> bytes:
    """sheets: list of (sheet_name, columns, rows).

    columns: list of (header, width). rows: list of lists; values may be
    str/int/float/None (bool is treated as int). Returns .xlsx bytes.
    """
    strings: list[str] = []
    str_index: dict[str, int] = {}

    def sref(value) -> int:
        text = _clean(value)
        if text not in str_index:
            str_index[text] = len(strings)
            strings.append(text)
        return str_index[text]

    sheet_xmls = []
    for si, (name, columns, rows) in enumerate(sheets):
        name = _sheet_name(name)
        ncols = len(columns)
        last_ref = f"{_col_letter(ncols - 1)}{len(rows) + 1}" if ncols else "A1"
        cols_xml = "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, (_, w) in enumerate(columns))
        out = [f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               f'<dimension ref="A1:{last_ref}"/>'
               '<sheetViews><sheetView workbookViewId="0">'
               '<pane ySplit="1" topLeftCell="A2" state="frozen"/>'
               '</sheetView></sheetViews>'
               f'<cols>{cols_xml}</cols><sheetData>']
        # header row (style 1 = bold + fill + border)
        cells = []
        for ci, (header, _) in enumerate(columns):
            cells.append(f'<c r="{_col_letter(ci)}1" t="s" s="1">'
                         f'<v>{sref(header)}</v></c>')
        out.append(f'<row r="1">{"".join(cells)}</row>')
        # data rows
        for ri, row in enumerate(rows, start=2):
            cells = []
            for ci in range(ncols):
                v = row[ci] if ci < len(row) else None
                if v is None or v == "":
                    continue
                ref = f"{_col_letter(ci)}{ri}"
                if isinstance(v, bool):
                    v = int(v)
                if isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}" s="0"><v>{v}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="s" s="0"><v>{sref(v)}</v></c>')
            out.append(f'<row r="{ri}">{"".join(cells)}</row>')
        out.append('</sheetData></worksheet>')
        sheet_xmls.append("".join(out))

    sst = "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
    shared = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
              f'count="{len(strings)}" uniqueCount="{len(strings)}">{sst}</sst>')

    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(sheets)))
    sheets_el = "".join(
        f'<sheet name="{escape(_sheet_name(n))}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, (n, _, _) in enumerate(sheets))
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(sheets)))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES % overrides)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK % sheets_el)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS % rels)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/sharedStrings.xml", shared)
        for i, sx in enumerate(sheet_xmls):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", sx)
    return buf.getvalue()
