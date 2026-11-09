"""Automated WMS Replenishment Generator (Streamlit)."""
import os

import pandas as pd
import streamlit as st

from replen import loaders
from replen.engine import Settings, run_replenishment
from replen.export import exception_book, report_book, write_wms

st.set_page_config(page_title="WMS Replenishment Generator", layout="wide")
st.title("Automated WMS Replenishment Generator")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "template", "WMS_Template.xlsx")


@st.cache_data(show_spinner=False)
def _candidates(data: bytes, kind: str):
    return loaders.candidates(data, kind)


@st.cache_data(show_spinner=False)
def _load(data: bytes, kind: str, sheet):
    return loaders.load(data, kind, sheet)


def pick_sheet(label, data, kind, key):
    cands = _candidates(data, kind)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    return st.selectbox(f"Sheet untuk {label}", cands, key=key)


def read_one(label, kind, uploaded, key):
    """Baca satu file; tampilkan pesan jika tidak sesuai."""
    data = uploaded.getvalue()
    sheet = pick_sheet(f"{label} ({uploaded.name})", data, kind, f"sheet_{key}_{uploaded.name}")
    try:
        df = _load(data, kind, sheet)
    except loaders.LoaderError as e:
        st.error(f"{uploaded.name}: {e}")
        return None
    st.success(f"{uploaded.name} (sheet {sheet}): {len(df):,} baris terbaca")
    return df


# ---------------- Sidebar: pengaturan ----------------
with st.sidebar:
    st.header("Pengaturan")
    min_res = st.number_input("Batas minimum stok Reserve (pcs)", min_value=0.0, value=5.0, step=1.0,
                              help="SKU dengan Reserve di bawah batas ini tidak dibuatkan task (masuk laporan exception).")
    p1 = st.slider("Prioritas 1 jika Active/Max di bawah", 0.0, 1.0, 0.5, 0.05)
    p2 = st.slider("Prioritas 2 jika Active/Max di bawah", 0.0, 1.0, 0.8, 0.05)
    st.caption("PRIORITY di file WMS (template contoh berisi 5)")
    w1 = st.text_input("Nilai PRIORITY untuk Prioritas 1", "5")
    w2 = st.text_input("Nilai PRIORITY untuk Prioritas 2", "5")
    w3 = st.text_input("Nilai PRIORITY untuk Prioritas 3", "5")
    round_up = st.checkbox("Bulatkan kebutuhan ke atas (RR13 berpecahan)", value=True)
    st.caption("Cara hitung (bawaan = sama dengan rencana PPIC 4 Nov)")
    dem_need = st.checkbox("Tambahkan SO+TO+RR13 ke kebutuhan", value=False,
                           help="Kebutuhan = Max - Active + demand. Nonaktif: Max - Active.")
    dem_over = st.checkbox("Active < demand otomatis Prioritas 1", value=False)
    only_rr = st.checkbox("Hanya SKU yang ada di file RR13", value=False,
                          help="Meniru daftar SKU di workbook PPIC (sheet RESUME).")

settings = Settings(min_reserve=min_res, p1_below=p1, p2_below=p2, wms_priority={1: w1, 2: w2, 3: w3}, round_up=round_up,
                    demand_in_need=dem_need, demand_override=dem_over, only_rr_skus=only_rr)

# ---------------- Upload ----------------
st.subheader("1. Upload data")
c1, c2 = st.columns(2)
with c1:
    f_il = st.file_uploader("Item & Location (Excel)", type=["xlsx"], key="il")
    f_inv = st.file_uploader("Inventory Balance (Excel)", type=["xlsx"], key="inv")
with c2:
    f_ord = st.file_uploader("Sales Order / Transfer Order (boleh lebih dari satu file)", type=["xlsx"],
                             accept_multiple_files=True, key="ord")
    f_rr = st.file_uploader("Running Rate RR13 (kolom RR13 / Forecast, per SKU)", type=["xlsx"], key="rr")
f_tpl = st.file_uploader("Template WMS (opsional, jika kosong dipakai template bawaan)", type=["xlsx"], key="tpl")

il = inv = rr = None
orders = pd.DataFrame(columns=["item", "qty", "kind"])
if f_il:
    il = read_one("Item & Location", "il", f_il, "il")
