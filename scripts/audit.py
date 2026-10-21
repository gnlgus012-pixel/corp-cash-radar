#!/usr/bin/env python3
"""
누락 공시 점검 (수동 실행용)

대시보드 수집과 별개로, 같은 기간의 DART 공시 목록을 '필터 없이' 다시 받아서
1) 수집 규칙(event_rules)에 맞는데 대시보드(data.json)에 빠진 공시
2) 규칙에 안 걸렸지만 자금 관련 단어가 들어간 보고서명 (규칙 추가 후보)
3) 제외 규칙(자회사·철회 등)으로 빠진 공시
4) 유형별로 실제 매칭된 보고서명 목록 (오분류 확인용)
을 docs/data/audit.json 으로 남긴다.

대시보드 수집과 다른 점
- 거래소공시(I)를 수시공시(I001)로 좁히지 않고 전체 조회
- 기타공시(E)도 조회 (자금 관련 공시가 섞여 있는지 확인)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

OUT = C.ROOT / "docs" / "data" / "audit.json"
DATA = C.ROOT / "docs" / "data" / "data.json"

MONEY_WORDS = ["증자", "사채", "차입", "어음", "전자단기", "양수", "양도", "취득", "처분", "매각", "인수", "투자",
               "배당", "합병", "분할", "교환", "이전", "자기주식", "자금", "출자", "대여", "담보", "채무", "유동화",
               "신탁", "소각", "감자", "공개매수"]


def base_name(nm: str) -> str:
    """[기재정정] 같은 머리말·(자율공시) 같은 꼬리말을 떼고 비교용 이름으로"""
    nm = re.sub(r"^\[[^\]]*\]", "", nm.strip())
    nm = re.sub(r"\((자율공시|종속회사의주요경영사항|자회사의 주요경영사항|공정공시)\)$", "", nm)
    return nm.strip()


def fetch_all(bgn, end, ty, last_only=True):
    rows, page = [], 1
    while True:
        j = C.dart_get("list.json", bgn_de=C.ymd(bgn), end_de=C.ymd(end), pblntf_ty=ty,
                       last_reprt_at="Y" if last_only else "N", page_no=page, page_count=100)
        rows.extend(j.get("list") or [])
        if page >= int(j.get("total_page") or 1):
            return rows
        page += 1


def main() -> int:
    if not C.DART_KEY:
        print("DART_API_KEY 없음", file=sys.stderr)
        return 1
    started = time.monotonic()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    have = {e["rcept_no"]: e for e in data["events"]}
    dates = sorted(e["date"] for e in data["events"])
    a, b = C.parse_ymd(dates[0]), C.parse_ymd(dates[-1])  # 대시보드가 가진 기간과 정확히 같은 기간
    types = os.environ.get("AUDIT_TYPES", "B,C,I,E").split(",")
    C.log(f"점검 기간 {a} ~ {b} / 유형 {types} / 대시보드 이벤트 {len(have):,}건")

    all_rows: dict[str, dict] = {}
    pages = Counter()
    cur = a
    done_until = None
    try:
        while cur <= b:
            e = min(cur + timedelta(days=6), b)
            for ty in types:
                rows = fetch_all(cur, e, ty)
                pages[ty] += max(1, (len(rows) + 99) // 100)
                for r in rows:
                    r["_ty"] = ty
                    all_rows[r["rcept_no"]] = r
            done_until = e
            C.log(f"  {cur}~{e} 누적 {len(all_rows):,}건")
            cur = e + timedelta(days=1)
    except (C.OutOfTime, C.DartError) as ex:
        C.warn(f"점검 중단 ({ex.__class__.__name__}: {ex}) — {done_until}까지만 비교")
    if done_until is None:
        return 1
    b = done_until
    have = {rn: e for rn, e in have.items() if e["date"] <= C.ymd(b)}

    matched, missing, excluded = [], [], []
    unmatched = Counter()
    unmatched_ex: dict[str, dict] = {}
    by_cat: dict[str, Counter] = defaultdict(Counter)
    for r in all_rows.values():
        nm = r.get("report_nm", "")
        rule = C.classify(nm)
        if rule:
            matched.append(r)
            by_cat[rule["category"]][base_name(nm)] += 1
            if r["rcept_no"] not in have:
                missing.append({"rcept_no": r["rcept_no"], "date": r["rcept_dt"], "corp": r["corp_name"],
                                "corp_cls": r.get("corp_cls"), "type": r["_ty"], "report_nm": nm,
                                "category": rule["category"]})
            continue
        n = C.norm(nm)
        if any(C.norm(k) in n for k in C.CFG["dart"].get("exclude_keywords", [])) and \
                any(C.norm(rr["keyword"]) in n for rr in C.CFG["event_rules"]):
            excluded.append({"date": r["rcept_dt"], "corp": r["corp_name"], "report_nm": nm})
            continue
        if any(w in nm for w in MONEY_WORDS):
            k = f"{r['_ty']} | {base_name(nm)}"
            unmatched[k] += 1
            unmatched_ex.setdefault(k, {"rcept_no": r["rcept_no"], "corp": r["corp_name"], "date": r["rcept_dt"]})

    # 대시보드에는 있는데 이번 전체 조회에 없는 것 (정정으로 대체된 경우 등)
    extra = [e for rn, e in have.items() if rn not in all_rows]

    out = {
        "generated_at": C.now_kst().isoformat(timespec="minutes"),
        "elapsed_sec": int(time.monotonic() - started),
        "period": [str(a), str(b)],
        "complete": str(b) == dates[-1][:4] + "-" + dates[-1][4:6] + "-" + dates[-1][6:],
        "types": types,
        "pages": pages,
        "total_rows": len(all_rows),
        "dashboard_events": len(have),
        "matched": len(matched),
        "missing": sorted(missing, key=lambda x: x["date"], reverse=True),
        "missing_by_type": Counter(m["type"] for m in missing),
        "missing_by_category": Counter(m["category"] for m in missing),
        "dashboard_only": [{"rcept_no": e["rcept_no"], "date": e["date"], "corp": e["corp_name"],
                            "report_nm": e["report_nm"]} for e in extra][:200],
        "excluded_total": len(excluded),
        "excluded": Counter(re.sub(r"\s+", " ", x["report_nm"]) for x in excluded).most_common(60),
        "unmatched_candidates": [{"name": k, "count": v, "example": unmatched_ex[k]}
                                 for k, v in unmatched.most_common(150)],
        "matched_names_by_category": {k: v.most_common() for k, v in sorted(by_cat.items())},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    C.log(f"점검 완료: 전체 {len(all_rows):,}건 / 규칙 해당 {len(matched):,}건 / 대시보드 누락 {len(missing):,}건 / "
          f"규칙 밖 후보 {len(unmatched):,}종 / {out['elapsed_sec']}초")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=누락 점검::기간 {a}~{b} 규칙 해당 {len(matched):,}건 중 누락 {len(missing):,}건, "
              f"규칙 밖 후보 {len(unmatched):,}종")
    return 0


if __name__ == "__main__":
    sys.exit(main())
