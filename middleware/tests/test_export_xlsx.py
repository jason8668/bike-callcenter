import os
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge_core import CallStore, parse_cdr  # noqa: E402
from export_xlsx import CALL_COLUMNS, build_xlsx, call_row, format_ts  # noqa: E402

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_sheet(xlsx: bytes, idx=1):
    """Return (headers, rows) of decoded cell values for sheet idx."""
    with zipfile.ZipFile(__import__("io").BytesIO(xlsx)) as z:
        names = z.namelist()
        for want in ("[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml",
                     "xl/sharedStrings.xml", f"xl/worksheets/sheet{idx}.xml"):
            assert want in names, f"missing part {want}"
        strings = [t.text or "" for t in
                   ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "t")]
        sheet = ET.fromstring(z.read(f"xl/worksheets/sheet{idx}.xml"))
    grid = {}
    for row in sheet.iter(NS + "row"):
        r = int(row.get("r"))
        for c in row.iter(NS + "c"):
            ref, v = c.get("r"), c.find(NS + "v")
            if v is None:
                continue
            val = v.text
            if c.get("t") == "s":
                val = strings[int(val)]
            elif "." in (val or ""):
                val = float(val)
            else:
                val = int(val)
            grid[(r, ref.rstrip("0123456789"))] = val
    ncols = max(ord(k[1]) - 64 for k in grid) if grid else 0
    nrows = max(k[0] for k in grid) if grid else 0
    cols = [chr(65 + i) for i in range(ncols)]
    table = [[grid.get((r, c), "") for c in cols] for r in range(1, nrows + 1)]
    return table[0], table[1:]


class TestXlsxWriter(unittest.TestCase):
    def test_roundtrip_chinese_and_special_chars(self):
        cols = [("记录号", 10), ("通话内容", 30), ("时长", 12)]
        rows = [
            [101, "还不了车 <故障>&\"报修\"", 65],
            [102, None, 0],
            [103, "", 3.5],
        ]
        headers, data = read_sheet(build_xlsx([("通话记录", cols, rows)]))
        self.assertEqual(headers, ["记录号", "通话内容", "时长"])
        self.assertEqual(data[0], [101, '还不了车 <故障>&"报修"', 65])
        self.assertEqual(data[1][0], 102)  # None cell skipped -> ""
        self.assertEqual(data[2][2], 3.5)

    def test_header_styled_and_frozen(self):
        raw = build_xlsx([("s", [("A", 8)], [[1]])])
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn('s="1"', sheet)  # header uses the bold style
        self.assertIn('state="frozen"', sheet)
        self.assertIn('width="8"', sheet)

    def test_sheet_name_sanitized(self):
        raw = build_xlsx([("a/b:c*d?e[f]g" + "x" * 40, [("A", 8)], [])])
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as z:
            wb = z.read("xl/workbook.xml").decode()
        self.assertNotIn("/", wb.split('name="')[1].split('"')[0])

    def test_two_sheets(self):
        raw = build_xlsx([("一", [("A", 8)], [[1]]), ("二", [("B", 8)], [[2]])])
        _, d1 = read_sheet(raw, 1)
        _, d2 = read_sheet(raw, 2)
        self.assertEqual(d1[0][0], 1)
        self.assertEqual(d2[0][0], 2)


class TestRowBuilders(unittest.TestCase):
    def test_call_row(self):
        row = call_row({
            "record_id": 7, "call_id": "abc-1", "caller": "13800000000",
            "agent": "8001", "kind": "电话", "duration_sec": 65,
            "start_epoch": 1792800000, "service_type": "还车异常",
            "question": "还不了车", "recording_file": "abc-1.wav",
            "answered": True})
        self.assertEqual(len(row), len(CALL_COLUMNS))
        self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(row[1], "13800000000")
        self.assertEqual(row[2], "8001")
        self.assertEqual(row[3], "1分05秒")
        self.assertEqual(row[4], "热线电话")
        self.assertEqual(row[5], "还车异常")
        self.assertEqual(row[6], "还不了车")
        self.assertEqual(row[7], "abc-1.wav")
        self.assertEqual(row[8], "abc-1")

    def test_call_row_missed_and_voicemail(self):
        missed = call_row({
            "call_id": "m-1", "caller": "13800000000", "agent": "",
            "kind": "未接", "duration_sec": 20, "start_epoch": 1792800000,
            "service_type": "", "question": "", "recording_file": "",
            "answered": False})
        self.assertEqual(missed[3], "未接")
        self.assertEqual(missed[4], "热线电话")
        vm = call_row({
            "call_id": "v-1", "caller": "13900000000", "agent": "8002",
            "kind": "留言", "duration_sec": 8, "start_epoch": 1792800000,
            "service_type": "", "question": "", "recording_file": "v-1.wav",
            "answered": True})
        self.assertEqual(vm[4], "语音留言")
        self.assertEqual(vm[3], "0分08秒")

    def test_format_ts_edge_cases(self):
        self.assertEqual(format_ts(0), "")
        self.assertEqual(format_ts(""), "")
        self.assertEqual(format_ts(None), "")
        # 1792800000 falls on 2026-10-24 in Asia/Shanghai
        self.assertTrue(format_ts(1792800000).startswith("2026-10-24"))


class TestCallStoreExport(unittest.TestCase):
    def test_list_calls_with_migration(self):
        tmp = tempfile.mkdtemp()
        store = CallStore(os.path.join(tmp, "b.db"))
        rec = parse_cdr({"uuid": "u1", "caller_id_number": "1381",
                         "destination_number": "4001", "billsec": "30",
                         "start_epoch": "1792800000", "bridge_ticket": "1"})
        store.save_call(rec, True, kind="电话")
        # reopening must not fail on the ALTER TABLE migration
        store2 = CallStore(os.path.join(tmp, "b.db"))
        rows = store2.list_calls(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["call_id"], "u1")
        self.assertEqual(rows[0]["caller"], "1381")
        self.assertEqual(rows[0]["kind"], "电话")
        self.assertEqual(store2.list_calls(1792900000), [])


if __name__ == "__main__":
    unittest.main()
