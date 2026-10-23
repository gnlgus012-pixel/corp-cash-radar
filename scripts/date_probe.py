#!/usr/bin/env python3
"""자금 일자 검증용 1회성 조사: 일자 항목이 안 잡히는 공시 유형의 원문 표 샘플 → reports/date_probe.json"""
from __future__ import annotations
import re, sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

KEYS = ["단기차입금증가결정", "부동산투자회사자금차입", "증권신고서(채무증권)", "단기차입금감소결정"]
out, seen = {}, {}
end = C.now_kst().date()
for ty in ("B", "C", "I"):
    cur = end - timedelta(days=60)
    while cur <= end:
        e = min(cur + timedelta(days=6), end)
        page = 1
        while True:
            j = C.dart_get("list.json", bgn_de=C.ymd(cur), end_de=C.ymd(e), pblntf_ty=ty, last_reprt_at="Y", page_no=page, page_count=100)
            for r in j.get("list") or []:
                b = re.sub(r"\s+", "", r["report_nm"])
                for k in KEYS:
                    if k in b and "자회사" not in b and "종속회사" not in b and seen.get(k, 0) < 3:
                        seen[k] = seen.get(k, 0) + 1
                        txt, err = C.dart_document(r["rcept_no"])
                        rows = [x for x in C.doc_rows(txt) if any(c.strip() for c in x)][:110] if txt else []
                        out.setdefault(k, []).append({"rcept_no": r["rcept_no"], "corp": r["corp_name"], "report_nm": r["report_nm"], "err": err, "rows": rows})
            if page >= int(j.get("total_page") or 1):
                break
            page += 1
        cur = e + timedelta(days=1)
C.save_json(C.ROOT / "reports" / "date_probe.json", out)
print({k: len(v) for k, v in out.items()})
