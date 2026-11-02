#!/usr/bin/env python3
"""
보호예수(의무보유) 해제 일정 — 사람별 (collect.py·holders.py 다음, calendar_build.py 전)

  IPO        : 증권신고서(지분증권, 신규상장) 원문의 '상장 후 유통제한/매각제한' · '최대주주등 의무보유' 표
               → 주주명 · 관계 · 매각제한 주식수 · 기간(상장일 기준) → 해제 예정일 = 상장일 + 기간
               상장일: 상장 후엔 첫 거래일(시세), 상장 전엔 납입일 + 4일(추정)
  제3자배정  : 유상증자결정 원문의 '제3자배정 대상자별 … 배정내역' 표 (비고·면제사유의 보호예수/전매제한)
               → 대상자 · 관계 · 배정주식수 · 기간(기본 1년) → 해제 예정일 = 신주 상장예정일(없으면 납입일) + 기간

개인만 (holders.is_person). 평가액 = 주식수 × 최근 종가 (상장 전이면 공모가·발행가).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
import holders as H  # noqa: E402

STORE_PATH = C.CACHE_DIR / "lockup.json"
CHECK_OUT = C.ROOT / "lockup_check.json"      # 검증용 (개인 이름 포함 → 저장소에 올리지 않고 Actions 산출물로만)
PARSE_VER = "l1"
BUILD_RESERVE = int(os.environ.get("LOCKUP_BUILD_RESERVE_SEC", "240"))
IPO_YEARS = 3.3
RIGHTS_MONTHS = 26
MAX_DOCS = int(C.os.environ.get("LOCKUP_MAX_DOCS", "150"))
CHECK = "--check" in sys.argv
STATS = Counter()

PERIOD_RX = re.compile(r"^(?:상장(?:일)?\s*(?:로부터|후)\s*)?(?:(\d+)\s*년)?\s*(?:(\d+)\s*개월)?\s*(?:(\d+)\s*일)?$")
INT_RX = re.compile(r"^\d{1,3}(,\d{3})+$|^\d+$")
GROUP_RX = re.compile(r"\s*외\s*\d+\s*(인|명)$")
NOT_PERSON_REL = re.compile(r"벤처|전문투자|기관|조합|펀드|투자자|상장주선|주관|증권|은행|법인|회사$")
LOCK_WORD = re.compile(r"보호예수|전매제한|의무보유|매각제한|계속보유")


def nz(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def period_of(cell: str):
    """'3년' '30개월' '2년 6개월' '상장일로부터 1년' → (개월 수, 원문)"""
    t = (cell or "").strip()
    if not t or len(t) > 16:
        return None
    m = PERIOD_RX.match(t)
    if not m or not any(m.groups()):
        return None
    y, mo, d = (int(x) if x else 0 for x in m.groups())
    if d and not (y or mo):
        months = round(d / 30)
        if months == 0:
            return None
    else:
        months = y * 12 + mo
    if not 1 <= months <= 120:
        return None
    return months, t


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def person_ok(name: str, rel: str) -> bool:
    core = GROUP_RX.sub("", name or "").strip()
    return H.is_person(core) and not NOT_PERSON_REL.search(nz(rel))


def to_int(s: str):
    s = (s or "").strip()
    return int(s.replace(",", "")) if INT_RX.match(s) else None


# ───────────────────────── 표 해석 ─────────────────────────
def parse_ipo(rows: list[list[str]]) -> dict:
    out = {"ipo": False, "pay_date": None, "price": None, "people": []}
    flat = [nz(" ".join(r)) for r in rows]
    hi = next((i for i, f in enumerate(flat[:300]) if "상장을위한공모여부" in f), None)
    if hi is not None and hi + 1 < len(rows):
        out["ipo"] = "신규상장" in flat[hi + 1]
    else:
        out["ipo"] = any("상장예비심사" in f for f in flat[:400]) and any("신규상장" in f for f in flat[:300])
    # 납입기일: '청약기일 | 납입기일 | …' 다음 행 같은 칸
    for i, r in enumerate(rows[:400]):
        cells = [nz(c) for c in r]
        if "납입기일" in cells and i + 1 < len(rows):
            j = cells.index("납입기일")
            nxt = rows[i + 1]
            if j < len(nxt):
                out["pay_date"] = C.parse_date(nxt[j])
            if out["pay_date"]:
                break
    for i, r in enumerate(rows[:200]):
        cells = [nz(c) for c in r]
        if "모집(매출)가액" in cells and i + 1 < len(rows):
            j = cells.index("모집(매출)가액")
            if j < len(rows[i + 1]):
                out["price"] = to_int(rows[i + 1][j])
            if out["price"]:
                break
    seen = set()
    i = 0
    while i < len(rows):
        h = flat[i]
        if not (re.search(r"매각제한|의무보유|보호예수|확약기간", h) and re.search(r"기간", h)
                and re.search(r"주주명|성명|주주", h)):
            i += 1
            continue
        header = " ".join(flat[i:i + 3])
        restricted_col = "매각제한물량" in header or "매각제한주식" in header or "확약주식수" in header
        prev = None
        j = i + 1
        while j < len(rows) and j < i + 120:
            r = rows[j]
            f = flat[j]
            if (f.startswith(("합계", "합 계", "총계")) or re.match(r"^\(?주\d", f) or f.startswith(("[", "【", "※"))
                    or re.match(r"^[가-하]\.", f)):
                break
            p_idx = None
            for k in range(len(r) - 1, -1, -1):
                if period_of(r[k]):
                    p_idx = k
                    break
            if p_idx is None:
                j += 1
                continue
            months, ptxt = period_of(r[p_idx])
            before = r[:p_idx]
            name_idx = next((k for k, c in enumerate(before) if to_int(c) is None and "%" not in c
                             and person_ok(c, "")), None)
            ints = [to_int(c) for c in before]
            ints = [v for v in ints if v]
            if name_idx is None:
                if prev and before and to_int(before[0]):   # 한 사람의 기간별 나눔 (이름 칸 병합)
                    name, rel = prev
                else:
                    prev = None
                    j += 1
                    continue
            else:
                name = before[name_idx].strip()
                rel = next((c.strip() for c in before[name_idx + 1:] if c.strip() and to_int(c) is None
                            and "%" not in c and c.strip() != "-" and not re.fullmatch(r"[\d.,]+|보통주|우선주|종류주|기명식.*", c.strip())), "")
                if not person_ok(name, rel):
                    prev = None
                    j += 1
                    continue
                prev = (name, rel)
            if not ints:
                j += 1
                continue
            if name_idx is None:
                shares = ints[0]
            elif restricted_col and len(ints) >= 3:
                shares = ints[2]
            else:
                shares = ints[-1]
            key = (name, months, shares)
            if key not in seen:
                seen.add(key)
                out["people"].append({"name": name, "rel": rel, "shares": shares, "months": months, "period": ptxt})
            j += 1
        i = j
    # 같은 사람·기간이 여러 표에 나오면 하나만 (주식 수 큰 쪽)
    best = {}
    for p in out["people"]:
        if re.search(r"^(타인|기타|주주|개인|소액|일반)", nz(p["rel"])) and p["months"] < 6:
            continue   # 대주주와 무관한 개인 소액 주주의 단기 확약
        k = (p["name"], p["months"])
        if k not in best or p["shares"] > best[k]["shares"]:
            best[k] = p
    singles = {(p["name"], p["months"]) for p in best.values() if not GROUP_RX.search(p["name"])}
    out["people"] = [p for p in best.values()   # '홍길동 외 7인' 묶음이 개인별 행과 겹치면 묶음은 뺀다
                     if not (GROUP_RX.search(p["name"]) and (GROUP_RX.sub("", p["name"]).strip(), p["months"]) in singles)]
    return out


def parse_rights(rows: list[list[str]]) -> dict:
    out = {"third": False, "pay_date": None, "list_date": None, "price": None, "lock_months": None, "people": []}
    flat = [nz(" ".join(r)) for r in rows]
    doc_lock = None
    for r, f in zip(rows, flat):
        c0 = nz(r[0]) if r else ""
        if out["pay_date"] is None and re.search(r"납입일$", c0):
            out["pay_date"] = next((C.parse_date(c) for c in r[1:] if C.parse_date(c)), None)
        if out["list_date"] is None and re.search(r"상장예정일", c0):
            out["list_date"] = next((C.parse_date(c) for c in r[1:] if C.parse_date(c)), None)
        if out["price"] is None and re.search(r"신주발행가액", c0):
            out["price"] = next((to_int(c) for c in r[1:] if to_int(c)), None)
        if "증자방식" in c0 and "제3자" in f:
            out["third"] = True
        if re.search(r"제출을면제받은경우", c0) and LOCK_WORD.search(f):
            m = re.search(r"(\d+)\s*년", " ".join(r[1:]))
            doc_lock = int(m.group(1)) * 12 if m else 12
    for i, f in enumerate(flat):
        if not ("제3자배정대상자" in f and "배정주식수" in f):
            continue
        hdr = [nz(c) for c in rows[i]]
        ci = lambda rx: next((k for k, c in enumerate(hdr) if re.search(rx, c)), None)  # noqa: E731
        c_name, c_rel, c_sh, c_note = ci(r"대상자$|대상자명|^성명"), ci(r"관계"), ci(r"배정주식수"), ci(r"비고")
        if c_name is None or c_sh is None:
            continue
        out["third"] = True
        for r in rows[i + 1:i + 80]:
            f = nz(" ".join(r))
            if not r or f.startswith(("합계", "총계", "【", "[", "※")) or re.match(r"^\(?주\d", f):
                break
            name = r[c_name].strip() if c_name < len(r) else ""
            rel = r[c_rel].strip() if c_rel is not None and c_rel < len(r) else ""
            sh = to_int(r[c_sh]) if c_sh < len(r) else None
            note = r[c_note] if c_note is not None and c_note < len(r) else ""
            if not sh or not person_ok(name, rel):
                continue
            months = None
            if LOCK_WORD.search(nz(note)):
                m = re.search(r"(\d+)\s*년", note)
                mm = re.search(r"(\d+)\s*개월", note)
                months = int(m.group(1)) * 12 if m else (int(mm.group(1)) if mm else 12)
            months = months or doc_lock
            if not months:
                continue
            out["people"].append({"name": name, "rel": rel, "shares": sh, "months": months,
                                  "period": f"{months // 12}년" if months % 12 == 0 else f"{months}개월"})
        break
    out["lock_months"] = doc_lock
    return out


# ───────────────────────── 수집 ─────────────────────────
def scan(store: dict, key: str, ty: str, days: int, want, today: date) -> None:
    sc = store.setdefault("scan", {}).setdefault(key, {})
    floor = today - timedelta(days=days)
    windows = [(today - timedelta(days=10), today)]
    cur_end = C.parse_ymd(sc["from"]) - timedelta(days=1) if sc.get("from") else today - timedelta(days=11)
    while cur_end >= floor:
        windows.append((max(floor, cur_end - timedelta(days=30)), cur_end))
        cur_end -= timedelta(days=31)
    docs = store.setdefault("docs", {})
    for a, b in windows:
        page = 1
        while True:
            j = C.dart_get("list.json", bgn_de=C.ymd(a), end_de=C.ymd(b), pblntf_ty=ty, last_reprt_at="Y",
                           page_no=page, page_count=100)
            STATS[f"list_{key}"] += 1
            for r in j.get("list") or []:
                if want(nz(re.sub(r"^\[[^\]]*\]", "", r.get("report_nm", "")))) and r["rcept_no"] not in docs:
                    docs[r["rcept_no"]] = {"kind": key, "corp_code": r["corp_code"], "corp_name": r["corp_name"],
                                           "corp_cls": r.get("corp_cls", ""), "stock_code": r.get("stock_code", ""),
                                           "date": r["rcept_dt"], "report_nm": r["report_nm"]}
            if page >= int(j.get("total_page") or 1):
                break
            page += 1
        if a != today - timedelta(days=10) and (not sc.get("from") or C.ymd(a) < sc["from"]):
            sc["from"] = C.ymd(a)
        C.save_json(STORE_PATH, store)


def latest_ipo_per_corp(docs: dict) -> set:
    """한 회사의 IPO 신고서는 정정·발행조건확정이 여러 개 → 회사별 최신 것만 읽는다 (120일 안 묶음)"""
    keep, by = set(), {}
    for rn, d in docs.items():
        if d["kind"] == "ipo":
            by.setdefault(d["corp_code"], []).append(rn)
    for rns in by.values():
        rns.sort(reverse=True)
        last = None
        for rn in rns:
            if last is None or (C.parse_ymd(docs[last]["date"]) - C.parse_ymd(docs[rn]["date"])).days > 120:
                keep.add(rn)
                last = rn
    return keep


def parse_pending(store: dict) -> None:
    docs = store["docs"]
    ipo_keep = latest_ipo_per_corp(docs)
    todo = [rn for rn, d in docs.items() if d.get("pv") != PARSE_VER and (d["kind"] != "ipo" or rn in ipo_keep)
            and d.get("tries", 0) < 4]
    todo.sort(reverse=True)
    n = 0
    for rn in todo:
        if n >= MAX_DOCS:
            STATS["deferred"] += 1
            continue
        d = docs[rn]
        txt, err = C.dart_document(rn)
        n += 1
        if not txt:
            d["err"] = err
            d["tries"] = d.get("tries", 0) + 1
            STATS["doc_err"] += 1
            continue
        rows = [r for r in C.doc_rows(txt) if any(c.strip() for c in r)]
        d["parsed"] = parse_ipo(rows) if d["kind"] == "ipo" else parse_rights(rows)
        d["pv"] = PARSE_VER
        d.pop("err", None)
        STATS[f"parsed_{d['kind']}"] += 1
        if n % 20 == 0:
            C.save_json(STORE_PATH, store)


def _d(s: str) -> date:
    """YYYY-MM-DD / YYYYMMDD 둘 다 받음"""
    s = s.replace("-", "")
    return C.parse_ymd(s)


def first_trade(prices: dict, code: str, after: str, today: date):
    """상장 후 첫 거래일 (네이버 일별 시세의 첫날, after 이후)"""
    if not code:
        return None
    H.close_on(prices, code, after, today)
    px = prices.get(code, {}).get("px", {})
    a = after.replace("-", "")
    pre = C.ymd(_d(after) - timedelta(days=10))
    if any(pre <= k < a for k in px):
        return None    # 납입 전에도 거래됨 → 이미 상장된 회사
    cands = sorted(k for k in px if k >= a)
    return f"{cands[0][:4]}-{cands[0][4:6]}-{cands[0][6:]}" if cands else None


def build(store: dict, prices: dict, today: date, corp_map: dict) -> list[dict]:
    items = []
    lo = today - timedelta(days=30)
    ipo_keep = latest_ipo_per_corp(store["docs"])
    for rn, d in store["docs"].items():
        if d["kind"] == "ipo" and rn not in ipo_keep:
            continue
        p = d.get("parsed")
        if not p or not p.get("people"):
            continue
        C.check_time()
        code = d.get("stock_code") or corp_map.get(d["corp_code"], {}).get("stock_code", "")
        if d["kind"] == "ipo":
            if not p.get("ipo") or not p.get("pay_date"):
                continue
            lst = first_trade(prices, code, p["pay_date"], today) if code else None
            if lst and (_d(lst) - _d(p["pay_date"])).days > 40:
                lst = None
            if not lst and _d(p["pay_date"]) < today - timedelta(days=60):
                STATS["ipo_not_listed"] += 1   # 철회·상장 무산 추정
                continue
            base, base_kind = (lst, "상장일") if lst else (str(_d(p["pay_date"]) + timedelta(days=4)), "상장일(납입일+4일 추정)")
            src = "IPO"
        else:
            if not p.get("third"):
                continue
            base = p.get("list_date") or p.get("pay_date")
            if not base:
                STATS["rights_no_date"] += 1
                continue
            base_kind = "신주 상장예정일" if p.get("list_date") else "납입일"
            src = "제3자배정 증자"
        for pp in p["people"]:
            rel_d = add_months(_d(base), pp["months"])
            if rel_d < lo or rel_d > today + timedelta(days=365 * 3 + 40):
                continue
            px = H.close_on(prices, code, str(today), today) if code else None
            px_kind = "최근 종가"
            if not px:
                px, px_kind = p.get("price"), ("공모가" if src == "IPO" else "발행가")
            items.append({"corp_code": d["corp_code"], "corp_name": d["corp_name"], "corp_cls": d.get("corp_cls", ""),
                          "stock_code": code, "person": pp["name"], "relation": pp["rel"], "shares": pp["shares"],
                          "period": pp["period"], "months": pp["months"], "base": base, "base_kind": base_kind,
                          "date": str(rel_d), "amount": round(pp["shares"] * px) if px else None, "px": px,
                          "px_kind": px_kind, "src": src, "rcept_no": rn, "filed": d["date"]})
    items.sort(key=lambda x: (x["date"], -(x.get("amount") or 0)))
    return items


def main() -> int:
    if not C.DART_KEY:
        return 1
    C.CFG["details"]["max_doc_mb"] = 60
    today = C.now_kst().date()
    store = C.load_json(STORE_PATH, {"docs": {}})
    store.setdefault("docs", {})
    try:
        scan(store, "ipo", "C", int(365 * IPO_YEARS), lambda n: "증권신고서(지분증권)" in n, today)
        scan(store, "rights", "B", RIGHTS_MONTHS * 31,
             lambda n: "유상증자결정" in n and "자회사" not in n and "종속회사" not in n and "철회" not in n, today)
    except (C.OutOfTime, C.DartError) as ex:
        C.warn(f"보호예수 목록 수집 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
    full = C.TIME_BUDGET_SEC
    if full:
        C.TIME_BUDGET_SEC = max(60, full - BUILD_RESERVE)   # 시세 조회·정리할 시간은 남겨 둠
    try:
        parse_pending(store)
    except (C.OutOfTime, C.DartError) as ex:
        C.warn(f"보호예수 원문 읽기 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
    C.TIME_BUDGET_SEC = full
    C.save_json(STORE_PATH, store)
    prices = H.load_prices()
    H.corp_names()
    corp_map = C.load_json(C.CACHE_DIR / "corpcode.json", {}).get("map", {})
    try:
        items = build(store, prices, today, corp_map)
    except C.OutOfTime:
        items = []
        C.warn("시간 부족으로 보호예수 항목 생략")
    C.save_json(H.PRICE_PATH, prices)
    docs = store["docs"]
    keep = latest_ipo_per_corp(docs)
    pend = sum(1 for rn, d in docs.items() if d.get("pv") != PARSE_VER and (d["kind"] != "ipo" or rn in keep)
               and d.get("tries", 0) < 4)
    status = {"generated_at": C.now_kst().isoformat(timespec="minutes"), "items": len(items),
              "by_src": Counter(i["src"] for i in items), "docs": Counter(d["kind"] for d in docs.values()),
              "ipo_docs_read": sum(1 for d in docs.values() if d["kind"] == "ipo" and d.get("parsed")),
              "ipo_with_people": sum(1 for d in docs.values() if d["kind"] == "ipo" and (d.get("parsed") or {}).get("ipo")
                                     and d["parsed"].get("people")),
              "ipo_flag": sum(1 for d in docs.values() if d["kind"] == "ipo" and (d.get("parsed") or {}).get("ipo")),
              "rights_third": sum(1 for d in docs.values() if d["kind"] == "rights" and (d.get("parsed") or {}).get("third")),
              "rights_with_people": sum(1 for d in docs.values() if d["kind"] == "rights" and (d.get("parsed") or {}).get("people")),
              "pending": pend, "scan_from": {k: v.get("from") for k, v in store.get("scan", {}).items()},
              "no_amount": sum(1 for i in items if not i.get("amount")),
              "next_90d": sum(1 for i in items if i["date"] <= str(today + timedelta(days=90))),
              "corps_top": [{"corp": i["corp_name"], "date": i["date"], "src": i["src"], "amount_eok": round((i.get("amount") or 0) / 1e8)}
                            for i in sorted(items, key=lambda x: -(x.get("amount") or 0))[:10]],
              "stats": STATS}
    C.save_json(C.ROOT / "reports" / "lockup_status.json", status)   # 공개용: 건수·회사명만
    if CHECK:   # 공개용 진단: 회사명·접수번호·보고서명만 (개인 이름 없음)
        miss = [{"corp": d["corp_name"], "rcept_no": rn, "report_nm": d.get("report_nm", ""), "date": d["date"],
                 "pay_date": (d.get("parsed") or {}).get("pay_date"), "err": d.get("err")}
                for rn, d in sorted(docs.items(), reverse=True)
                if d["kind"] == "ipo" and rn in keep and (d.get("parsed") or {}).get("ipo") and not d["parsed"].get("people")]
        hit = [{"corp": d["corp_name"], "rcept_no": rn, "report_nm": d.get("report_nm", ""), "date": d["date"]}
               for rn, d in sorted(docs.items(), reverse=True)
               if d["kind"] == "ipo" and rn in keep and (d.get("parsed") or {}).get("people")]
        C.save_json(C.ROOT / "reports" / "lockup_diag.json", {"ipo_no_people": miss, "ipo_with_people": hit})
        C.save_json(CHECK_OUT, {"items": items, "ipo_samples": [
            {"rcept_no": rn, "corp": d["corp_name"], **(d.get("parsed") or {})}
            for rn, d in sorted(docs.items(), reverse=True) if d["kind"] == "ipo" and d.get("parsed")][:40],
            "rights_samples": [{"rcept_no": rn, "corp": d["corp_name"], **(d.get("parsed") or {})}
                               for rn, d in sorted(docs.items(), reverse=True)
                               if d["kind"] == "rights" and (d.get("parsed") or {}).get("third")][:40]})
    if C.OUT_PATH.exists():
        data = json.loads(C.OUT_PATH.read_text(encoding="utf-8"))
        data["lockup"] = {"items": items, "pending": pend}
        C.OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    C.log(f"보호예수 해제 {len(items)}건 (원문 대기 {pend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
