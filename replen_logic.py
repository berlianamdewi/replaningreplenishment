"""Logika rekomendasi replenishment pick face.

Input : export Excel Inventory view + export Excel Sales Order Progress Summary.
Output: daftar SKU yang perlu direplen (qty dalam PCS), plus daftar yang perlu dicek.

Aturan (per SKU, 1 bin PICK per SKU):
  projected = Available_PICK - Qty_Didnt_Allocated (dalam PCS)
  trigger   = projected < Min  (dan Max sudah diset)
  qty       = min(Max - OnHand_PICK, Available_OTHER)   -> isi sampai max
"""
import re
import sqlite3
from io import BytesIO

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- baca Excel
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


INV_FIELDS = {
    "item": ["item", "sku", "itemcode"],
    "location": ["location", "loc"],
    "on_hand": ["onhand"],
    "available": ["available"],
    "allocated": ["allocated"],
    "on_hold": ["onhold"],
    "cap_min": ["capacityminimum", "capminimum"],
    "cap_max": ["capacitymaximum", "capmaximum"],
    "loc_type": ["locationtype", "loctype"],
    "description": ["description"],
}
INV_REQUIRED = ["item", "location", "on_hand", "available", "cap_min", "cap_max", "loc_type"]
INV_LABELS = {
    "item": "Item / SKU", "location": "Location", "on_hand": "On Hand",
    "available": "Available", "cap_min": "Capacity (Minimum)",
    "cap_max": "Capacity (Maximum)", "loc_type": "Location Type",
}

ORD_FIELDS = {
    "sku": ["sku", "item", "itemcode"],
    "uom": ["uom"],
    "reason": ["reasoncode"],
    "status": ["status"],
    "qty_so": ["qtyso"],
    "order_no": ["ordernavnumber", "orderno"],
}
# "Qty Didn't Allocated" -> "qtydidntallocated"
ORD_FUZZY = {"qty_not_alloc": lambda n: "alloc" in n and ("didn" in n or "not" in n or "un" in n)}
ORD_REQUIRED = ["sku", "qty_not_alloc", "uom"]
ORD_LABELS = {"sku": "SKU", "qty_not_alloc": "Qty Didn't Allocated", "uom": "UOM"}


def _headers(vals):
    out, seen = [], {}
    for j, v in enumerate(vals):
        h = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
        if not h:
            h = f"kolom_{j + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h}.{seen[h]}"
        else:
            seen[h] = 0
        out.append(h)
    return out


def read_excel_smart(file, fields, sheet_name=0, scan_rows=20):
    """Baca Excel; baris header dicari otomatis (kalau ada judul/baris kosong di atas)."""
    raw = pd.read_excel(file, header=None, sheet_name=sheet_name, dtype=object)
    best_i, best_hits = 0, -1
    for i in range(min(scan_rows, len(raw))):
        row = [_norm(v) for v in raw.iloc[i].tolist()]
        hits = sum(1 for aliases in fields.values() if any(r in aliases for r in row))
        if hits > best_hits:
            best_i, best_hits = i, hits
    df = raw.iloc[best_i + 1:].copy()
    df.columns = _headers(raw.iloc[best_i].tolist())
    return df.dropna(how="all").reset_index(drop=True)


def detect_columns(columns, fields, fuzzy=None):
    """Cocokkan nama kolom asli ke nama standar. Return {nama_standar: nama_kolom_asli}."""
    normed = {c: _norm(c) for c in columns}
    found = {}
    for key, aliases in fields.items():
        for c, n in normed.items():
            if n in aliases and c not in found.values():
                found[key] = c
                break
    for key, fn in (fuzzy or {}).items():
        for c, n in normed.items():
            if c not in found.values() and fn(n):
                found[key] = c
                break
    return found


def _num(s):
    if s.dtype == object:
        s = s.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _txt(s):
    return s.astype(str).str.strip().str.upper()


# ---------------------------------------------------------------- siapkan data
def prepare_inventory(df, m):
    d = pd.DataFrame({
        "sku": _txt(df[m["item"]]),
        "location": df[m["location"]].astype(str).str.strip(),
        "on_hand": _num(df[m["on_hand"]]),
        "available": _num(df[m["available"]]),
        "cap_min": _num(df[m["cap_min"]]),
        "cap_max": _num(df[m["cap_max"]]),
        "loc_type": _txt(df[m["loc_type"]]),
        "description": df[m["description"]].astype(str).str.strip() if "description" in m else "",
    })
    return d[~d["sku"].isin(["", "NAN", "NONE"])].reset_index(drop=True)


