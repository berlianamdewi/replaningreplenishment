"""Logika bisnis replenishment (sesuai kesepakatan dengan user, lihat README)."""
import math
from dataclasses import dataclass, field

import pandas as pd

WMS_COLUMNS = [
    "TASKDETAILKEY", "TASKTYPE", "STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOID", "TOLOC",
    "QTY", "PRIORITY", "STATUS", "USERKEYOVERRIDE", "RELEASEDATE", "AISLE",
    "ALT_ROUTE1", "ALT_ROUTE2", "OPTIMIZED_ROUTE",
]


@dataclass
class Settings:
    min_reserve: float = 5            # batas minimum stok Reserve agar task dibuat
    p1_below: float = 0.5             # rasio Active/Max di bawah ini -> Prioritas 1
    p2_below: float = 0.8             # rasio di bawah ini -> Prioritas 2, selain itu 3
    wms_priority: dict = field(default_factory=lambda: {1: "5", 2: "5", 3: "5"})
    tasktype: str = "MV"
    task_status: str = "OK"
    active_type: str = "PICK"
    reserve_type: str = "OTHER"
    ok_status: str = "OK"
    round_up: bool = True             # kebutuhan pecahan (RR13) dibulatkan ke atas
    demand_in_need: bool = False      # True: kebutuhan = Max - Active + (SO+TO+RR13). False: Max - Active (cara PPIC 4 Nov)
    demand_override: bool = False     # True: Active < total demand -> masuk plan & Prioritas 1
    only_rr_skus: bool = False        # True: hanya SKU yang ada di file RR13 (daftar SKU RESUME)


@dataclass
class Result:
    sku_report: pd.DataFrame
    tasks: pd.DataFrame
    task_detail: pd.DataFrame
    exceptions: pd.DataFrame
    validations: pd.DataFrame
    summary: dict


def _q(x):
    x = float(x)
    return int(x) if x.is_integer() else round(x, 5)


def priority_of(ratio: float, active: float, total: float, s: Settings) -> int:
    if s.demand_override and active < total:   # stok bin tidak cukup untuk demand -> Prioritas 1
        return 1
    if ratio < s.p1_below:
        return 1
    if ratio < s.p2_below:
        return 2
    return 3


