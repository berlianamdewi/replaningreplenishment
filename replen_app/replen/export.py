"""Penulisan file Excel output: template WMS, laporan replenishment, laporan exception."""
import io

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .engine import WMS_COLUMNS


def write_wms(wms: pd.DataFrame, template: bytes) -> bytes:
    """Isi sheet 'Data' pada template WMS asli. Header, urutan kolom, dan sheet Validations tidak diubah."""
    wb = load_workbook(io.BytesIO(template))
    ws = wb["Data"]
    # baris 1 = nama kolom WMS (B..S), baris 2 = label; data mulai baris 3
    pos = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v in WMS_COLUMNS:
            pos[v] = c
    missing = [c for c in WMS_COLUMNS if c not in pos]
    if missing:
        raise ValueError(f"Template WMS tidak punya kolom: {missing}")
    if ws.max_row >= 3:
        ws.delete_rows(3, ws.max_row - 2)
    for i, row in enumerate(wms.itertuples(index=False), start=3):
        ws.cell(i, 1).value = ""  # kolom Messages
        for col, val in zip(wms.columns, row):
            if val == "" or (isinstance(val, float) and pd.isna(val)):
                continue
            ws.cell(i, pos[col]).value = val
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _fmt(ws, df):
    head = PatternFill("solid", fgColor="1F4E78")
    for c in range(1, len(df.columns) + 1):
        cell = ws.cell(1, c)
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = head
        width = max([len(str(df.columns[c - 1]))] + [len(str(v)) for v in df.iloc[:300, c - 1]]) + 2
        ws.column_dimensions[get_column_letter(c)].width = min(width, 60)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial", size=10)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_book(sheets: dict) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df = df.copy()
            for c in df.columns:
                if str(df[c].dtype).startswith("datetime"):
                    df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M")
            df.to_excel(xw, sheet_name=name, index=False)
            _fmt(xw.sheets[name], df)
            ws = xw.sheets[name]
            for c, col in enumerate(df.columns, start=1):
                if col == "% Active/Max":
                    for r in range(2, len(df) + 2):
                        ws.cell(r, c).number_format = "0.0%"
    return out.getvalue()


def report_book(result) -> bytes:
    return write_book({"Ringkasan SKU": result.sku_report, "Detail Task": result.task_detail})


def exception_book(result) -> bytes:
    return write_book({"Exception": result.exceptions, "Validasi": result.validations})