def prepare_orders(df, m):
    d = pd.DataFrame({
        "sku": _txt(df[m["sku"]]),
        "uom": _txt(df[m["uom"]]),
        "qty_na": _num(df[m["qty_not_alloc"]]),
        "reason": df[m["reason"]].astype(str).str.lower() if "reason" in m else "",
        "status": df[m["status"]].astype(str).str.lower() if "status" in m else "",
    })
    d = d[~d["sku"].isin(["", "NAN", "NONE"])]
    d["cancelled"] = d["reason"].str.contains("cancel") | d["status"].str.contains("cancel")
    return d.reset_index(drop=True)


def active_demand_rows(orders):
    """Baris order yang masih jadi kebutuhan: belum dialokasi dan tidak di-cancel."""
    return orders[(~orders["cancelled"]) & (orders["qty_na"] > 0)].copy()


def uoms_needing_factor(orders):
    a = active_demand_rows(orders)
    g = a.groupby("uom", as_index=False).agg(baris=("sku", "count"), total_qty=("qty_na", "sum"))
    return g.sort_values("uom").reset_index(drop=True)


def build_demand(orders, uom_factors, sku_factors=None):
    """Konversi qty 'belum dialokasi' ke PCS.

    uom_factors : {"PCS": 1.0, "BOX": 12.0}   (PCS per 1 UOM)
    sku_factors : {("SKU", "BOX"): 24.0}      (opsional, menimpa uom_factors)
    Return (demand per SKU, baris yang konversinya belum ada).
    """
    a = active_demand_rows(orders)
    sku_factors = sku_factors or {}

    def f(r):
        v = sku_factors.get((r["sku"], r["uom"]))
        return uom_factors.get(r["uom"]) if v is None else v

    a["factor"] = a.apply(f, axis=1) if len(a) else pd.Series(dtype=float)
    a["factor"] = pd.to_numeric(a["factor"], errors="coerce")
    ok = a[a["factor"].notna()].copy()
    ok["demand_pcs"] = ok["qty_na"] * ok["factor"]
    demand = ok.groupby("sku", as_index=False).agg(demand_pcs=("demand_pcs", "sum"))
    unresolved = a[a["factor"].isna()][["sku", "uom", "qty_na"]].rename(
        columns={"qty_na": "qty_belum_dialokasi"})
    return demand, unresolved.reset_index(drop=True)


# ---------------------------------------------------------------- hitung
def _fmt(x):
    return f"{x:g}"


def _suggest(res_map, sku, qty):
    out, left = [], qty
    for loc, av in res_map.get(sku, []):
        if left <= 0:
            break
        take = min(av, left)
        out.append(f"{loc} ({_fmt(take)})")
        left -= take
    return ", ".join(out)