if f_inv:
    inv = read_one("Inventory Balance", "inv", f_inv, "inv")
if f_ord:
    parts = [read_one("SO/TO", "order", f, "ord") for f in f_ord]
    parts = [p for p in parts if p is not None]
    if parts:
        orders = pd.concat(parts, ignore_index=True)
        stats = sorted(s_ for s_ in orders["status"].unique() if s_ != "")
        if stats:
            summ = orders.groupby("status")["qty"].agg(["count", "sum"]).rename(columns={"count": "baris", "sum": "total qty"})
            st.caption("Status order di file SO/TO:")
            st.dataframe(summ, use_container_width=False)
            chosen = st.multiselect(
                "Status order yang dihitung sebagai demand (status yang sudah di-pick/pack/kirim tidak dipilih bawaan)",
                stats, default=loaders.default_included_statuses(stats), key="order_status")
            dropped = orders[~orders["status"].isin(chosen) & (orders["status"] != "")]
            st.caption(f"Dikeluarkan dari demand: {len(dropped):,} baris, {dropped['qty'].sum():,.0f} pcs.")
            orders = orders[orders["status"].isin(chosen) | (orders["status"] == "")].reset_index(drop=True)
if f_rr:
    rr = read_one("RR13", "rr", f_rr, "rr")
if rr is None:
    rr = pd.DataFrame(columns=["item", "rr"])

# ---------------- Preview ----------------
if il is not None or inv is not None:
    st.subheader("2. Preview data")
    t1, t2, t3, t4 = st.tabs(["Item & Location", "Inventory Balance", "SO / TO", "RR13"])
    with t1:
        if il is not None:
            st.write(f"{len(il):,} baris, {il['item'].nunique():,} SKU. Tipe lokasi: {il['loc_type'].value_counts().to_dict()}")
            st.dataframe(il.head(100), use_container_width=True)
    with t2:
        if inv is not None:
            st.write(f"{len(inv):,} baris, {inv['item'].nunique():,} SKU. "
                     f"Date Updated tidak terbaca: {int(inv['dt'].isna().sum())}. LPN kosong: {int((inv['lpn']=='').sum()):,}")
            st.dataframe(inv.head(100), use_container_width=True)
    with t3:
        st.write(f"{len(orders):,} baris order")
        st.dataframe(orders.head(100), use_container_width=True)
    with t4:
        st.write(f"{len(rr):,} baris; terisi: {int(rr['rr'].notna().sum()) if len(rr) else 0:,}")
        st.dataframe(rr.head(100), use_container_width=True)

# ---------------- Generate ----------------
st.subheader("3. Generate replenishment")
ready = il is not None and inv is not None
if not ready:
    st.info("Upload minimal Item & Location dan Inventory Balance untuk memulai.")
if st.button("Generate", type="primary", disabled=not ready):
    with st.spinner("Menghitung..."):
        st.session_state["result"] = run_replenishment(il, inv, orders, rr, settings)
        st.session_state["template"] = f_tpl.getvalue() if f_tpl else open(TEMPLATE_PATH, "rb").read()

res = st.session_state.get("result")
if res is not None:
    st.subheader("4. Hasil")
    cols = st.columns(len(res.summary))
    for col, (k, v) in zip(cols, res.summary.items()):
        col.metric(k, f"{v:,}")
    tab1, tab2, tab3, tab4 = st.tabs(["Task WMS", "Ringkasan SKU", "Exception", "Validasi"])
    with tab1:
        st.dataframe(res.tasks, use_container_width=True)
    with tab2:
        st.dataframe(res.sku_report, use_container_width=True)
    with tab3:
        st.dataframe(res.exceptions, use_container_width=True)
    with tab4:
        st.dataframe(res.validations, use_container_width=True)

    st.subheader("5. Download")
    d1, d2, d3 = st.columns(3)
    try:
        d1.download_button("Excel output WMS (siap upload)", write_wms(res.tasks, st.session_state["template"]),
                           "WMS_Replenishment.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:  # template tidak sesuai
        d1.error(f"Gagal membuat file WMS: {e}")
    d2.download_button("Laporan hasil replenishment", report_book(res), "Laporan_Replenishment.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d3.download_button("Laporan exception & validasi", exception_book(res), "Laporan_Exception.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
