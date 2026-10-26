#!/usr/bin/env python3
"""만기 캘린더·IPO 알림 설계용 1회성 조사 → reports/plan_probe.json"""
from __future__ import annotations
import re, sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

out = {}
end = C.now_kst().date()
bgn = end - timedelta(days=88)

def lst(ty, detail=None, b=bgn, e=end):
    rows, page = [], 1
    while True:
        p = dict(bgn_de=C.ymd(b), end_de=C.ymd(e), pblntf_ty=ty, page_no=page, page_count=100, last_reprt_at="Y")
        if detail:
            p["pblntf_detail_ty"] = detail
        j = C.dart_get("list.json", **p)
        rows += j.get("list") or []
        if page >= int(j.get("total_page") or 1):
            return rows, j.get("status"), j.get("message")
        page += 1

for d in ("C001", "C002"):
    r, st, msg = lst("C", d)
    out[f"list_{d}"] = {"status": st, "msg": msg, "n": len(r), "names": Counter(re.sub(r"^\[[^\]]*\]", "", x["report_nm"]).strip() for x in r).most_common(15),
                        "cls": Counter(x.get("corp_cls") for x in r), "sample": r[:5]}
    out[f"_rows_{d}"] = r

def keys_rows(j, n=3):
    rows = []
    def walk(o):
        if isinstance(o, dict):
            if "rcept_no" in o:
                rows.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(j)
    return {"status": j.get("status"), "groups": [g.get("title") for g in j.get("group", []) if isinstance(g, dict)],
            "n_rows": len(rows), "rows": rows[:n]}

# 회사채: 3년 전 발행분 per corp-year
bonds = out.pop("_rows_C002")
cc = [x["corp_code"] for x in bonds][:2]
for c in cc:
    for y in (2023, 2024):
        out.setdefault("bdRs_year", []).append({"corp": c, "year": y, **keys_rows(C.dart_get("bdRs.json", corp_code=c, bgn_de=f"{y}0101", end_de=f"{y}1231"), 6)})

# IPO 후보: 지분증권 신고서
eq = out.pop("_rows_C001")
for x in eq[:4]:
    j = C.dart_get("estkRs.json", corp_code=x["corp_code"], bgn_de=C.ymd(bgn), end_de=C.ymd(end))
    out.setdefault("estkRs", []).append({"corp": x["corp_name"], "cls": x.get("corp_cls"), "report_nm": x["report_nm"], **keys_rows(j, 4)})

# 메자닌: 주요사항 API (전환사채권 발행결정) — 2년 전
r, st, msg = lst("B", "B001", end - timedelta(days=800), end - timedelta(days=715))
cb = [x for x in r if "전환사채권발행결정" in re.sub(r"\s", "", x["report_nm"])][:3]
out["b001_n"] = {"status": st, "msg": msg, "n": len(r), "cb": len(cb)}
for x in cb:
    j = C.dart_get("cvbdIsDecsn.json", corp_code=x["corp_code"], bgn_de=x["rcept_dt"], end_de=x["rcept_dt"])
    out.setdefault("cvbd", []).append({"corp": x["corp_name"], **keys_rows(j, 2)})
C.save_json(C.ROOT / "reports" / "plan_probe.json", out)
print("ok")