def run_replenishment(il: pd.DataFrame, inv: pd.DataFrame, orders: pd.DataFrame, rr: pd.DataFrame,
                      s: Settings = None) -> Result:
    s = s or Settings()
    exc, vals = [], []

    def add_exc(owner, item, reason, detail="", sev="Error"):
        exc.append({"Tingkat": sev, "Owner": owner, "Item": item, "Alasan": reason, "Detail": detail})

    def add_val(name, n, level, note=""):
        vals.append({"Pemeriksaan": name, "Jumlah": n, "Hasil": "OK" if n == 0 and level != "Info" else level,
                     "Keterangan": note})

    # ---------- Peta tipe lokasi (properti lokasi, dari Item & Location) ----------
    loc_type = (il[il["loc_type"] != ""].groupby("location")["loc_type"]
                .agg(lambda x: x.mode().iat[0]).to_dict())

    inv = inv.copy()
    inv["loc_type"] = inv["location"].map(loc_type)
    il_items = set(il["item"])

    # ---------- Demand: SO + TO + RR13 ----------
    so = orders[orders["kind"] == "SO"].groupby("item")["qty"].sum()
    to = orders[orders["kind"] == "TO"].groupby("item")["qty"].sum()
    rr_s = rr.dropna(subset=["rr"]).groupby("item")["rr"].sum() if len(rr) else pd.Series(dtype=float)

    # ---------- Active BIN (lokasi bertipe PICK) ----------
    pick = il[il["loc_type"] == s.active_type]
    if s.only_rr_skus and len(rr):
        pick = pick[pick["item"].isin(set(rr["item"]))]
    pg = pick.groupby("item").agg(
        n_pick=("location", "nunique"), locs=("location", lambda x: " | ".join(sorted(set(x)))),
        active=("on_hand", "sum"), cap_max=("cap_max", "max"), pack=("pack_key", "first"),
        owner=("owner", "first"))

    # ---------- Sumber Reserve: lokasi OTHER, status OK, Available>0, ada LPN ----------
    ok = inv["status"].eq(s.ok_status.upper())
    res = inv[(inv["loc_type"] == s.reserve_type) & ok & (inv["available"] > 0) & (inv["lpn"] != "")]
    res = res.sort_values(["item", "dt", "location", "lpn"], na_position="last")
    res_by = {k: g for k, g in res.groupby("item", sort=False)}

    # ---------- Validasi data input ----------
    add_val("Date Updated tidak terbaca (baris Inventory Balance)", int(inv["dt"].isna().sum()), "Peringatan",
            "Baris ini diurutkan paling akhir saat memilih LPN.")
    add_val("Available negatif / kosong (baris Inventory Balance)", int((inv["available"].isna() | (inv["available"] < 0)).sum()),
            "Peringatan", "Tidak dipakai sebagai sumber.")
    add_val("Lokasi Inventory Balance tidak ada di Item & Location", int(inv["loc_type"].isna().sum()), "Info",
            "Tipe lokasi tidak diketahui, baris tidak dipakai sebagai sumber.")
    add_val("Baris LPN kosong di lokasi non-PICK", int(((inv["lpn"] == "") & inv["loc_type"].notna() & (inv["loc_type"] != s.active_type)).sum()),
            "Info", "Tidak dianggap Active BIN (Active = tipe PICK).")
    add_val("Baris ber-LPN di lokasi PICK", int(((inv["lpn"] != "") & (inv["loc_type"] == s.active_type)).sum()),
            "Info", "Tidak dipakai sebagai sumber (sumber hanya lokasi tipe OTHER).")
    add_val("SKU di SO/TO tidak ada di Item & Location", len(set(orders["item"]) - il_items), "Peringatan")
    add_val("SKU di file RR13 tidak ada di Item & Location", len(set(rr["item"]) - il_items) if len(rr) else 0, "Peringatan")
    if len(rr) and rr["item"].duplicated().any():
        add_val("SKU ganda di file RR13 (nilainya dijumlahkan)", int(rr["item"].duplicated().sum()), "Peringatan")
    inv_status_other = int((~ok).sum())
    add_val("Baris Inventory Balance berstatus bukan OK (HOLD dll.)", inv_status_other, "Info", "Tidak dipakai sebagai sumber.")

    # Owner Item & Location vs Inventory Balance (hanya jika kedua file mengisi owner)
    owner_bad = set()
    if (il["owner"] != "").any():
        o_il = il[il["owner"] != ""].groupby("item")["owner"].agg(set)
        o_inv = inv.groupby("item")["owner"].agg(set)
        for item in o_il.index.intersection(o_inv.index):
            if not (o_il[item] & o_inv[item]):
                owner_bad.add(item)
                add_exc(", ".join(sorted(o_inv[item])), item, "Owner tidak sesuai antar file",
                        f"Item&Location: {sorted(o_il[item])}, Inventory: {sorted(o_inv[item])}")
    add_val("Owner tidak sesuai antar file", len(owner_bad), "Error")

    # ---------- Evaluasi per SKU ----------
    rows, tasks, anomalies, no_cap = [], [], 0, 0
    for item, r in pg.iterrows():
        so_q, to_q = float(so.get(item, 0.0)), float(to.get(item, 0.0))
        has_rr = item in rr_s.index
        rr_q = float(rr_s.get(item, 0.0))
        total = so_q + to_q + rr_q
        base = {"Owner": r.owner or "", "Item": item, "Kategori": r.pack, "Location BIN": r.locs,
                "Max Cap": r.cap_max, "Stock BIN Active": r.active, "SO": so_q, "TO": to_q,
                "RR13": rr_q, "RR13 ditemukan": "Ya" if has_rr else "Tidak", "Total SO+TO+RR13": total}
        if item in owner_bad:
            continue
        if r.n_pick > 1:
            anomalies += 1
            rows.append({**base, "Status": "Anomali: lebih dari 1 Active BIN", "Keterangan": r.locs})
            add_exc(r.owner or "", item, "Lebih dari satu Active BIN", r.locs)
            continue
        if not r.cap_max or r.cap_max <= 0:
            no_cap += 1
            if total > 0:
                add_exc(r.owner or "", item, "Max Capacity kosong/0 padahal ada demand",
                        f"Total demand {total:g}", "Peringatan")
            continue

        ratio = r.active / r.cap_max
        need_raw = r.cap_max - r.active + (total if s.demand_in_need else 0.0)
        need = math.ceil(need_raw - 1e-9) if s.round_up else need_raw
        qualifies = (ratio < s.p2_below) or (s.demand_override and r.active < total)
        prio = priority_of(ratio, r.active, total, s)
        base.update({"% Active/Max": ratio, "Prioritas": f"Prioritas {prio}", "Kebutuhan Replenish": max(need, 0)})
        if not qualifies or need <= 0:
            rows.append({**base, "Status": "Tidak perlu replenish", "Qty Task": 0, "Jumlah Task": 0})
            continue

        grp = res_by.get(item)
        avail = float(grp["available"].sum()) if grp is not None else 0.0
        base["Stock BIN Reserved"] = avail
        if avail <= 0:
            rows.append({**base, "Status": "Gagal: tidak ada stok Reserve", "Qty Task": 0, "Jumlah Task": 0,
                         "Kekurangan": need})
            add_exc(r.owner or "", item, "Tidak ada stok Reserve yang bisa diambil",
                    f"Kebutuhan {need:g}; Reserve OTHER/OK/Available>0/ber-LPN = 0")
            continue
        if avail < s.min_reserve:
            rows.append({**base, "Status": "Dilewati: Reserve di bawah batas minimum", "Qty Task": 0, "Jumlah Task": 0,
                         "Kekurangan": need})
            add_exc(r.owner or "", item, "Stok Reserve di bawah batas minimum",
                    f"Reserve {avail:g} < batas {s.min_reserve:g}; kebutuhan {need:g}", "Peringatan")
            continue

        target = min(need, avail)
        remaining, n_task = target, 0
        for row in grp.itertuples():
            if remaining <= 0:
                break
            take = min(remaining, float(row.available))
            if take <= 0:
                continue
            tasks.append({
                "STORERKEY": row.owner, "SKU": item, "FROMLOC": row.location, "LOT": row.lot, "FROMID": row.lpn,
                "TOLOC": r.locs, "QTY": _q(take), "_prio": prio, "_avail": float(row.available),
                "_date": row.dt, "_need": target,
            })
            remaining -= take
            n_task += 1
        short = max(0.0, need - avail)
        status = "Task dibuat" if short <= 0 else "Task dibuat: stok Reserve tidak cukup"
        rows.append({**base, "Status": status, "Qty Task": _q(target), "Jumlah Task": n_task,
                     "Kekurangan": short,
                     "Keterangan": f"Kurang {short:g} pcs dari kebutuhan" if short > 0 else ""})
        if short > 0:
            add_exc(r.owner or "", item, "Stok Reserve tidak mencukupi",
                    f"Kebutuhan {need:g}, task dibuat {target:g}, kurang {short:g}", "Peringatan")

    # SKU punya demand tapi tidak punya Active BIN / tidak ada di Item & Location
    pick_items = set(pg.index)
    demand_items = (set(so.index) | set(to.index) | set(rr_s.index))
    no_active = 0
    for item in sorted(demand_items - pick_items):
        total = float(so.get(item, 0)) + float(to.get(item, 0)) + float(rr_s.get(item, 0))
        if total <= 0:
            continue
        if item in il_items:
            no_active += 1
            add_exc("", item, "SKU tidak punya Active BIN (tipe PICK)", f"Total demand {total:g}")
        # SKU yang tidak ada sama sekali di Item & Location sudah dihitung di validasi
    add_val("SKU dengan demand tapi tanpa Active BIN", no_active, "Error")
    add_val("SKU dengan lebih dari satu Active BIN", anomalies, "Error", "Task tidak dibuat; lokasi tidak dipilih sembarangan.")
    add_val("SKU punya Active BIN tapi Max Capacity 0/kosong (dilewati)", no_cap, "Info")
    rr_missing = sum(1 for r_ in rows if r_.get("RR13 ditemukan") == "Tidak")
    add_val("SKU diproses tanpa nilai RR13 (dianggap 0)", rr_missing, "Info")

    # ---------- Susun task + validasi akhir ----------
    td = pd.DataFrame(tasks)
    if len(td):
        td = td.sort_values(["_prio", "SKU", "_date"], na_position="last").reset_index(drop=True)
        # validasi: QTY <= Available per LPN, total per SKU <= target, tidak ada duplikat
        bad = set()
        for _, t in td.iterrows():
            if t["QTY"] > t["_avail"] + 1e-9 or t["QTY"] <= 0:
                bad.add(t["SKU"])
        tot = td.groupby("SKU").agg(q=("QTY", "sum"), need=("_need", "first"))
        bad |= set(tot[tot["q"] > tot["need"] + 1e-9].index)
        dup = td[td.duplicated(["SKU", "FROMLOC", "FROMID", "LOT"], keep=False)]
        bad |= set(dup["SKU"])
        for item in bad:
            add_exc("", item, "Validasi internal gagal, task dibatalkan", "QTY melebihi Available/kebutuhan atau duplikat")
        add_val("Task gagal validasi internal (QTY > Available/kebutuhan, duplikat)", len(bad), "Error")
        td = td[~td["SKU"].isin(bad)].reset_index(drop=True)
    else:
        add_val("Task gagal validasi internal (QTY > Available/kebutuhan, duplikat)", 0, "Error")

    wms = pd.DataFrame(columns=WMS_COLUMNS)
    if len(td):
        wms = pd.DataFrame({c: "" for c in WMS_COLUMNS}, index=td.index)
        wms["TASKTYPE"] = s.tasktype
        wms["STORERKEY"] = td["STORERKEY"]
        wms["SKU"] = td["SKU"]
        wms["FROMLOC"] = td["FROMLOC"]
        wms["LOT"] = td["LOT"]
        wms["FROMID"] = td["FROMID"]
        wms["TOLOC"] = td["TOLOC"]
        wms["QTY"] = td["QTY"]
        wms["PRIORITY"] = td["_prio"].map(lambda p: str(s.wms_priority.get(int(p), "5")))
        wms["STATUS"] = s.task_status
        required = ["TASKTYPE", "STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOLOC", "QTY", "PRIORITY", "STATUS"]
        miss = wms[required].apply(lambda col: col.astype(str).str.strip().eq("")).any(axis=1)
        add_val("Task dengan kolom wajib WMS kosong", int(miss.sum()), "Error")
        # task dibuang jika kolom wajib kosong
        keep = ~miss
        wms, td = wms[keep].reset_index(drop=True), td[keep].reset_index(drop=True)

    # tandai SKU yang task-nya dibatalkan setelah validasi
    created = set(td["SKU"]) if len(td) else set()
    for r_ in rows:
        if r_.get("Status", "").startswith("Task dibuat") and r_["Item"] not in created:
            r_["Status"] = "Gagal: validasi internal"
            r_["Qty Task"], r_["Jumlah Task"] = 0, 0

    report = pd.DataFrame(rows)
    cols = ["Owner", "Item", "Kategori", "Location BIN", "Max Cap", "Stock BIN Active", "Stock BIN Reserved", "SO", "TO",
            "RR13", "RR13 ditemukan", "Total SO+TO+RR13", "Kebutuhan Replenish", "Qty Task", "Kekurangan",
            "% Active/Max", "Prioritas", "Jumlah Task", "Status", "Keterangan"]
    if len(report):
        for c in cols:
            if c not in report:
                report[c] = ""
        report = report[cols]
        report["_p"] = report["Prioritas"].fillna("Z")
        report = report.sort_values(["_p", "Item"]).drop(columns="_p").reset_index(drop=True)
    else:
        report = pd.DataFrame(columns=cols)

    detail = pd.DataFrame()
    if len(td):
        detail = td.assign(**{"Prioritas": td["_prio"].map(lambda p: f"Prioritas {p}")})
        detail = detail.rename(columns={"_avail": "Available LPN", "_date": "Date Updated LPN"})[
            ["Prioritas", "STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOLOC", "QTY", "Available LPN", "Date Updated LPN"]]

    st_ = report["Status"] if len(report) else pd.Series(dtype=str)
    exc_df = pd.DataFrame(exc, columns=["Tingkat", "Owner", "Item", "Alasan", "Detail"])
    failed = int(st_.str.startswith(("Gagal", "Dilewati")).sum())
    summary = {
        "Total SKU diproses": int(len(report)) + no_cap,
        "SKU butuh replenish": int((st_ != "Tidak perlu replenish").sum()) - anomalies,
        "Task dibuat": int(len(wms)),
        "Total qty replenishment": _q(wms["QTY"].sum()) if len(wms) else 0,
        "SKU stok Reserve tidak cukup": int(st_.str.contains("tidak cukup").sum()),
        "SKU anomali": anomalies + no_active + len(owner_bad),
        "SKU gagal dibuat task": failed,
    }
    return Result(report, wms, detail, exc_df, pd.DataFrame(vals), summary)
