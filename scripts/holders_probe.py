#!/usr/bin/env python3
"""
대주주(개인) 자금 공시 — 사전 조사용 (1회성)
최근 30일 지분공시(D)·거래소공시(I 전체)의 보고서명 분포와, 후보 보고서의 원문 표 샘플,
대주주 관련 DART API(majorstock / elestock / hyslrSttus) 응답 샘플을 reports/holders_probe.json 으로 남긴다.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

OUT = C.ROOT / "reports" / "holders_probe.json"
CAND = re.compile(r"최대주주|대량보유|주요주주|담보|증여|상속|양수도|공개매수|특수관계인|소유주식")
PERSON = re.compile(r"^[가-힣]{2,4}$")


def fetch_all(bgn, end, ty):
    rows, page = [], 1
    while True:
        j = C.dart_get("list.json", bgn_de=C.ymd(bgn), end_de=C.ymd(end), pblntf_ty=ty,
                       last_reprt_at="Y", page_no=page, page_count=100)
        rows.extend(j.get("list") or [])
        if page >= int(j.get("total_page") or 1):
            return rows
        page += 1


def base(nm):
    nm = re.sub(r"^\[[^\]]*\]", "", nm.strip())
    return re.sub(r"\s+", "", nm)


def main():
    end = C.now_kst().date()
    bgn = end - timedelta(days=30)
    out = {"period": [str(bgn), str(end)], "names": {}, "samples": {}, "api": {}}
    by_ty = {}
    for ty in ("D", "I"):
        rows = []
        cur = bgn
        while cur <= end:
            e = min(cur + timedelta(days=6), end)
            rows += fetch_all(cur, e, ty)
            cur = e + timedelta(days=1)
        by_ty[ty] = rows
        cnt = Counter(base(r["report_nm"]) for r in rows)
        person = Counter(base(r["report_nm"]) for r in rows if PERSON.match(r.get("flr_nm", "")))
        out["names"][ty] = [{"name": k, "count": v, "filer_person": person.get(k, 0)} for k, v in cnt.most_common(120)]
        C.log(f"{ty}: {len(rows):,}건, 보고서명 {len(cnt)}종")

    # 후보 보고서별 원문 샘플 3건
    seen = Counter()
    for ty, rows in by_ty.items():
        for r in rows:
            k = base(r["report_nm"])
            if not CAND.search(k) or seen[k] >= 3:
                continue
            seen[k] += 1
            txt, err = C.dart_document(r["rcept_no"])
            s = {"rcept_no": r["rcept_no"], "corp_name": r["corp_name"], "corp_cls": r.get("corp_cls"),
                 "flr_nm": r.get("flr_nm"), "date": r["rcept_dt"], "err": err}
            if txt:
                s["rows"] = [row for row in C.doc_rows(txt) if any(c.strip() for c in row)][:160]
            out["samples"].setdefault(f"{ty} | {k}", []).append(s)
    C.log(f"원문 샘플 {sum(seen.values())}건")

    # 대주주 관련 API 샘플: 최근 대량보유 보고가 있는 상장사 3곳
    corps = []
    for r in by_ty["D"]:
        if "대량보유" in r["report_nm"] and r.get("corp_cls") in ("Y", "K") and r["corp_code"] not in corps:
            corps.append(r["corp_code"])
        if len(corps) >= 3:
            break
    for cc in corps:
        for ep, extra in (("majorstock.json", {}), ("elestock.json", {}),
                          ("hyslrSttus.json", {"bsns_year": str(end.year - 1), "reprt_code": "11011"})):
            j = C.dart_get(ep, corp_code=cc, **extra)
            lst = j.get("list") or []
            out["api"].setdefault(ep, []).append({"corp_code": cc, "status": j.get("status"), "total": len(lst),
                                                  "rows": lst[-8:] if ep != "hyslrSttus.json" else lst[:15]})
    C.save_json(OUT, out)
    C.log("저장 완료")


if __name__ == "__main__":
    main()
