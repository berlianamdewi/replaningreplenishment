"""Pembaca file Excel input: deteksi header otomatis, pembersihan angka/tanggal."""
import io
import re
import warnings

import pandas as pd


class LoaderError(Exception):
    pass


def norm(x) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()


# Nama kolom yang dikenali (sudah dinormalisasi: huruf kecil, tanpa tanda baca).
SPECS = {
    "il": {  # Item & Location
        "item": ["item", "sku", "item name"],
        "location": ["location"],
        "on_hand": ["on hand"],
        "cap_max": ["capacity maximum"],
        "loc_type": ["location type"],
        "available": ["available"],
        "on_hold": ["on hold"],
        "cap_min": ["capacity minimum"],
        "pack_key": ["pack key"],
        "owner": ["owner"],
    },
    "inv": {  # Inventory Balance
        "owner": ["owner"],
        "item": ["item", "sku"],
        "lot": ["lot"],
        "location": ["location"],
        "lpn": ["lpn"],
        "on_hand": ["on hand"],
        "available": ["available"],
        "status": ["status"],
        "date_updated": ["date updated"],
    },
    "order": {  # SO / TO
        "sku": ["sku", "item"],
        "qty_so": ["qty so"],
        "order_type": ["order type"],
        "status": ["status"],
    },
    "rr": {  # Running Rate
        "item": ["item name", "item", "sku"],
        "rr": ["rr13", "forecast", "rr"],
    },
}
REQUIRED = {
    "il": ["item", "location", "on_hand", "cap_max", "loc_type"],
    "inv": ["owner", "item", "lot", "location", "lpn", "available", "status", "date_updated"],
    "order": ["sku", "qty_so"],
    "rr": ["item", "rr"],
}
LABEL = {
    "il": "Item & Location",
    "inv": "Inventory Balance",
    "order": "SO / TO",
    "rr": "Running Rate (RR13)",
}


def _detect(raw: pd.DataFrame, kind: str):
    """Cari baris header dalam 10 baris pertama. Return (index, {field: kolom}) atau None."""
    for i in range(min(10, len(raw))):
        vals = [norm(v) for v in raw.iloc[i].tolist()]
        mapping = {}
        for field, aliases in SPECS[kind].items():
            for a in aliases:
                if a in vals:
                    mapping[field] = vals.index(a)
                    break
        if all(f in mapping for f in REQUIRED[kind]):
            return i, mapping
    return None


def candidates(data: bytes, kind: str) -> list:
    """Nama sheet yang cocok dengan jenis file."""
    xl = pd.ExcelFile(io.BytesIO(data))
    out = []
    for sh in xl.sheet_names:
        head = xl.parse(sh, header=None, nrows=10, dtype=str, keep_default_na=False)
        if _detect(head, kind):
            out.append(sh)
    return out


def num(s: pd.Series) -> pd.Series:
    t = s.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(t, errors="coerce")


def parse_dt(s: pd.Series) -> pd.Series:
    """Format Infor: '11/3/26, 12:48 PM' (ada spasi khusus U+202F sebelum AM/PM)."""
    t = s.astype(str).str.replace("\u202f", " ", regex=False).str.replace("\u00a0", " ", regex=False).str.strip()
    out = pd.to_datetime(t, format="%m/%d/%y, %I:%M %p", errors="coerce")
    miss = out.isna() & t.ne("")
    if miss.any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out[miss] = pd.to_datetime(t[miss], errors="coerce")
    return out


def _clean_text(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace("\u00a0", " ", regex=False).str.replace("\u202f", " ", regex=False).str.strip()


def load(data: bytes, kind: str, sheet: str = None) -> pd.DataFrame:
    """Baca satu file Excel menjadi DataFrame bersih sesuai jenisnya."""
    if sheet is None:
        cands = candidates(data, kind)
        if not cands:
            need = ", ".join(REQUIRED[kind])
            raise LoaderError(
                f"File {LABEL[kind]} tidak sesuai: tidak ada sheet yang punya kolom wajib ({need}). "
                f"Periksa nama kolom di baris header."
            )
        sheet = cands[0]
    raw = pd.read_excel(io.BytesIO(data), sheet_name=sheet, header=None, dtype=str, keep_default_na=False)
    found = _detect(raw, kind)
    if not found:
        raise LoaderError(f"Sheet '{sheet}' tidak punya kolom wajib untuk {LABEL[kind]}.")
    idx, mapping = found
    body = raw.iloc[idx + 1:]
    df = pd.DataFrame({f: body.iloc[:, c].values for f, c in mapping.items()})
    for c in df.columns:
        df[c] = _clean_text(df[c])
    key = {"il": "item", "inv": "item", "order": "sku", "rr": "item"}[kind]
    df = df[df[key] != ""].reset_index(drop=True)

    if kind == "il":
        for c in ["on_hand", "cap_max", "available", "on_hold", "cap_min"]:
            df[c] = num(df[c]) if c in df else 0.0
            df[c] = df[c].fillna(0.0)
        df["loc_type"] = df["loc_type"].str.upper()
        if "pack_key" not in df:
            df["pack_key"] = ""
        if "owner" not in df:
            df["owner"] = ""
    elif kind == "inv":
        df["available"] = num(df["available"])
        df["on_hand"] = num(df["on_hand"]) if "on_hand" in df else df["available"]
        df["dt"] = parse_dt(df["date_updated"])
        df["status"] = df["status"].str.upper()
    elif kind == "order":
        df["qty"] = num(df["qty_so"]).fillna(0.0)
        ot = df["order_type"].str.lower() if "order_type" in df else pd.Series("", index=df.index)
        df["kind"] = ot.map(lambda v: "TO" if "transfer" in v else "SO")
        df["status"] = df["status"] if "status" in df else ""
        df = df.rename(columns={"sku": "item"})[["item", "qty", "kind", "status"]]
    elif kind == "rr":
        df["rr"] = num(df["rr"])
    return df


# Status order yang barangnya sudah diambil / dikemas / dikirim: sudah keluar dari bin, tidak terkait replenishment lagi.
_DONE_PREFIX = ("picked", "shipped", "loaded", "packed", "in packing", "pack complete", "packing")


def default_included_statuses(statuses) -> list:
    """Status yang secara bawaan ikut dihitung sebagai demand (belum selesai di-pick).
    Status 'Part ...' tetap ikut karena sebagian qty-nya belum diambil."""
    return [st for st in statuses if not norm(st).startswith(_DONE_PREFIX)]
