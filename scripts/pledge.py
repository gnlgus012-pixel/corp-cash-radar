#!/usr/bin/env python3
"""
대주주 주식담보 현황 — 세 가지 공시를 합쳐 '지금 걸려 있는 담보'를 보여준다 (holders.py 다음, 암호화 전)

  ① 주식등의 대량보유상황보고서(5% 보고) 원문 › '2. 보유주식등에 관한 계약 › 나. 계약 내용'
       보고자·특별관계자(가족·계열사)별 담보 계약 전부 (계약 상대방·주식 수·체결일·계약기간)
       → 보고서마다 '현재 유효한 계약 전체'를 다시 적으므로, (회사, 보고자)별 최신 보고서 = 현재 담보 현황
       → '대출금액' 표가 채워져 있으면 금액도
  ② 최대주주 변경을 수반하는 주식담보제공 계약 체결 (거래소 공시, holders.py 가 이미 읽어 둔 것)
       → 차입금액·담보설정금액이 있어 ①의 같은 계약에 금액을 붙이고, ①에 없는 계약은 따로 표시
  ③ 분기·반기·사업보고서 › 'Ⅶ. 주주에 관한 사항' 의 담보·질권 문구 (회사별 최신 보고서)
       → 형식이 자유로워 문장 그대로 표시 (①②에 안 잡히는 소규모 담보·해제 사실 보완)

  결과 : data["pledge"] = {items, notes, stats}
         reports/pledge_status.json (건수·회사명·담보권자명만 — 개인 이름 없음)
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
import holders as H  # noqa: E402

STORE_PATH = C.CACHE_DIR / "pledge.json"
MAJOR_VER = "m1"
NOTE_VER = "n1"
MAJOR_LOOKBACK = int(C.os.environ.get("PLEDGE_LOOKBACK_DAYS", "730"))   # 5% 보고서: 2년 (담보계약은 대부분 1년 안쪽으로 연장·변경 보고)
NOTE_LOOKBACK = 200                                                     # 정기보고서: 회사별 최신 1건
BACKFILL_WEEKS = int(C.os.environ.get("PLEDGE_BACKFILL_WEEKS", "8"))     # 한 번에 거슬러 올라갈 주 수
MAX_DOCS = int(C.os.environ.get("PLEDGE_MAX_DOCS", "400"))
RESERVE = int(C.os.environ.get("PLEDGE_BUILD_RESERVE_SEC", "180"))
C.CFG["details"]["max_doc_mb"] = 60   # 정기보고서는 수 MB

PLEDGE_RX = re.compile(r"담보|질권|대출|공탁")
NOT_PLEDGE_RX = re.compile(r"^(주식)?대차|대여계약|의결권|공동보유|신탁(?!.*담보)")
TAG = re.compile(r"<[^>]+>")
STATS = Counter()
PENDING: dict = {}


def nz(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def lnorm(s) -> str:
    """담보권자 이름 비교용: (주)·㈜·주식회사·괄호 설명·'등 n인'·공백 제거"""
    s = re.sub(r"\(주\)|㈜|주식회사|\(담보권자\)|등\s*\d+\s*(인|명|개사)", "", str(s or ""))
    s = re.sub(r"\([^)]*\)", "", s)
    return re.sub(r"[\s.,·]+", "", s)


def same_lender(a, b) -> bool:
    a, b = lnorm(a), lnorm(b)
    if not a or not b:
        return True
    return a == b or (len(min(a, b, key=len)) >= 4 and (a in b or b in a))


def num(s) -> int | None:
    s = str(s or "").replace(",", "").strip()
    m = re.fullmatch(r"-?\d+(\.\d+)?", s)
    return int(float(s)) if m else None


def dates_in(s: str) -> list[str]:
    out = []
    for y, m, d in C.DATE_RE.findall(s or ""):
        try:
            out.append(date(int(y), int(m), int(d)).isoformat())
        except ValueError:
            pass
    return out


def time_left() -> float:
    if not C.TIME_BUDGET_SEC:
        return 1e9
    return C.TIME_BUDGET_SEC - (time.monotonic() - C._START)


# ───────────────────────── 목록 ─────────────────────────
def kind_of(report_nm: str, ty: str) -> str | None:
    b = nz(report_nm)
    if ty == "D" and "대량보유상황보고서" in b:
        return "major"
    if ty == "A" and re.search(r"분기보고서|반기보고서|사업보고서", b) and "첨부" not in b:
        return "periodic"
    return None


def scan(store: dict, today: date) -> None:
    sc = store.setdefault("scan", {})
    oldest = today - timedelta(days=MAJOR_LOOKBACK)
    recent_from = today - timedelta(days=4)
    if sc.get("to"):
        recent_from = min(recent_from, C.parse_ymd(sc["to"]) - timedelta(days=1))
    windows = [(max(recent_from, oldest), today)]
    frm = C.parse_ymd(sc["from"]) if sc.get("from") else recent_from
    cur_end = frm - timedelta(days=1)
    while cur_end >= oldest and len(windows) <= BACKFILL_WEEKS:
        windows.append((max(cur_end - timedelta(days=6), oldest), cur_end))
        cur_end -= timedelta(days=7)
    note_from = C.ymd(today - timedelta(days=NOTE_LOOKBACK))
    for a, b in windows:
        try:
            for ty in ("D", "A"):
                if ty == "A" and C.ymd(b) < note_from:
                    continue
                cur = a
                while cur <= b:
                    e = min(cur + timedelta(days=6), b)
                    page = 1
                    while True:
                        j = C.dart_get("list.json", bgn_de=C.ymd(cur), end_de=C.ymd(e), pblntf_ty=ty,
                                       last_reprt_at="Y", page_no=page, page_count=100)
                        STATS["list_pages"] += 1
                        for row in j.get("list") or []:
                            k = kind_of(row.get("report_nm", ""), ty)
                            if not k or (k == "periodic" and row.get("corp_cls") not in ("Y", "K", "N")):
                                continue
                            d = store["docs"].setdefault(row["rcept_no"], {})
                            d.update({"kind": k, "date": row["rcept_dt"], "corp_code": row["corp_code"],
                                      "corp_name": row["corp_name"], "stock_code": row.get("stock_code", ""),
                                      "corp_cls": row.get("corp_cls", ""), "report_nm": row["report_nm"],
                                      "flr_nm": row.get("flr_nm", "")})
                        if page >= int(j.get("total_page") or 1):
                            break
                        page += 1
                    cur = e + timedelta(days=1)
        except (C.OutOfTime, C.DartError) as ex:
            C.warn(f"주식담보 목록 조회 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
            break
        if not sc.get("to") or C.ymd(b) > sc["to"]:
            sc["to"] = C.ymd(b)
        if not sc.get("from") or C.ymd(a) < sc["from"]:
            sc["from"] = C.ymd(a)
    # 정리: 5% 보고서는 보관기간 밖 삭제, 정기보고서는 회사별 최신만
    cut = C.ymd(oldest)
    latest_p = {}
    for rn, d in store["docs"].items():
        if d["kind"] == "periodic" and (d["corp_code"] not in latest_p or rn > latest_p[d["corp_code"]]):
            latest_p[d["corp_code"]] = rn
    for rn in [rn for rn, d in store["docs"].items()
               if (d["kind"] == "major" and d["date"] < cut)
               or (d["kind"] == "periodic" and latest_p.get(d["corp_code"]) != rn)]:
        del store["docs"][rn]


# ───────────────────────── 원문 해석 ─────────────────────────
COLS = [("seq", r"^연번|^순번"), ("name", r"성명|명칭"), ("rel", r"관계"), ("kind", r"주식등의종류"),
        ("shares", r"주식등의수|주식수"), ("party", r"계약상대방|상대방"), ("ctype", r"계약의종류|계약종류"),
        ("signed", r"체결"), ("period", r"계약기간"), ("ratio", r"^비율"), ("memo", r"비고")]


def parse_major(rows: list[list[str]]) -> dict:
    out = {"has_contract": None, "reason": "", "contracts": [], "loans": []}
    hdr = None
    loan_hdr = None
    for r in rows:
        cells = [nz(c) for c in r]
        if not any(cells):
            continue
        if cells[0] in ("보고사유", "변경사유") and len(r) > 1 and not out["reason"]:
            out["reason"] = r[1].strip()[:120]
        if any("주요계약체결" in c and "변경여부" in c for c in cells) and len(r) > 1:
            v = next((c for c in cells[1:] if c in ("있음", "없음", "해당없음", "-")), "")
            out["has_contract"] = v == "있음" if v else out["has_contract"]
        # 계약 내용 표 머리
        if any("계약상대방" in c for c in cells) and any(re.search(r"계약의?종류", c) for c in cells):
            hdr = {}
            for key, rx in COLS:
                for i, c in enumerate(cells):
                    if re.search(rx, c) and key not in hdr and i not in hdr.values():
                        hdr[key] = i
                        break
            loan_hdr = None
            continue
        if any("대출금액" in c for c in cells) and any("주식등의수" in c for c in cells):
            loan_hdr = {k: next((i for i, c in enumerate(cells) if re.search(rx, c)), None)
                        for k, rx in (("shares", r"주식등의수"), ("amount", r"대출금액"), ("debtor", r"채무자"),
                                      ("rate", r"이자율"), ("ltv", r"담보유지"), ("etc", r"기타"))}
            hdr = None
            continue
        if hdr is not None:
            if not re.fullmatch(r"\d+", cells[0] or ""):
                if cells[0] in ("", "-") and len(cells) <= 2:
                    continue
                hdr = None
                continue
            off = len(r) - (max(hdr.values()) + 1)   # 오른쪽 열이 하나 더/덜 있는 표 대비 (연번 기준 왼쪽 정렬)
            g = lambda k: (r[hdr[k]].strip() if k in hdr and hdr[k] < len(r) else "")  # noqa: E731
            ctype = g("ctype")
            out["contracts"].append({
                "name": re.sub(r"\s+", " ", g("name")), "rel": g("rel"), "shares": num(g("shares")),
                "party": re.sub(r"\s+", " ", g("party")), "ctype": ctype, "signed": (dates_in(g("signed")) or [None])[0],
                "period": g("period")[:60], "ratio": g("ratio")[:10], "memo": g("memo")[:80], "off": off})
            continue
        if loan_hdr is not None:
            if not re.fullmatch(r"\d+", cells[0] or ""):
                loan_hdr = None
                continue
            gl = lambda k: (r[loan_hdr[k]].strip() if loan_hdr.get(k) is not None and loan_hdr[k] < len(r) else "")  # noqa: E731
            out["loans"].append({"shares": num(gl("shares")), "amount": num(gl("amount")), "debtor": gl("debtor")[:40],
                                 "rate": gl("rate")[:20], "ltv": gl("ltv")[:20]})
    return out


def doc_lines(txt: str) -> list[str]:
    t = re.sub(r"(?i)</(P|TR|TABLE|TITLE|SPAN|TD|TH)>", "\n", txt)
    t = html.unescape(TAG.sub(" ", t))
    return [re.sub(r"\s+", " ", x).strip() for x in t.split("\n") if x.strip()]


NOTE_RX = re.compile(r"담보|질권")
NOTE_SKIP = re.compile(r"해당\s*사항\s*(이\s*)?없|종속기업|관계기업|유형자산|전환사채\s*관련|사채권자|토지|건물")
# 대주주 주식이 담보로 잡힌 문장만: '주식'과 '담보·질권'이 가깝게 붙어 있거나 반대매매·담보권 실행
NOTE_POS = re.compile(r"(주식|주권|보통주).{0,40}(담보|질권)|(담보|질권).{0,40}(주식|주권|보통주)|반대매매|담보권\s*실행")
NOTE_WHO = re.compile(r"최대주주|대표이사|특수관계인|회장|주주|본인")
NOTE_NEG = re.compile(r"없습니다|없음|없으며|해당\s*사항|정관|인수목적|스팩|예치|신탁|지급보증|배당|질권자에게|제공받은\s*담보|여신전문|계속보유확약|차입할\s*수\s*없")


def good_note(l: str) -> bool:
    return bool(NOTE_POS.search(l) and NOTE_WHO.search(l) and not NOTE_NEG.search(l) and not NOTE_SKIP.search(l))


def parse_periodic(txt: str) -> dict:
    lines = doc_lines(txt)
    starts = [i for i, l in enumerate(lines) if re.search(r"주주에\s*관한\s*사항", l) and len(l) < 40]
    best = []
    for s in starts:
        e = next((i for i in range(s + 1, min(len(lines), s + 6000))
                  if re.search(r"임원\s*및\s*직원", lines[i]) and len(lines[i]) < 40), min(len(lines), s + 6000))
        if e - s > len(best):
            best = lines[s:e]
    notes = []
    for l in best:
        if NOTE_RX.search(l) and len(l) >= 15 and good_note(l):
            l = l[:400]
            if l not in notes:
                notes.append(l)
    return {"notes": notes[:8], "section_lines": len(best)}


def parse_pending(store: dict) -> None:
    # 5% 보고서는 (회사, 보고자)별 최신 보고서만 읽으면 됨 (보고서마다 현재 유효한 계약 전체를 다시 적음)
    latest = {}
    for rn, d in store["docs"].items():
        if d["kind"] == "major":
            k = (d["corp_code"], nz(d.get("flr_nm")))
            if k not in latest or rn > latest[k]:
                latest[k] = rn
    keep = set(latest.values())
    pend = [(rn, d) for rn, d in store["docs"].items()
            if (d["kind"] != "major" or rn in keep)
            and d.get("pv") != (MAJOR_VER if d["kind"] == "major" else NOTE_VER) and d.get("tries", 0) < 3]
    # 5% 보고서 3건 : 정기보고서 1건 비율로 섞어서(최신순) — 한쪽만 밀리지 않게
    mj = sorted([p for p in pend if p[1]["kind"] == "major"],
                key=lambda x: ("약식" not in x[1]["report_nm"], x[0]), reverse=True)
    pr = sorted([p for p in pend if p[1]["kind"] != "major"], key=lambda x: x[0], reverse=True)
    pend = []
    while mj or pr:
        pend += mj[:3] + pr[:1]
        mj, pr = mj[3:], pr[1:]
    PENDING["major"] = [rn for rn, d in pend if d["kind"] == "major"]
    PENDING["periodic"] = [rn for rn, d in pend if d["kind"] != "major"]
    C.log(f"주식담보 원문 대기 {len(pend):,}건 (5% {sum(1 for p in pend if p[1]['kind'] == 'major'):,})")
    for rn, d in pend[:MAX_DOCS]:
        if time_left() < RESERVE:
            C.log("정리 시간 확보를 위해 원문 조회를 여기서 멈춤 (다음 실행에서 이어서)")
            break
        try:
            txt, err = C.dart_document(rn)
        except (C.OutOfTime, C.DartError) as ex:
            C.warn(f"주식담보 원문 조회 중단 ({ex.__class__.__name__})")
            break
        STATS["docs_" + d["kind"]] += 1
        if not txt:
            d["err"] = err
            d["tries"] = d.get("tries", 0) + 1
            STATS["doc_err"] += 1
            continue
        if d["kind"] == "major":
            d["parsed"] = parse_major(C.doc_rows(txt))
            d["pv"] = MAJOR_VER
        else:
            d["parsed"] = parse_periodic(txt)
            d["pv"] = NOTE_VER
        d.pop("err", None)
        PENDING[d["kind"] if d["kind"] == "major" else "periodic"].remove(rn)


# ───────────────────────── 현재 담보 목록 ─────────────────────────
def is_pledge(ctype: str) -> bool:
    c = nz(ctype)
    return bool(PLEDGE_RX.search(c)) and not NOT_PLEDGE_RX.search(c)


def period_end(p: str) -> tuple[str | None, str | None, bool]:
    """계약기간 문자열 → (시작, 만기, 만기 미정 여부)"""
    ds = dates_in(p)
    open_end = bool(re.search(r"해지\s*시|해제\s*시|상환\s*시|변제\s*시|까지\s*$|별도|미정|만기\s*없", p)) and len(ds) <= 1
    if len(ds) >= 2:
        return ds[0], ds[-1], False
    if len(ds) == 1:
        return (ds[0], None, True) if open_end or "~" in p and p.strip().endswith("~") else (None, ds[0], False)
    return None, None, True


def lender_kind(s: str) -> str:
    s = nz(s)
    if re.search(r"증권|투자증권|금융투자", s):
        return "증권"
    if re.search(r"저축은행", s):
        return "저축은행"
    if re.search(r"은행|농협|수협|신협|새마을", s):
        return "은행"
    if re.search(r"캐피탈|카드|대부|파이낸셜|여신", s):
        return "캐피탈·여신"
    if re.search(r"세무서|국세청|공탁|법원", s):
        return "세금·공탁"
    if re.search(r"조합|투자|PEF|사모|자산운용|파트너스|인베스트", s):
        return "투자조합·펀드"
    return "기타(개인·법인)"


def build(store: dict, hstore: dict, prices: dict, today: date, corp_map: dict) -> tuple[list[dict], dict]:
    td = today.isoformat()
    # ① (회사, 보고자)별 최신 5% 보고서
    latest = {}
    for rn, d in store["docs"].items():
        if d["kind"] != "major" or not d.get("parsed"):
            continue
        k = (d["corp_code"], nz(d.get("flr_nm")))
        if k not in latest or rn > latest[k]:
            latest[k] = rn
    items = []
    seen = set()
    for (cc, _), rn in latest.items():
        d = store["docs"][rn]
        p = d["parsed"]
        loans = [l for l in p.get("loans") or [] if (l.get("amount") or 0) >= 1e7]
        for c in p["contracts"]:
            if not is_pledge(c["ctype"]):
                STATS["non_pledge_contract"] += 1
                continue
            if not c.get("shares") or c["shares"] <= 0:
                STATS["zero_shares"] += 1
                continue
            st, end, open_end = period_end(c["period"])
            if end and end < td:
                STATS["expired"] += 1
                continue
            # 정정보고서에 정정 전·후 표가 함께 실리면 같은 행이 두 번 나온다 → 한 번만
            key = (rn, nz(c["name"]), c["shares"], nz(c["party"]), c["signed"], c["period"])
            if key in seen:
                STATS["dup_row"] += 1
                continue
            seen.add(key)
            amt = next((l["amount"] for l in loans if l.get("shares") and l["shares"] == c["shares"]), None)
            items.append({"corp_code": cc, "corp_name": d["corp_name"], "corp_cls": d.get("corp_cls", ""),
                          "stock_code": d.get("stock_code") or corp_map.get(cc, {}).get("stock_code", ""),
                          "reporter": d.get("flr_nm", ""), "person": c["name"], "rel": c["rel"],
                          "is_person": H.is_person(c["name"]), "shares": c["shares"], "lender": c["party"],
                          "lender_kind": lender_kind(c["party"]), "ctype": c["ctype"], "signed": c["signed"],
                          "start": st, "end": end, "open_end": open_end, "period": c["period"],
                          "loan": amt, "src": "5% 보고", "rcept_no": rn, "date": H.ymd_dash(d["date"]),
                          "reason": p.get("reason", "")})
    # ② 최대주주 변경 수반 담보 (회사·채무자별 최신 공시) — ①과 같은 계약이면 금액만 붙임
    hlatest = {}
    for rn, d in (hstore.get("docs") or {}).items():
        if d.get("kind") != "pledge" or not d.get("parsed"):
            continue
        hlatest.setdefault(d["corp_code"], []).append(rn)
    idx = defaultdict(list)
    for it in items:
        idx[it["corp_code"]].append(it)
    for cc, rns in hlatest.items():
        rn = max(rns)
        d = hstore["docs"][rn]
        p = d["parsed"]
        for c in p.get("contracts") or []:
            end = c.get("end")
            if end and end < td:
                continue
            if not c.get("shares"):
                continue
            cands = [x for x in idx.get(cc, []) if x["src"] == "5% 보고" and same_lender(c.get("lender"), x["lender"])]
            # 주식 수가 같은 계약 우선. 없으면 5% 보고서가 더 최신일 때 같은 담보권자 중 주식 수가 가장 가까운 계약
            # (5% 보고서는 그 시점 계약 전체를 다시 싣기 때문에 수량이 바뀐 같은 계약으로 본다)
            match = next((x for x in cands if x["shares"] == c["shares"]), None)
            if not match:
                newer = [x for x in cands if x["rcept_no"][:8] >= rn[:8] and "amount_set" not in x]
                if newer:
                    match = min(newer, key=lambda x: abs(x["shares"] - c["shares"]))
            if match:
                match["amount_set"] = c.get("amount")
                match["src2"] = rn
                STATS["matched_major_change"] += 1
                continue
            who = c.get("debtor") or p.get("pledgor") or ""
            items.append({"corp_code": cc, "corp_name": d["corp_name"], "corp_cls": d.get("corp_cls", ""),
                          "stock_code": d.get("stock_code") or corp_map.get(cc, {}).get("stock_code", ""),
                          "reporter": p.get("pledgor", ""), "person": who, "rel": p.get("debtor_relation") or "최대주주",
                          "is_person": H.is_person(who), "shares": c.get("shares"), "lender": c.get("lender") or "",
                          "lender_kind": lender_kind(c.get("lender") or ""), "ctype": c.get("type") or "주식담보",
                          "signed": c.get("signed"), "start": c.get("start"), "end": end, "open_end": not end,
                          "period": "", "loan": None, "amount_set": c.get("amount"), "debt_total": p.get("debt_total"),
                          "src": "최대주주변경 수반", "rcept_no": rn, "date": H.ymd_dash(d["date"]), "reason": ""})
    # 평가액 = 주식 수 × 최근 종가
    for it in items:
        px = H.close_on(prices, it["stock_code"], td, today) if it["stock_code"] and time_left() > 30 else None
        it["px"] = px
        it["value"] = it["shares"] * px if px and it.get("shares") else None
    items.sort(key=lambda x: (x["corp_name"], -(x.get("value") or 0)))
    # ③ 정기보고서 문구
    notes = {}
    for rn, d in store["docs"].items():
        texts = [t for t in (d.get("parsed") or {}).get("notes") or [] if good_note(t)] if d["kind"] == "periodic" else []
        if texts:
            notes[d["corp_code"]] = {"corp_name": d["corp_name"], "corp_cls": d.get("corp_cls", ""),
                                     "stock_code": d.get("stock_code", ""), "rcept_no": rn,
                                     "report": d["report_nm"], "date": H.ymd_dash(d["date"]), "texts": texts}
    return items, notes


def main() -> int:
    if not C.DART_KEY:
        return 1
    today = C.now_kst().date()
    store = C.load_json(STORE_PATH, {"docs": {}})
    store.setdefault("docs", {})
    scan(store, today)
    C.save_json(STORE_PATH, store)
    parse_pending(store)
    C.save_json(STORE_PATH, store)
    hstore = C.load_json(H.STORE_PATH, {"docs": {}})
    prices = H.load_prices()
    corp_map = C.load_json(C.CACHE_DIR / "corpcode.json", {}).get("map", {})
    items, notes = build(store, hstore, prices, today, corp_map)
    C.save_json(H.PRICE_PATH, prices)
    docs = store["docs"]
    pend = {"major": len(PENDING.get("major", [])), "periodic": len(PENDING.get("periodic", []))}
    stats = {"items": len(items), "corps": len({i["corp_code"] for i in items}), "notes": len(notes),
             "by_src": Counter(i["src"] for i in items), "by_lender": Counter(i["lender_kind"] for i in items),
             "person_items": sum(1 for i in items if i["is_person"]),
             "docs": Counter(d["kind"] for d in docs.values()), "pending": pend,
             "scan_from": store.get("scan", {}).get("from"), "scan_to": store.get("scan", {}).get("to")}
    if C.OUT_PATH.exists():
        data = json.loads(C.OUT_PATH.read_text(encoding="utf-8"))
        data["pledge"] = {"items": items, "notes": notes, "stats": stats}
        C.OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    top = defaultdict(int)
    for i in items:
        top[i["corp_name"]] += i.get("value") or 0
    C.save_json(C.ROOT / "reports" / "pledge_status.json", {   # 공개용: 건수·회사명·담보권자명만
        "generated_at": C.now_kst().isoformat(timespec="minutes"), **stats, "run": STATS,
        "corp_top_value_eok": sorted(((k, round(v / 1e8)) for k, v in top.items()), key=lambda x: -x[1])[:20],
        "lender_top": Counter(i["lender"] for i in items).most_common(20),
        "no_end": sum(1 for i in items if not i["end"]), "no_value": sum(1 for i in items if not i.get("value"))})
    C.log(f"주식담보 {len(items):,}건 · {stats['corps']}개사 · 정기보고서 문구 {len(notes)}개사 · 대기 {pend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
