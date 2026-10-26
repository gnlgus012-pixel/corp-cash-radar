#!/usr/bin/env python3
"""
만기·상환 캘린더 — data.json 에 calendar 항목을 붙인다 (collect.py·holders.py 다음, 암호화 전)

  회사채 만기    : 최근 6년 공모 증권신고서(채무증권) 발행사 → DART 증권신고서 주요정보(bdRs)의 회차별 상환기일·모집총액
  메자닌 만기    : 최근 5년 전환·신주인수권부·교환사채 발행결정 → DART 주요사항 API(cvbd/bdwt/exbd)의 사채만기일·권면총액
  주식담보 만기  : 대주주(개인) 탭의 최대주주 변경 수반 주식담보 계약의 가장 빠른 만기
  대여금 회수    : 금전대여 결정의 대여 종료일 (자금 유입)

한계 (화면에 표시): 발행 당시 기준이라 조기상환·매입소각·전환으로 줄어든 잔액은 반영 못 함. 사모 회사채·CP·전단채는 DART에 없어 제외.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

STORE = C.CACHE_DIR / "calendar.json"
BOND_YEARS = 6
MEZZ_YEARS = 5
MAX_API = int(C.os.environ.get("CAL_MAX_API", "1500"))     # 실행당 발행정보 API 호출 상한 (DART 일일 한도 보호)
MEZZ_API = {"전환사채권발행결정": ("cvbdIsDecsn.json", "CB"), "신주인수권부사채권발행결정": ("bdwtIsDecsn.json", "BW"),
            "교환사채권발행결정": ("exbdIsDecsn.json", "EB")}
STATS = Counter()


def norm_nm(s: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"^\[[^\]]*\]", "", s or ""))


def ymd_dash(s) -> str | None:
    s = str(s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return C.parse_date(s)


def scan(store: dict, key: str, ty: str, years: int, want, today: date) -> None:
    """공시 목록을 한 달씩 훑어 want(보고서명) 에 맞는 행만 저장. 최근은 매번, 과거는 끝날 때까지 이어서."""
    sc = store.setdefault("scan", {}).setdefault(key, {})
    floor = today - timedelta(days=365 * years)
    windows = [(today - timedelta(days=10), today)]
    cur_end = C.parse_ymd(sc["from"]) - timedelta(days=1) if sc.get("from") else today - timedelta(days=11)
    while cur_end >= floor:
        windows.append((max(floor, cur_end - timedelta(days=30)), cur_end))
        cur_end -= timedelta(days=31)
    hits = store.setdefault(key, {})
    for a, b in windows:
        page = 1
        while True:
            j = C.dart_get("list.json", bgn_de=C.ymd(a), end_de=C.ymd(b), pblntf_ty=ty, last_reprt_at="Y",
                           page_no=page, page_count=100)
            STATS[f"list_{key}"] += 1
            for r in j.get("list") or []:
                k = want(norm_nm(r.get("report_nm", "")))
                if k:
                    hits[r["rcept_no"]] = {"corp_code": r["corp_code"], "corp_name": r["corp_name"], "kind": k,
                                           "date": r["rcept_dt"], "corp_cls": r.get("corp_cls", "")}
            if page >= int(j.get("total_page") or 1):
                break
            page += 1
        if a != today - timedelta(days=10) and (not sc.get("from") or C.ymd(a) < sc["from"]):
            sc["from"] = C.ymd(a)
        C.save_json(STORE, store)


def bond_tranches(store: dict) -> None:
    """발행사·연도별 bdRs 조회 → 회차별 상환기일. 지난 해는 한 번만, 올해는 매번(신규 발행 반영)."""
    done = store.setdefault("bdrs", {})
    this_year = C.now_kst().year
    want = sorted({(h["corp_code"], h["date"][:4]) for h in store.get("bonds", {}).values()}, key=lambda x: x[1], reverse=True)
    for cc, y in want:
        key = f"{cc}:{y}"
        if key in done and (int(y) < this_year and done[key].get("ok")):
            continue
        if STATS["api"] >= MAX_API:
            STATS["api_deferred"] += 1
            continue
        j = C.dart_get("bdRs.json", corp_code=cc, bgn_de=f"{y}0101", end_de=f"{y}1231")
        STATS["api"] += 1
        rows = []
        for d in C._walk_dicts(j):
            if d.get("rpd") and d.get("slta") and d.get("tm") is not None:
                rows.append({k: d.get(k) for k in ("rcept_no", "corp_name", "bdnmn", "tm", "slta", "rpd", "pymd", "intr", "isrr")})
        done[key] = {"ok": j.get("status") in ("000", "013"), "rows": rows}


def mezz_terms(store: dict) -> None:
    got = store.setdefault("mezz_terms", {})
    for rn, h in sorted(store.get("mezz", {}).items(), reverse=True):
        if rn in got:
            continue
        if STATS["api"] >= MAX_API:
            STATS["api_deferred"] += 1
            continue
        ep = MEZZ_API[h["kind"]][0]
        j = C.dart_get(ep, corp_code=h["corp_code"], bgn_de=h["date"], end_de=h["date"])
        STATS["api"] += 1
        rows = [d for d in C._walk_dicts(j) if d.get("bd_mtd")]
        pick = [d for d in rows if d.get("rcept_no") == rn] or rows[:1]
        got[rn] = {k: pick[0].get(k) for k in ("bd_mtd", "bd_fta", "bd_tm", "bd_knd", "bdis_mthn", "bd_intr_ex", "bd_intr_sf", "pymd")} if pick else {}


def build(store: dict, data: dict, today: date) -> list[dict]:
    lo, hi = (today - timedelta(days=30)).isoformat(), (today + timedelta(days=366)).isoformat()
    items = []
    # 회사채: (법인, 회차) 별 최신 신고서 기준
    seen = {}
    for key, v in store.get("bdrs", {}).items():
        cc = key.split(":")[0]
        for r in v.get("rows", []):
            k = (cc, str(r.get("tm")))
            if k not in seen or str(r.get("rcept_no")) > str(seen[k].get("rcept_no")):
                seen[k] = {**r, "corp_code": cc}
    cls_of = {h["corp_code"]: h.get("corp_cls", "") for h in store.get("bonds", {}).values()}
    for (cc, tm), r in seen.items():
        d = ymd_dash(r.get("rpd"))
        if not d or not (lo <= d <= hi):
            continue
        intr = r.get("intr") if r.get("intr") not in (None, "-", "") else r.get("isrr")
        items.append({"kind": "회사채 만기", "corp_code": cc, "corp_name": r.get("corp_name") or "", "corp_cls": cls_of.get(cc, ""),
                      "date": d, "amount": C.to_int(r.get("slta")), "rcept_no": r.get("rcept_no"),
                      "label": f"{tm}회 {r.get('bdnmn') or '회사채'}" + (f" · 금리 {intr}%" if intr not in (None, "-", "") else ""),
                      "issued": ymd_dash(r.get("pymd"))})
    # 메자닌: (법인, 종류, 회차) 별 최신 결정 기준
    seen = {}
    for rn, t in store.get("mezz_terms", {}).items():
        h = store.get("mezz", {}).get(rn)
        if not t or not h:
            continue
        k = (h["corp_code"], h["kind"], str(t.get("bd_tm")))
        if k not in seen or rn > seen[k][0]:
            seen[k] = (rn, t, h)
    early = {}   # 법인별 만기 전 사채 취득(풋옵션 등 조기상환) 공시
    for e in data.get("events", []):
        if e.get("category") == "사채 조기취득":
            early.setdefault(e["corp_code"], []).append(e)
    for (cc, kind, tm), (rn, t, h) in seen.items():
        d = ymd_dash(t.get("bd_mtd"))
        if not d or not (lo <= d <= hi):
            continue
        iss = ymd_dash(t.get("pymd")) or ymd_dash(h["date"])
        ers = [e for e in early.get(cc, []) if f"{e['date'][:4]}-{e['date'][4:6]}-{e['date'][6:]}" >= (iss or "")]
        items.append({"kind": "메자닌 만기", "corp_code": cc, "corp_name": h["corp_name"], "corp_cls": h.get("corp_cls", ""),
                      "date": d, "amount": C.to_int(t.get("bd_fta")), "rcept_no": rn,
                      "label": f"{tm}회 {MEZZ_API[kind][1]} · {t.get('bdis_mthn') or ''}".strip(" ·")
                               + (f" · 만기이자 {t['bd_intr_sf']}%" if t.get("bd_intr_sf") not in (None, "-", "") else ""),
                      "issued": ymd_dash(t.get("pymd")),
                      "check": "조기상환 공시 있음" if ers else "잔액 확인",
                      "early_rcept": ers[0]["rcept_no"] if ers else None})
    # 주식담보 만기: (법인, 대주주) 별 최신 공시
    latest = {}
    for i in (data.get("holders") or {}).get("items", []):
        if i.get("cat") == "주식담보" and i.get("maturity"):
            k = (i["corp_code"], i.get("person"))
            if k not in latest or i["date"] > latest[k]["date"]:
                latest[k] = i
    for i in latest.values():
        if lo <= i["maturity"] <= hi:
            items.append({"kind": "주식담보 만기", "corp_code": i["corp_code"], "corp_name": i["corp_name"], "corp_cls": i.get("corp_cls", ""),
                          "date": i["maturity"], "amount": i.get("debt_total") or i.get("amount"), "rcept_no": i["rcept_no"],
                          "label": f"대주주 {i.get('person', '')} · {i.get('how') or ''}".strip(" ·"), "issued": None})
    # 대여금 회수
    for e in data.get("events", []):
        if e.get("category") != "금전 대여":
            continue
        det = e.get("detail") or {}
        end = dict(det.get("dates") or []).get("대여 종료")
        if end and lo <= end <= hi and not det.get("old"):
            to = dict(det.get("texts") or []).get("대여상대", "")
            items.append({"kind": "대여금 회수", "corp_code": e["corp_code"], "corp_name": e["corp_name"], "corp_cls": e.get("corp_cls", ""),
                          "date": end, "amount": det.get("amount"), "rcept_no": e["rcept_no"],
                          "label": f"대여상대 {to}" if to else "금전대여 종료", "issued": None})
    items.sort(key=lambda x: (x["date"], -(x.get("amount") or 0)))
    return items


def main() -> int:
    if not C.DART_KEY:
        return 1
    today = C.now_kst().date()
    store = C.load_json(STORE, {})
    try:
        scan(store, "bonds", "C", BOND_YEARS, lambda n: "bond" if "증권신고서(채무증권)" in n else None, today)
        scan(store, "mezz", "B", MEZZ_YEARS,
             lambda n: next((k for k in MEZZ_API if k in n and "자회사" not in n and "종속회사" not in n), None), today)
        bond_tranches(store)
        mezz_terms(store)
    except (C.OutOfTime, C.DartError) as ex:
        C.warn(f"만기 캘린더 수집 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
    C.save_json(STORE, store)
    out = {"generated_at": C.now_kst().isoformat(timespec="minutes"), "stats": STATS,
           "scan": store.get("scan"), "bond_filings": len(store.get("bonds", {})), "mezz_filings": len(store.get("mezz", {})),
           "mezz_pending": sum(1 for rn in store.get("mezz", {}) if rn not in store.get("mezz_terms", {}))}
    if C.OUT_PATH.exists():
        data = json.loads(C.OUT_PATH.read_text(encoding="utf-8"))
        items = build(store, data, today)
        data["calendar"] = {"items": items, "scan_from": {k: v.get("from") for k, v in (store.get("scan") or {}).items()},
                            "pending": out["mezz_pending"]}
        C.OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        out.update({"items": len(items), "by_kind": Counter(i["kind"] for i in items),
                    "amount_by_kind_eok": {k: round(sum(i.get("amount") or 0 for i in items if i["kind"] == k) / 1e8)
                                           for k in {i["kind"] for i in items}},
                    "top": [{k: i.get(k) for k in ("kind", "corp_name", "date", "amount", "label")}
                            for i in sorted(items, key=lambda x: -(x.get("amount") or 0))[:12] if i["kind"] != "주식담보 만기"]})
    C.save_json(C.ROOT / "reports" / "calendar_status.json", out)   # 공개 저장소용: 건수·회사명만 (개인 이름 없음)
    C.log(f"만기 캘린더 {out.get('items')}건 / API {STATS['api']}회 (보류 {STATS['api_deferred']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