def compute(inv, demand):
    pick = inv[inv["loc_type"] == "PICK"]
    other = inv[(inv["loc_type"] == "OTHER") & (inv["available"] > 0)]

    pick_g = pick.groupby("sku", as_index=False).agg(
        description=("description", "first"),
        pick_location=("location", lambda x: ", ".join(x)),
        n_pick=("location", "count"),
        on_hand=("on_hand", "sum"), available=("available", "sum"),
        cap_min=("cap_min", "sum"), cap_max=("cap_max", "sum"),
    )
    res_g = other.groupby("sku", as_index=False).agg(reserve_available=("available", "sum"))
    res_map = {s: list(zip(g["location"], g["available"]))
               for s, g in other.sort_values("available", ascending=False).groupby("sku")}

    df = pick_g.merge(res_g, on="sku", how="left").merge(demand, on="sku", how="left")
    df[["reserve_available", "demand_pcs"]] = df[["reserve_available", "demand_pcs"]].fillna(0.0)

    df["projected"] = df["available"] - df["demand_pcs"]
    df["space"] = df["cap_max"] - df["on_hand"]
    df["trigger"] = (df["cap_max"] > 0) & (df["projected"] < df["cap_min"])
    qty = np.minimum(df["space"].clip(lower=0), df["reserve_available"])
    df["qty_replen"] = np.where(df["trigger"], qty, 0.0)
    df["priority"] = np.where(~df["trigger"], "-", np.where(df["projected"] < 0, "Tinggi", "Sedang"))
    df["gap"] = df["projected"] - df["cap_min"]
    df["source"] = [_suggest(res_map, s, q) if q > 0 else "" for s, q in zip(df["sku"], df["qty_replen"])]

    notes = []
    for r in df.itertuples():
        n = []
        if r.n_pick > 1:
            n.append(f"{r.n_pick} bin PICK untuk SKU ini (angka dijumlah)")
        if r.cap_max <= 0:
            n.append("Min/Max bin PICK belum diset" + (" padahal ada kebutuhan" if r.demand_pcs > 0 else ""))
        if r.trigger and r.reserve_available <= 0:
            n.append("Perlu replen tapi tidak ada stok reserve tersedia")
        elif r.trigger and r.space <= 0:
            n.append("Bin PICK sudah penuh, kebutuhan melebihi isi bin")
        elif r.trigger:
            after = r.projected + r.qty_replen
            if after < 0:
                why = "reserve habis" if r.qty_replen >= r.reserve_available else "kapasitas bin penuh"
                n.append(f"Setelah replen masih kurang {_fmt(-after)} PCS ({why})")
        if r.cap_max > 0 and r.demand_pcs > r.cap_max:
            n.append("Kebutuhan > Max bin: cek apakah Max terlalu kecil")
        notes.append("; ".join(n))
    df["note"] = notes

    # SKU yang punya kebutuhan tapi tidak punya baris PICK di data inventory
    no_pick = demand[~demand["sku"].isin(df["sku"])].merge(res_g, on="sku", how="left")
    no_pick["reserve_available"] = no_pick["reserve_available"].fillna(0.0)
    no_pick["note"] = ("Ada kebutuhan tapi tidak ada baris PICK di data inventory "
                       "(cek filter export atau SKU belum punya bin PICK)")

    rec = df[df["qty_replen"] > 0].sort_values(["gap", "sku"]).reset_index(drop=True)
    cek = df[df["note"] != ""].sort_values("sku").reset_index(drop=True)
    return {"all": df.sort_values("sku").reset_index(drop=True),
            "rec": rec, "cek": cek, "no_pick": no_pick.reset_index(drop=True)}


# ---------------------------------------------------------------- tampilan / output
REC_COLS = [
    ("sku", "SKU"), ("description", "Deskripsi"), ("pick_location", "Bin PICK (tujuan)"),
    ("qty_replen", "Qty replen (PCS)"), ("source", "Ambil dari reserve (saran)"),
    ("priority", "Prioritas"), ("on_hand", "On Hand PICK"), ("available", "Available PICK"),
    ("demand_pcs", "Belum dialokasi (PCS)"), ("projected", "Proyeksi PICK"),
    ("cap_min", "Min"), ("cap_max", "Max"), ("reserve_available", "Reserve tersedia"),
    ("note", "Catatan"),
]


def view(df, cols=REC_COLS):
    return df[[c for c, _ in cols]].rename(columns=dict(cols))


def to_excel_bytes(res, unresolved):
    cek = pd.concat([
        view(res["cek"]),
        res["no_pick"].rename(columns={
            "sku": "SKU", "demand_pcs": "Belum dialokasi (PCS)",
            "reserve_available": "Reserve tersedia", "note": "Catatan"}),
    ], ignore_index=True)
    sheets = {
        "Rekomendasi replen": view(res["rec"]),
        "Semua SKU": view(res["all"]),
        "Perlu dicek": cek,
        "UOM belum ada konversi": unresolved,
    }
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, d in sheets.items():
            d.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for i, col in enumerate(d.columns, 1):
                width = max([len(str(col))] + [len(str(v)) for v in d[col].head(200)]) + 2
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width, 60)
    return buf.getvalue()


# ---------------------------------------------------------------- snapshot
def save_snapshot(db_path, res, ts):
    d = res["all"][["sku", "pick_location", "on_hand", "available", "reserve_available",
                    "demand_pcs", "cap_min", "cap_max", "qty_replen"]].copy()
    d.insert(0, "snapshot_ts", ts.strftime("%Y-%m-%d %H:%M:%S"))
    with sqlite3.connect(db_path) as con:
        d.to_sql("snapshot_sku", con, if_exists="append", index=False)
    return len(d)
