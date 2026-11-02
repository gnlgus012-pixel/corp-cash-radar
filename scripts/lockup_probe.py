#!/usr/bin/env python3
"""보호예수(의무보유) 표 구조 조사 (1회성) → reports/lockup_probe.json"""
from __future__ import annotations
import re, sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
C.CFG["details"]["max_doc_mb"] = 60
end = C.now_kst().date(); out = {"ipo": [], "rights": []}
def lst(ty, a, b):
    rows, page = [], 1
    while True:
        j = C.dart_get("list.json", bgn_de=C.ymd(a), end_de=C.ymd(b), pblntf_ty=ty, last_reprt_at="Y", page_no=page, page_count=100)
        rows += j.get("list") or []
        if page >= int(j.get("total_page") or 1): return rows
        page += 1
KW = re.compile(r"(의무보유|보호예수|매각제한|계속보유)\s*(기간|기한)|유통제한|유통\s*가능\s*주식|상장\s*후\s*유통")
def grab(rn):
    txt, err = C.dart_document(rn)
    if not txt: return {"err": err}
    rows = [r for r in C.doc_rows(txt) if any(c.strip() for c in r)]
    hits = [i for i, r in enumerate(rows) if KW.search(" ".join(r))]
    blocks = []
    seen=-99
    for i in hits:
        if i - seen < 20: continue
        seen=i; blocks.append(rows[max(0, i - 2): i + 26])
        if len(blocks) >= 8: break
    return {"n_rows": len(rows), "n_hits": len(hits), "blocks": blocks}
cur = end - timedelta(days=90); eq = []; rg = []
while cur <= end:
    b = min(cur + timedelta(days=30), end)
    for r in lst("C", cur, b):
        if "증권신고서(지분증권)" in re.sub(r"\s", "", r["report_nm"]): eq.append(r)
    for r in lst("B", cur, b):
        if "유상증자결정" in re.sub(r"\s", "", r["report_nm"]) and "자회사" not in r["report_nm"]: rg.append(r)
    cur = b + timedelta(days=1)
for r in [x for x in eq if x.get("corp_cls") not in ("Y", "K")][:4] + eq[:1]:
    out["ipo"].append({"corp": r["corp_name"], "cls": r.get("corp_cls"), "rcept_no": r["rcept_no"], "report_nm": r["report_nm"], **grab(r["rcept_no"])})
for r in []:
    out["rights"].append({"corp": r["corp_name"], "rcept_no": r["rcept_no"], "report_nm": r["report_nm"], **grab(r["rcept_no"])})
C.save_json(C.ROOT / "reports" / "lockup_probe.json", out)
print(len(eq), len(rg))
