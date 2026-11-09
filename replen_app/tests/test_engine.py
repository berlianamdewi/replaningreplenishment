import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pandas as pd
from replen.engine import run_replenishment, Settings

DEM = Settings(demand_in_need=True, demand_override=True)


def il(rows):  # (item, loc, type, on_hand, cap_max)
    return pd.DataFrame([{"item": i, "location": l, "loc_type": t, "on_hand": oh, "cap_max": cm, "available": oh,
                          "on_hold": 0, "cap_min": 0, "pack_key": "HPL-1", "owner": ""} for i, l, t, oh, cm in rows])


def inv(rows):  # (item, loc, lpn, avail, status, date)
    return pd.DataFrame([{"owner": "TACO", "item": i, "lot": "0000000001", "location": l, "lpn": lp, "available": a,
                          "on_hand": a, "status": st, "dt": pd.Timestamp(d)} for i, l, lp, a, st, d in rows])


def orders(rows=()):
    return pd.DataFrame(list(rows), columns=["item", "qty", "kind"])


RR0 = pd.DataFrame(columns=["item", "rr"])


def test_multi_lpn_oldest_first_exact_qty():
    r = run_replenishment(il([("A", "P1", "PICK", 0, 100), ("A", "R1", "OTHER", 60, 0), ("A", "R2", "OTHER", 70, 0), ("A", "R3", "OTHER", 50, 0)]),
                          inv([("A", "R1", "L1", 60, "OK", "2026-05-09"), ("A", "R2", "L2", 70, "OK", "2026-06-24"), ("A", "R3", "L3", 50, "OK", "2026-10-30")]),
                          orders(), RR0)
    assert list(r.tasks["FROMID"]) == ["L1", "L2"]
    assert r.tasks["QTY"].sum() == 100 and list(r.tasks["QTY"]) == [60, 40]
    assert (r.tasks["TOLOC"] == "P1").all() and (r.tasks["TOID"] == "").all()


def test_insufficient_stock_is_partial_and_reported():
    r = run_replenishment(il([("A", "P1", "PICK", 0, 100), ("A", "R1", "OTHER", 75, 0)]),
                          inv([("A", "R1", "L1", 75, "OK", "2026-05-09")]), orders(), RR0)
    assert r.tasks["QTY"].sum() == 75
    assert r.sku_report.iloc[0]["Kekurangan"] == 25
    assert (r.exceptions["Alasan"] == "Stok Reserve tidak mencukupi").any()


def test_hold_and_pick_lpn_not_used():
    r = run_replenishment(il([("A", "P1", "PICK", 0, 100), ("A", "R1", "OTHER", 50, 0), ("A", "P1", "PICK", 0, 100)]).drop_duplicates(["item", "location"]),
                          inv([("A", "R1", "L1", 50, "HOLD", "2026-05-09"), ("A", "P1", "LX", 40, "OK", "2026-05-09")]), orders(), RR0)
    assert len(r.tasks) == 0
    assert (r.exceptions["Alasan"] == "Tidak ada stok Reserve yang bisa diambil").any()


def test_no_active_bin_with_demand_and_multiple_active():
    r = run_replenishment(il([("A", "R1", "OTHER", 50, 0), ("B", "P1", "PICK", 0, 40), ("B", "P2", "PICK", 0, 40), ("B", "R9", "OTHER", 50, 0)]),
                          inv([("A", "R1", "L1", 50, "OK", "2026-05-09"), ("B", "R9", "L9", 50, "OK", "2026-05-09")]),
                          orders([("A", 10, "SO")]), RR0)
    reasons = set(r.exceptions["Alasan"])
    assert "SKU tidak punya Active BIN (tipe PICK)" in reasons and "Lebih dari satu Active BIN" in reasons
    assert len(r.tasks) == 0


def test_priority_override_and_demand():
    r = run_replenishment(il([("A", "P1", "PICK", 32, 40), ("A", "R1", "OTHER", 300, 0)]),
                          inv([("A", "R1", "L1", 300, "OK", "2026-05-09")]), orders([("A", 100, "TO")]), RR0, DEM)
    row = r.sku_report.iloc[0]
    assert row["Prioritas"] == "Prioritas 1"          # Active 32 < demand 100 walau rasio 80%
    assert row["Kebutuhan Replenish"] == 108 and r.tasks["QTY"].sum() == 108


def test_not_needed_when_bin_full_and_reserve_below_min():
    r = run_replenishment(il([("A", "P1", "PICK", 40, 40), ("B", "P2", "PICK", 0, 40), ("B", "R1", "OTHER", 3, 0)]),
                          inv([("B", "R1", "L1", 3, "OK", "2026-05-09")]), orders(), RR0, Settings(min_reserve=5))
    st = dict(zip(r.sku_report["Item"], r.sku_report["Status"]))
    assert st["A"] == "Tidak perlu replenish" and st["B"].startswith("Dilewati")
    assert len(r.tasks) == 0


def test_rr13_counts_and_rounds_up():
    rr = pd.DataFrame([{"item": "A", "rr": 10.3}])
    r = run_replenishment(il([("A", "P1", "PICK", 20, 40), ("A", "R1", "OTHER", 500, 0)]),
                          inv([("A", "R1", "L1", 500, "OK", "2026-05-09")]), orders(), rr, DEM)
    assert r.tasks["QTY"].sum() == 31      # 40-20+10.3 = 30.3 -> 31


def test_default_matches_ppic_max_minus_active_ignoring_demand():
    r = run_replenishment(il([("A", "P1", "PICK", 19, 40), ("A", "R1", "OTHER", 206, 0)]),
                          inv([("A", "R1", "L1", 206, "OK", "2026-05-09")]), orders([("A", 10, "SO")]), RR0)
    assert r.sku_report.iloc[0]["Kebutuhan Replenish"] == 21 and r.sku_report.iloc[0]["Prioritas"] == "Prioritas 1"
