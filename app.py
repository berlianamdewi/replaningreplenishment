from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import replen_logic as rl

DB_PATH = Path(__file__).parent / "snapshots.db"

st.set_page_config(page_title="Rekomendasi Replenishment", layout="wide")
st.title("Rekomendasi Replenishment Pick Face")
st.caption("Upload 2 file Excel dari ERP. Tool hanya memberi rekomendasi, tidak menulis balik ke sistem.")

with st.sidebar:
    st.header("1. Upload data")
    inv_file = st.file_uploader("Inventory view (Excel)", type=["xlsx", "xls"])
    ord_file = st.file_uploader("Sales Order Progress Summary (Excel)", type=["xlsx", "xls"])
    sku_conv_file = st.file_uploader(
        "Opsional: konversi UOM per SKU (kolom SKU, UOM, PCS_per_UOM)", type=["xlsx", "xls"])
    st.markdown("---")
    st.caption("Aturan: Proyeksi = Available PICK - Qty Didn't Allocated. "
               "Replen jika Proyeksi < Min. Qty = isi sampai Max, dibatasi ruang bin dan reserve.")

if not (inv_file and ord_file):
    st.info("Upload kedua file di sidebar untuk mulai.")
    st.stop()


def resolve_mapping(df, fields, fuzzy, required, labels, prefix):
    found = rl.detect_columns(df.columns, fields, fuzzy)
    missing = [k for k in required if k not in found]
    if missing:
        st.warning("Beberapa kolom tidak terdeteksi otomatis. Pilih manual:")
        for k in missing:
            choice = st.selectbox(labels[k], ["- pilih -"] + list(df.columns), key=f"{prefix}_{k}")
            if choice != "- pilih -":
                found[k] = choice
    if any(k not in found for k in required):
        st.stop()
    return found


# ------------------------------------------------------------------ baca file
try:
    inv_raw = rl.read_excel_smart(inv_file, rl.INV_FIELDS)
    ord_raw = rl.read_excel_smart(ord_file, rl.ORD_FIELDS)
except Exception as e:  # noqa: BLE001
    st.error(f"File tidak bisa dibaca sebagai Excel: {e}")
    st.stop()

with st.expander("Cek pembacaan kolom", expanded=False):
    st.write("Kolom inventory:", list(inv_raw.columns))
    st.write("Kolom order:", list(ord_raw.columns))

inv_map = resolve_mapping(inv_raw, rl.INV_FIELDS, None, rl.INV_REQUIRED, rl.INV_LABELS, "inv")
ord_map = resolve_mapping(ord_raw, rl.ORD_FIELDS, rl.ORD_FUZZY, rl.ORD_REQUIRED, rl.ORD_LABELS, "ord")

inv = rl.prepare_inventory(inv_raw, inv_map)
orders = rl.prepare_orders(ord_raw, ord_map)

loc_types = sorted(inv["loc_type"].unique())
if not {"PICK", "OTHER"} <= set(loc_types):
    st.warning(f"Location Type yang ditemukan: {loc_types}. Tool mengharapkan PICK dan OTHER.")

# ------------------------------------------------------------------ konversi UOM
st.subheader("2. Konversi UOM ke PCS")
need = rl.uoms_needing_factor(orders)
factors = {}
if need.empty:
    st.write("Tidak ada order yang belum dialokasi.")
else:
    need["PCS per 1 UOM"] = [1.0 if u == "PCS" else None for u in need["uom"]]
    st.caption("Isi berapa PCS dalam 1 UOM (mis. 1 BOX = berapa PCS). Kosong = belum diketahui, "
               "barisnya tidak dihitung dan muncul di sheet 'UOM belum ada konversi'.")
    edited = st.data_editor(
        need.rename(columns={"uom": "UOM", "baris": "Jumlah baris", "total_qty": "Total qty"}),
        disabled=["UOM", "Jumlah baris", "Total qty"], hide_index=True, key="uom_editor",
        column_config={"PCS per 1 UOM": st.column_config.NumberColumn(min_value=0.0)},
    )
    factors = {r["UOM"]: r["PCS per 1 UOM"] for _, r in edited.iterrows() if pd.notna(r["PCS per 1 UOM"])}

sku_factors = {}
if sku_conv_file:
    sc = rl.read_excel_smart(sku_conv_file, {"sku": ["sku", "item"], "uom": ["uom"],
                                             "f": ["pcsperuom", "factor", "konversi"]})
    cm = rl.detect_columns(sc.columns, {"sku": ["sku", "item"], "uom": ["uom"],
                                        "f": ["pcsperuom", "factor", "konversi"]})
    if len(cm) == 3:
        for _, r in sc.iterrows():
            f = pd.to_numeric(r[cm["f"]], errors="coerce")
            if pd.notna(f):
                sku_factors[(str(r[cm["sku"]]).strip().upper(), str(r[cm["uom"]]).strip().upper())] = float(f)
    else:
        st.warning("File konversi per SKU harus punya kolom SKU, UOM, PCS_per_UOM. File diabaikan.")

# ------------------------------------------------------------------ hitung
demand, unresolved = rl.build_demand(orders, factors, sku_factors)
res = rl.compute(inv, demand)
rec, all_df = res["rec"], res["all"]

n_check = len(res["cek"]) + len(res["no_pick"]) + len(unresolved)
c1, c2, c3, c4 = st.columns(4)
c1.metric("SKU dengan bin PICK", len(all_df))
c2.metric("Perlu replen", len(rec))
c3.metric("Prioritas tinggi", int((rec["priority"] == "Tinggi").sum()))
c4.metric("Perlu dicek", n_check)

st.subheader("3. Hasil")
tab1, tab2, tab3 = st.tabs(["Rekomendasi replen", "Semua SKU", "Perlu dicek"])
with tab1:
    st.caption("Prioritas Tinggi = proyeksi PICK sudah negatif (alokasi berikutnya akan jatuh ke reserve).")
    st.dataframe(rl.view(rec), use_container_width=True, hide_index=True)
with tab2:
    st.dataframe(rl.view(all_df), use_container_width=True, hide_index=True)
with tab3:
    st.write("SKU dengan catatan:")
    st.dataframe(rl.view(res["cek"]), use_container_width=True, hide_index=True)
    if len(res["no_pick"]):
        st.write("Ada kebutuhan tapi tidak ada baris PICK:")
        st.dataframe(res["no_pick"], use_container_width=True, hide_index=True)
    if len(unresolved):
        st.write("Baris order yang UOM-nya belum ada konversi (tidak dihitung):")
        st.dataframe(unresolved, use_container_width=True, hide_index=True)

# ------------------------------------------------------------------ output
st.subheader("4. Download dan simpan")
stamp = datetime.now().strftime("%Y%m%d_%H%M")
col_a, col_b = st.columns(2)
col_a.download_button(
    "Download hasil (Excel)", rl.to_excel_bytes(res, unresolved),
    file_name=f"replen_{stamp}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
if col_b.button("Simpan snapshot stok ini ke database"):
    n = rl.save_snapshot(DB_PATH, res, datetime.now())
    st.success(f"{n} baris SKU disimpan ke {DB_PATH.name}. Simpan sekali per export supaya histori tidak dobel.")
