#!/usr/bin/env python3
"""
대주주(개인) 자금 공시 수집 — 최대주주와 특수관계인 개인 중심

수집 대상 (DART)
  거래소공시  최대주주등소유주식변동신고서            → 지분 매각(장내·시간외·장외), 증여·상속
  거래소공시  최대주주변경을수반하는주식양수도계약체결  → 경영권 양도 (양도인 개인, 양수도 대금)
  거래소공시  최대주주변경을수반하는주식담보제공계약체결 → 주식담보 (채무자 개인, 차입금액·채권자·만기)
  지분공시    임원ㆍ주요주주특정증권등거래계획보고서    → 매도 계획 (개인, 예정 금액·기간) / 철회보고서로 취소 표시
  (계산)      현금배당결정 × 최대주주 현황(사업보고서)  → 배당 수령 추정

금액
  양수도·담보·매도계획은 공시 원문 금액. 장내매도·증여·상속은 원문에 단가가 없어 '변동일 종가 × 주식수'로 추정(est=True).
  배당 수령은 '1주당 배당금 × 직전 사업보고서 기준 보유 주식수' 추정.

실행
  python scripts/holders.py           docs/data/data.json 에 holders 항목을 붙인다 (collect.py 다음, 암호화 전)
  python scripts/holders.py --check   최근 30일만 수집해 파싱 결과·원문 일부를 reports/holders_check.json 으로 (검증용)
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

PARSE_VER = "h1"
STORE_PATH = C.CACHE_DIR / "holders.json"
HYSLR_PATH = C.CACHE_DIR / "hyslr.json"
PRICE_PATH = C.CACHE_DIR / "prices.json"
CHECK_OUT = C.ROOT / "reports" / "holders_check.json"
CHECK = "--check" in sys.argv

HCFG = {"lookback_days": 365, "rescan_days": 4, "max_docs_per_run": 3000, "backfill_weeks_per_run": 12,
        "max_hyslr_calls_per_run": 250, **C.CFG.get("holders", {})}
if C.os.environ.get("HOLDERS_BACKFILL_WEEKS"):
    HCFG["backfill_weeks_per_run"] = int(C.os.environ["HOLDERS_BACKFILL_WEEKS"])
    HCFG["max_hyslr_calls_per_run"] = 1200

TARGETS = [  # (공시유형, 보고서명 핵심어(공백 제거), 종류)
    ("I", "최대주주등소유주식변동신고서", "change"),
    ("I", "최대주주변경을수반하는주식양수도계약체결", "transfer"),
    ("I", "최대주주변경을수반하는주식담보제공계약체결", "pledge"),
    ("D", "임원ㆍ주요주주특정증권등거래계획보고서", "plan"),
    ("D", "임원ㆍ주요주주특정증권등거래계획철회보고서", "plan_cancel"),
]
EXCLUDE = ("자회사의주요경영사항", "종속회사의주요경영사항", "해제", "취소")

SELL_RX = re.compile(r"매도|처분|시간외매매\s*\(-\)|블록딜|장외\s*양도|양도\s*\(-\)")
GIFT_OUT_RX = re.compile(r"증여")
INHERIT_RX = re.compile(r"상속")
CORP_WORDS = ("(주)", "㈜", "주식회사", "조합", "투자", "펀드", "재단", "법인", "LTD", "LLC", "INC", "CO.", "유한", "공단",
              "은행", "증권", "캐피탈", "홀딩스", "파트너스", "PEF", "사모", "신탁", "자산운용", "기금", "CORP", "L.P", "LP",
              "외 ", "외1", "외2", "외3", "외4", "외5", "회사", "그룹", "협회", "학교", "농협", "보험", "글로벌", "인베스트")


SURNAMES = set("김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반라왕금옥육인맹제모탁국어은편용예경봉사부가복태목형피두감음빈동온호범좌팽승간상시갈단견당화창궉계탄묵근미요")
COMPOUND = ("남궁", "황보", "제갈", "선우", "독고", "사공", "서문", "동방", "어금")
ORG_SUFFIX = ("생명", "물산", "화재", "전자", "자동차", "건설", "은행", "금융", "증권", "공단", "재단", "정부", "공사", "산업", "제약",
              "화학", "지주", "기업", "상사", "통신", "에너지", "개발", "카드", "해운", "항공", "철강", "제철", "중공업", "전기", "엔지니어링",
              "시스템", "테크", "바이오", "미디어", "문화", "장학회", "복지회", "학원", "병원", "교회", "연금", "기금", "협회", "중앙회", "재정부",
              "센터", "학교", "대학교", "산학협력단", "운용", "자산", "파트너스", "벤처스",
              "캐피탈", "인베스트", "홀딩스", "글로벌", "코리아", "인터내셔널", "엔터", "모터스", "건업", "실업", "물류", "상선", "흥업")


ORG_EXACT = {"정부", "국세청", "조달청", "국방부", "교육부", "대한민국", "국가", "본인", "기타", "합계", "소계"}
_CORP_NAMES: set | None = None


def corp_names() -> set:
    """DART 등록 회사명 (한화·기아처럼 짧은 회사명이 사람 이름으로 오인되지 않게)"""
    global _CORP_NAMES
    if _CORP_NAMES is None:
        m = C.load_json(C.CACHE_DIR / "corpcode.json", {}).get("map", {})
        _CORP_NAMES = {re.sub(r"\s+|\(주\)|㈜|주식회사", "", v.get("name", "")) for v in m.values()}
        _CORP_NAMES.discard("")
    return _CORP_NAMES


def is_person(name: str) -> bool:
    """개인 이름인지 (공시에 개인/법인 구분이 없는 표에서 사용). 한국 성씨로 시작하는 2~4자(복성은 5자) + 기관·회사형 어미 제외"""
    n = (name or "").strip()
    if not n or n in ("-",):
        return False
    up = n.upper()
    if any(w.upper() in up for w in CORP_WORDS):
        return False
    m = re.search(r"\(([가-힣\s]{2,6})\)", n)  # HUR JULIE(허승원) 같은 표기
    core = re.sub(r"\s+", "", re.sub(r"\(.*?\)", "", n))
    if not re.fullmatch(r"[가-힣]{2,5}", core):
        return bool(m) and is_person(m.group(1))
    if len(core) == 5 or (len(core) == 4 and core[:2] in COMPOUND):
        if core[:2] not in COMPOUND:
            return False
    elif core[0] not in SURNAMES:
        return False
    if len(core) >= 4 and core[:2] not in COMPOUND:
        return False  # 4자 이름은 거의 없고 '삼성생명' 같은 기관명이 대부분
    if core in ORG_EXACT or core in corp_names():
        return False
    return not any(core.endswith(x) and len(core) - len(x) >= 2 for x in ORG_SUFFIX)


def base(nm: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"^\[[^\]]*\]", "", nm.strip()))


def target_of(report_nm: str):
    b = base(report_nm)
    if any(x in b for x in EXCLUDE):
        return None
    for ty, key, kind in TARGETS:
        if key in b:
            # '거래계획보고서' 가 '거래계획철회보고서' 에 안 걸리도록 철회 먼저 판별
            if kind == "plan" and "철회" in b:
                continue
            return kind
    return None


def num(s) -> int | None:
    if s is None:
        return None
    t = re.sub(r"[^\d\-.]", "", str(s))
    if t in ("", "-", ".", "-."):
        return None
    try:
        return int(float(t))
    except ValueError:
        return None


def ymd_dash(s: str) -> str | None:
    s = (s or "").strip()
    if re.fullmatch(r"20\d{6}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return C.parse_date(s)


def cell(row, i, default=""):
    return row[i].strip() if i < len(row) and row[i] is not None else default


# ───────────────────────── 파서 ─────────────────────────
def parse_change(rows: list[list[str]]) -> dict:
    """최대주주등소유주식변동신고서: 사람별 블록(성명/국내외 구분/관계) 아래 변경 내역 행"""
    people, cur, mode = [], None, None
    for r in rows:
        c0 = cell(r, 0)
        if c0 == "성명" and len(r) > 1 and not cell(r, 1).startswith("생년월일"):
            cur = {"name": cell(r, 1), "type": "", "relation": "", "changes": []}
            people.append(cur)
            mode = "head"
            continue
        if cur is None:
            continue
        if c0.startswith("국내외") and mode == "head":
            cur["type"] = cell(r, 1)
        elif c0.startswith("최대주주") and "관계" in c0 and mode == "head":
            cur["relation"] = cell(r, 1)
        elif c0 == "변경일":
            mode = "rows"
        elif mode == "rows":
            d = ymd_dash(c0)
            if not d:
                mode = None
                continue
            cur["changes"].append({"date": d, "cause": cell(r, 1), "kind": cell(r, 2),
                                   "before": num(cell(r, 3)), "delta": num(cell(r, 4)), "after": num(cell(r, 5)),
                                   "note": cell(r, 6)})
    return {"people": people}


def parse_plan(rows: list[list[str]]) -> dict:
    out = {"name": "", "reporter_type": "", "position": "", "major": "", "plans": [], "purpose": ""}
    mode = None
    for i, r in enumerate(rows):
        c0 = cell(r, 0)
        if c0.startswith("보고자 구분") or c0.startswith("보고자구분"):
            out["reporter_type"] = cell(r, 1)
        elif c0.startswith("성명(명칭)") and not out["name"]:
            out["name"] = cell(r, 2) if cell(r, 1).replace(" ", "") == "한글" else cell(r, 1)
        elif c0.startswith("발행회사와의") and len(r) >= 5:
            out["position"] = cell(r, 4) if cell(r, 4) not in ("-", "") else cell(r, 2)
        elif c0 == "주요주주":
            out["major"] = cell(r, 1)
        elif c0 == "거래목적" and i + 1 < len(rows):
            out["purpose"] = cell(rows[i + 1], 0)[:120]
        elif c0.startswith("거래개시일"):
            mode = "plan"
        elif mode == "plan":
            d = ymd_dash(c0)
            if not d:
                mode = None
                continue
            out["plans"].append({"start": d, "end": ymd_dash(cell(r, 1)), "method": cell(r, 3), "kind": cell(r, 4),
                                 "shares": num(cell(r, 5)), "price": num(cell(r, 6)), "amount": num(cell(r, 7)),
                                 "note": cell(r, 8)})
    return out


def parse_cancel(rows: list[list[str]]) -> dict:
    """거래계획 철회보고서: 보고자 이름, 철회사유, 철회된 계획의 거래개시일"""
    out = {"name": "", "reason": "", "starts": []}
    mode = None
    for r in rows:
        c0 = cell(r, 0)
        if c0.replace(" ", "").startswith("보고자") and not out["name"]:
            out["name"] = cell(r, 2) if "성" in cell(r, 1) else cell(r, 1)
        elif c0 == "철회사유":
            out["reason"] = " ".join(x for x in r[1:] if x.strip())[:150]
        elif c0.startswith("거래개시일"):
            mode = "p"
        elif mode == "p":
            d = ymd_dash(c0)
            if not d:
                mode = None
                continue
            out["starts"].append(d)
    return out


def parse_pledge(rows: list[list[str]]) -> dict:
    out = {"pledgor": "", "debt_total": None, "secured_total": None, "contract_date": None, "contracts": [],
           "debtor_relation": ""}
    mode = None
    for r in rows:
        c0 = cell(r, 0)
        if c0.startswith("- 명칭") and not out["pledgor"]:
            out["pledgor"] = cell(r, 1)
        elif c0.startswith("2. 채무") or c0.startswith("2.채무"):
            out["debt_total"] = num(cell(r, 1))
        elif c0.startswith("3. 담보설정") or c0.startswith("3.담보설정"):
            out["secured_total"] = num(cell(r, 1))
        elif c0.startswith("5. 담보권") or c0.startswith("5.담보권"):
            out["contract_date"] = ymd_dash(cell(r, 1))
        elif c0 == "순번" and "채권자" in cell(r, 1):
            mode = "ct"
        elif mode == "ct":
            if c0 == "시작일":
                continue
            if not c0.isdigit():
                mode = None
                continue
            out["contracts"].append({"lender": cell(r, 1), "debtor": cell(r, 2), "purpose": cell(r, 3),
                                     "amount": num(cell(r, 4)), "type": cell(r, 5), "shares": num(cell(r, 6)),
                                     "start": ymd_dash(cell(r, 8)), "end": ymd_dash(cell(r, 9)),
                                     "signed": ymd_dash(cell(r, 10))})
        elif c0.startswith("- 채무자와 당해 상장법인의 관계"):
            out["debtor_relation"] = cell(r, 1)
    return out


def parse_transfer(rows: list[list[str]]) -> dict:
    out = {"seller": "", "seller_rel": "", "buyer": "", "shares": None, "price": None, "amount": None,
           "close_date": None, "contract_date": None, "holders": []}
    mode = None
    for r in rows:
        c0 = cell(r, 0)
        joined = "|".join(r)
        if "-양도인" in joined and not out["seller"]:
            k = [x.strip() for x in r].index(next(x for x in r if "-양도인" in x))
            out["seller"] = cell(r, k + 1)
            if "회사와의 관계" in cell(r, k + 2):
                out["seller_rel"] = cell(r, k + 3)
        elif c0.startswith("-양수인") and not out["buyer"]:
            out["buyer"] = cell(r, 1)
        elif "양수도 주식수" in joined:
            k = next(i for i, x in enumerate(r) if "양수도 주식수" in x)
            out["shares"] = num(cell(r, k + 1))
        elif c0.startswith("1주당 가액"):
            t = re.sub(r"[^\d.]", "", cell(r, 1))
            out["price"] = float(t) if re.fullmatch(r"\d+(\.\d+)?", t) else None
        elif c0.startswith("양수도 대금"):
            out["amount"] = num(cell(r, 1))
        elif c0.startswith("-변경 예정일자") or c0.startswith("-변경예정일자"):
            out["close_date"] = ymd_dash(cell(r, 1))
        elif c0.startswith("4. 계약일자") or c0.startswith("4.계약일자"):
            out["contract_date"] = ymd_dash(cell(r, 1))
        elif c0.startswith("성명") and len(r) > 2 and cell(r, 1) == "관계":
            mode = "h"
        elif mode == "h":
            if c0 == "주식수":
                continue
            if len(r) < 6 or not c0 or c0.startswith("1.") or c0.startswith("※"):
                mode = None
                continue
            out["holders"].append({"name": c0, "relation": cell(r, 1), "before": num(cell(r, 2)),
                                   "after": num(cell(r, 4)) or 0})
    return out


PARSERS = {"change": parse_change, "plan": parse_plan, "plan_cancel": parse_cancel,
           "pledge": parse_pledge, "transfer": parse_transfer}


# ───────────────────────── 시세 (추정 금액용) ─────────────────────────
def load_prices() -> dict:
    return C.load_json(PRICE_PATH, {})


def fetch_prices(code: str, start: date, end: date) -> dict:
    """네이버 일별 시세 (종가). 실패하면 빈 dict — 금액은 '미산출'로 남는다"""
    try:
        r = C.http_get("https://api.finance.naver.com/siseJson.naver",
                       params={"symbol": code, "requestType": 1, "startTime": C.ymd(start), "endTime": C.ymd(end),
                               "timeframe": "day"}, headers={"User-Agent": "Mozilla/5.0"})
        txt = re.sub(r"\s+", "", r.text)
        arr = ast.literal_eval(txt)
        return {str(row[0]): int(row[4]) for row in arr[1:] if row and str(row[0]).isdigit()}
    except Exception as e:  # noqa: BLE001
        STATS["price_errors"] += 1
        if STATS["price_errors"] <= 3:
            C.warn(f"시세 조회 실패 {code}: {str(e)[:80]}")
        return {}


def close_on(prices: dict, code: str, d: str, today: date) -> int | None:
    """d(YYYY-MM-DD) 당일 또는 직전 거래일 종가"""
    if not code:
        return None
    p = prices.setdefault(code, {"fetched_to": "", "px": {}})
    want = d.replace("-", "")
    if want > p["fetched_to"] or not p["px"] or min(p["px"]) > want:
        start = min(C.parse_ymd(want) - timedelta(days=10), C.parse_ymd(min(p["px"])) if p["px"] else date(2100, 1, 1))
        got = fetch_prices(code, start, today)
        if got:
            p["px"].update(got)
            p["fetched_to"] = C.ymd(today)
        else:
            p["fetched_to"] = max(p["fetched_to"], want)  # 같은 실행에서 반복 시도 방지
    cands = [k for k in p["px"] if k <= want]
    return p["px"][max(cands)] if cands else None


# ───────────────────────── 수집 ─────────────────────────
STATS = Counter()
DIV_ANOM: list[dict] = []   # 배당 추정 검증에서 걸린 건 (개인 이름 없이 회사·수치만)


def scan_lists(store: dict, today: date) -> None:
    """목록 조회: 최근 rescan_days 는 매번 다시, 과거는 lookback 까지 한 주씩 거슬러 채움"""
    lb = 30 if CHECK else HCFG["lookback_days"]
    oldest_needed = today - timedelta(days=lb)
    sc = store.setdefault("scan", {})
    windows = []
    recent_from = today - timedelta(days=HCFG["rescan_days"])
    if sc.get("to"):
        recent_from = min(recent_from, C.parse_ymd(sc["to"]) - timedelta(days=1))
    windows.append((max(recent_from, oldest_needed), today))
    frm = C.parse_ymd(sc["from"]) if sc.get("from") else recent_from
    cur_end = frm - timedelta(days=1)
    while cur_end >= oldest_needed and len(windows) <= HCFG["backfill_weeks_per_run"]:
        windows.append((max(cur_end - timedelta(days=6), oldest_needed), cur_end))
        cur_end = cur_end - timedelta(days=7)
    for a, b in windows:
        try:
            for ty in ("I", "D"):
                cur = a
                while cur <= b:
                    e = min(cur + timedelta(days=6), b)
                    page = 1
                    while True:
                        j = C.dart_get("list.json", bgn_de=C.ymd(cur), end_de=C.ymd(e), pblntf_ty=ty,
                                       last_reprt_at="Y", page_no=page, page_count=100)
                        STATS["list_pages"] += 1
                        for row in j.get("list") or []:
                            kind = target_of(row.get("report_nm", ""))
                            if not kind:
                                continue
                            d = store["docs"].setdefault(row["rcept_no"], {})
                            d.update({"kind": kind, "date": row["rcept_dt"], "corp_code": row["corp_code"],
                                      "corp_name": row["corp_name"], "stock_code": row.get("stock_code", ""),
                                      "corp_cls": row.get("corp_cls", ""), "report_nm": row["report_nm"],
                                      "flr_nm": row.get("flr_nm", "")})
                        if page >= int(j.get("total_page") or 1):
                            break
                        page += 1
                    cur = e + timedelta(days=1)
        except (C.OutOfTime, C.DartError) as ex:
            C.warn(f"대주주 공시 목록 조회 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
            break
        if not sc.get("to") or C.ymd(b) > sc["to"]:
            sc["to"] = C.ymd(b)
        if not sc.get("from") or C.ymd(a) < sc["from"]:
            sc["from"] = C.ymd(a)
    # 보관 기간 밖 정리
    cut = C.ymd(oldest_needed)
    for rn in [rn for rn, d in store["docs"].items() if d.get("date", "") < cut]:
        del store["docs"][rn]


def parse_pending(store: dict) -> None:
    pend = [rn for rn, d in store["docs"].items() if d.get("pv") != PARSE_VER]
    pend.sort(reverse=True)  # 최신 먼저
    C.log(f"대주주 공시 원문 대기 {len(pend):,}건")
    import time
    for rn in pend[: HCFG["max_docs_per_run"]]:
        if C.TIME_BUDGET_SEC and time.monotonic() - C._START > C.TIME_BUDGET_SEC - 300:
            C.log("배당 수령 추정 시간 확보를 위해 원문 조회를 여기서 멈춤 (다음 실행에서 이어서)")
            break
        d = store["docs"][rn]
        try:
            txt, err = C.dart_document(rn)
        except (C.OutOfTime, C.DartError) as ex:
            C.warn(f"대주주 공시 원문 조회 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
            break
        STATS["docs"] += 1
        if not txt:
            d["err"] = err
            STATS["doc_err"] += 1
            continue
        rows = [r for r in C.doc_rows(txt) if any(x.strip() for x in r)]
        d["parsed"] = PARSERS[d["kind"]](rows)
        d["pv"] = PARSE_VER
        d.pop("err", None)
        if CHECK:
            d["_rows"] = rows[:120]


# ───────────────────────── 항목 만들기 ─────────────────────────
def build_items(store: dict, prices: dict, today: date, corp_map: dict) -> list[dict]:
    items = []
    cancels = defaultdict(list)
    for rn, d in store["docs"].items():
        if d.get("kind") == "plan_cancel" and d.get("parsed"):
            nm = re.sub(r"\s+", "", d["parsed"].get("name") or d.get("flr_nm", ""))
            cancels[(d["corp_code"], nm)].append((d["date"], rn, d["parsed"].get("starts", []), d["parsed"].get("reason", "")))

    def base_item(rn, d, cat):
        return {"id": f"{rn}-{len(items)}", "cat": cat, "rcept_no": rn, "date": d["date"], "corp_code": d["corp_code"],
                "corp_name": d["corp_name"], "stock_code": d.get("stock_code") or corp_map.get(d["corp_code"], {}).get("stock_code", ""),
                "corp_cls": d.get("corp_cls", "")}

    for rn, d in store["docs"].items():
        p = d.get("parsed")
        if not p:
            continue
        k = d["kind"]
        if k == "change":
            for person in p["people"]:
                if "개인" not in person["type"]:
                    continue
                groups = defaultdict(list)
                for ch in person["changes"]:
                    cause = ch["cause"]
                    if INHERIT_RX.search(cause) or GIFT_OUT_RX.search(cause) or "수증" in cause:
                        cat = "증여·상속"
                    elif SELL_RX.search(cause) and "매수" not in cause:
                        cat = "지분 매각"
                    else:
                        continue
                    groups[cat].append(ch)
                for cat, chs in groups.items():
                    it = base_item(rn, d, cat)
                    sh = sum(abs(c["delta"] or 0) for c in chs)
                    amt, missing = 0, False
                    for c in chs:
                        px = close_on(prices, it["stock_code"], c["date"], today)
                        if px is None:
                            missing = True
                        else:
                            amt += abs(c["delta"] or 0) * px
                    dates = sorted(c["date"] for c in chs)
                    it.update({"person": person["name"], "relation": person["relation"], "shares": sh,
                               "amount": None if missing and not amt else amt, "est": True,
                               "event_date": dates[-1], "date_from": dates[0],
                               "how": ", ".join(sorted({re.sub(r"\s*\([+-]\)", "", c["cause"]) for c in chs})),
                               "note": "변동일 종가 × 주식수 추정" + (" (일부 종가 없음)" if missing and amt else "")})
                    items.append(it)
        elif k == "plan":
            if "개인" not in p.get("reporter_type", ""):
                continue
            outs = [x for x in p["plans"] if re.search(r"\(-\)|매도|처분|증여", x.get("method", ""))]
            gifts = [x for x in outs if "증여" in x["method"]]
            sells = [x for x in outs if x not in gifts]
            for g in gifts:  # 증여 계획 → 증여·상속 (수증자 증여세 수요), 금액은 예정 단가 또는 공시일 종가로 추정
                it = base_item(rn, d, "증여·상속")
                sh = abs(g["shares"] or 0)
                px = g["price"] if g.get("price") and g["price"] > 0 else close_on(prices, it["stock_code"], ymd_dash(d["date"]) or g["start"], today)
                it.update({"person": p["name"], "relation": " · ".join(x for x in (p["position"], p["major"]) if x and x != "-"),
                           "shares": sh, "amount": (abs(g["amount"]) if g.get("amount") else (sh * px if px and sh else None)),
                           "est": not g.get("amount"), "event_date": g["start"],
                           "how": "증여 예정 (거래계획보고서)",
                           "note": "예정일 " + g["start"] + (" · 목적: " + p["purpose"] if p.get("purpose") and p["purpose"] != "-" else "")})
                items.append(it)
            if not sells:
                continue
            it = base_item(rn, d, "매도 계획")
            nm = re.sub(r"\s+", "", p["name"])
            starts = {x["start"] for x in sells}
            cs = [c for c in cancels.get((d["corp_code"], nm), []) if c[0] >= d["date"]]
            canc = [c for c in cs if starts & set(c[2])] or [c for c in cs if not c[2]]
            it.update({"person": p["name"], "relation": " · ".join(x for x in (p["position"], p["major"]) if x and x != "-"),
                       "shares": sum(abs(x["shares"] or 0) for x in sells), "amount": sum(abs(x["amount"] or 0) for x in sells) or None,
                       "est": False, "event_date": min(x["start"] for x in sells), "date_to": max((x["end"] or x["start"]) for x in sells),
                       "how": ", ".join(sorted({re.sub(r"\s*\([+-]\)", "", x["method"]) for x in sells})),
                       "note": ("거래 예정 기간 " + min(x["start"] for x in sells) + " ~ " + max((x["end"] or x["start"]) for x in sells)
                                + (" · 목적: " + p["purpose"] if p.get("purpose") and p["purpose"] != "-" else "")),
                       "cancelled": bool(canc), "cancel_rcept": canc[0][1] if canc else None,
                       "cancel_reason": canc[0][3] if canc else None})
            items.append(it)
        elif k == "pledge":
            GENERIC = ("특수관계인", "최대주주", "본인", "기타", "대표이사")
            named = {c["debtor"] for c in p["contracts"] if is_person(c["debtor"]) and c["debtor"] not in GENERIC}
            generic = [c for c in p["contracts"] if c["debtor"] in GENERIC]
            if not named and is_person(p["pledgor"]):
                named = {p["pledgor"]}
            groups = {who: [c for c in p["contracts"] if c["debtor"] == who] for who in named}
            if generic:
                head = next(iter(sorted(named)), "") or re.sub(r"\s*외\s*\d*\s*인?$", "", p["pledgor"]).strip() or "최대주주"
                groups[f"{head} 특수관계인"] = generic
            single = len(groups) == 1
            for who, cts in groups.items():
                cts = cts or p["contracts"]
                new = [c for c in cts if p["contract_date"] and c.get("signed") == p["contract_date"]] or cts[:1]
                it = base_item(rn, d, "주식담보")
                ends = sorted(c["end"] for c in cts if c.get("end"))
                set_new = sum(c["amount"] or 0 for c in new)
                it.update({"person": who, "relation": p.get("debtor_relation") or "최대주주",
                           "shares": sum(c["shares"] or 0 for c in new),
                           "amount": (p.get("debt_total") if single and p.get("debt_total") else (sum(c["amount"] or 0 for c in cts) or None)),
                           "est": False, "event_date": p.get("contract_date") or ymd_dash(d["date"]),
                           "how": ", ".join(sorted({c["lender"] for c in new if c.get("lender")})),
                           "debt_total": p.get("debt_total"), "maturity": ends[0] if ends else None,
                           "amount_label": "차입 총액" if single and p.get("debt_total") else "담보설정액",
                           "note": (f"이번 담보설정 {C.eok(set_new)}" if set_new else "")
                                   + (f" · 가장 빠른 만기 {ends[0]}" if ends else "")
                                   + (f" · 목적 {new[0]['purpose']}" if new and new[0].get("purpose") else "")})
                items.append(it)
        elif k == "transfer":
            sellers = [h for h in p["holders"] if "변경전" in h["relation"] and is_person(h["name"])
                       and (h["before"] or 0) > (h["after"] or 0)]
            if not sellers and is_person(p["seller"]):
                sellers = [{"name": p["seller"], "relation": p["seller_rel"], "before": p["shares"], "after": 0}]
            for s in sellers:
                sh = (s["before"] or 0) - (s["after"] or 0)
                it = base_item(rn, d, "경영권 양도")
                amt = round(sh * p["price"]) if p.get("price") and sh else (p["amount"] if len(sellers) == 1 else None)
                it.update({"person": s["name"], "relation": s["relation"], "shares": sh, "amount": amt, "est": False,
                           "event_date": p.get("close_date") or p.get("contract_date") or d["date"],
                           "how": "양수인 " + (p.get("buyer") or "-"),
                           "note": (f"계약 전체 {C.eok(p['amount'])}" if p.get("amount") else "")
                                   + (f" · 대금 지급(변경 예정)일 {p['close_date']}" if p.get("close_date") else "")})
                items.append(it)
    for it in items:
        if re.fullmatch(r"(?:[가-힣]\s)+[가-힣]", it.get("person", "")):  # '최 태 원' → '최태원'
            it["person"] = it["person"].replace(" ", "")
        for k in ("event_date", "date_from", "date_to", "maturity"):
            v = it.get(k)
            if v and re.fullmatch(r"\d{8}", v):
                it[k] = f"{v[:4]}-{v[4:6]}-{v[6:]}"
    return items


def dividend_items(data: dict, today: date) -> list[dict]:
    """현금배당결정 × 최대주주 현황(직전 사업보고서) 개인 보유주식 → 수령 추정"""
    hy = C.load_json(HYSLR_PATH, {})
    out = []
    evs = [e for e in data.get("events", []) if e.get("category") == "배당" and e.get("corp_cls") in ("Y", "K")]
    for e in sorted(evs, key=lambda x: x["date"], reverse=True):
        det = e.get("detail") or {}
        if det.get("superseded_by") or det.get("old") or det.get("dup"):  # 이후 정정된 공시·과거분 정정은 제외
            continue
        dps = next((t[1] for t in det.get("texts", []) if t[0] == "주당배당금"), None)
        if not isinstance(dps, (int, float)) or dps <= 0:
            continue
        yr = int(e["date"][:4])
        cc = e["corp_code"]
        rows = None
        for y in (yr - 1, yr - 2):
            key = f"{cc}:{y}"
            if key not in hy:
                if STATS["hyslr_calls"] >= HCFG["max_hyslr_calls_per_run"]:
                    STATS["hyslr_deferred"] += 1
                    break
                try:
                    j = C.dart_get("hyslrSttus.json", corp_code=cc, bsns_year=str(y), reprt_code="11011")
                except (C.OutOfTime, C.DartError) as ex:
                    C.warn(f"최대주주 현황 조회 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
                    C.save_json(HYSLR_PATH, hy)
                    return out
                STATS["hyslr_calls"] += 1
                hy[key] = [{"nm": r.get("nm", ""), "relate": r.get("relate", ""), "kind": r.get("stock_knd", ""),
                            "qty": C.to_int(r.get("trmend_posesn_stock_co")), "rt": r.get("trmend_posesn_stock_qota_rt")}
                           for r in (j.get("list") or [])]
            if hy[key]:
                rows, used_year = hy[key], y
                break
        if not rows:
            continue
        dates = dict(det.get("dates") or [])
        total = det.get("amount")  # 배당총액
        implied = total / dps if total else None  # 배당 대상 주식수 (배당총액 ÷ 주당배당금)
        for r in rows:
            if "보통" not in r["kind"] or not r["qty"] or not is_person(r["nm"]) or r["nm"] in ("계", "합계"):
                continue
            if re.search(r"계열|법인|관계회사|재단|조합|펀드|공익|자회사|모회사", r.get("relate", "")):
                continue  # 관계가 회사·재단인 행
            qty, fix = r["qty"], ""
            try:
                rt = float(str(r["rt"]).replace(",", ""))
            except (TypeError, ValueError):
                rt = None
            # 검증 (배당 대상 주식수 = 배당총액 ÷ 주당배당금 기준)
            #  ① 한 사람 보유주식이 배당 대상 주식 전체보다 많음 → 공시 수치 오류 또는 차등배당 → 지분율로 계산, 확인 필요 표시
            #  ② 보유주식 비중이 지분율의 절반에도 못 미침 → 무상증자 등으로 주식수가 옛 기준 → 지분율로 계산
            #  (우선주·자사주가 있으면 비중이 지분율보다 커지는 게 정상이라 그 방향은 그대로 둠)
            if implied and rt and 0 < rt <= 100:
                share_pct = qty / implied * 100
                if qty > implied * 1.02 or share_pct < rt / 2 or share_pct > rt * 2.5:
                    DIV_ANOM.append({"corp": e["corp_name"], "rcept_no": e["rcept_no"], "dps": dps, "total": total,
                                     "qty": qty, "rt": r["rt"], "implied_shares": round(implied),
                                     "case": "over" if qty > implied * 1.02 else ("stale" if share_pct < rt / 2 else "inflated")})
                    new_qty = round(implied * rt / 100)
                    fix = (f" · 확인 필요: 공시상 보유 {r['qty']:,}주가 배당 대상 주식수({round(implied):,}주)보다 많아(공시 오류 또는 차등배당) 지분율 {rt}%로 계산"
                           if qty > implied * 1.02 else
                           f" · 공시상 보유 {r['qty']:,}주가 지분율 {rt}%와 맞지 않아 지분율로 계산 (원문 확인 권장)")
                    qty = new_qty
            elif implied and qty > implied * 1.02:
                DIV_ANOM.append({"corp": e["corp_name"], "rcept_no": e["rcept_no"], "dps": dps, "total": total,
                                 "qty": qty, "rt": r["rt"], "implied_shares": round(implied), "case": "drop"})
                continue
            amt = round(qty * dps)
            out.append({"id": f"{e['rcept_no']}-d-{r['nm']}", "cat": "배당 수령", "rcept_no": e["rcept_no"],
                        "date": e["date"], "corp_code": cc, "corp_name": e["corp_name"], "stock_code": e.get("stock_code", ""),
                        "corp_cls": e.get("corp_cls", ""), "person": r["nm"], "relation": r["relate"], "shares": qty,
                        "amount": amt, "est": True, "event_date": dates.get("지급예정일") or dates.get("기준일") or f"{e['date'][:4]}-{e['date'][4:6]}-{e['date'][6:]}",
                        "how": f"주당 {dps:,}원", "note": f"{used_year}년 사업보고서 기준 보유 {qty:,}주 ({r['rt']}%) × 주당 {dps:,}원 추정" + fix})
    C.save_json(HYSLR_PATH, hy)
    return out


def main() -> int:
    if not C.DART_KEY:
        print("DART_API_KEY 없음", file=sys.stderr)
        return 1
    today = C.now_kst().date()
    store = C.load_json(STORE_PATH, {"docs": {}}) if not CHECK else {"docs": {}}
    store.setdefault("docs", {})
    scan_lists(store, today)
    C.log(f"대주주 공시 대상 {len(store['docs']):,}건 (목록 {STATS['list_pages']}쪽)")
    parse_pending(store)
    if not CHECK:
        C.save_json(STORE_PATH, store)
    prices = load_prices()
    corp_map = C.load_json(C.CACHE_DIR / "corpcode.json", {}).get("map", {})
    try:
        items = build_items(store, prices, today, corp_map)
    except C.OutOfTime:
        items = []
    C.save_json(PRICE_PATH, prices)

    if CHECK:
        kinds = Counter(d["kind"] for d in store["docs"].values())
        causes = Counter()
        types = Counter()
        for d in store["docs"].values():
            if d["kind"] == "change" and d.get("parsed"):
                for pp in d["parsed"]["people"]:
                    types[pp["type"]] += 1
                    for ch in pp["changes"]:
                        causes[ch["cause"]] += 1
        samples = defaultdict(list)
        for rn, d in sorted(store["docs"].items(), reverse=True):
            if len(samples[d["kind"]]) < 8:
                samples[d["kind"]].append({"rcept_no": rn, **{k: v for k, v in d.items() if k != "_rows"},
                                           "rows": d.get("_rows", [])[:90]})
        empty = [{"rcept_no": rn, "kind": d["kind"], "corp": d["corp_name"], "rows": d.get("_rows", [])[:60]}
                 for rn, d in store["docs"].items()
                 if d.get("parsed") is not None and not any(
                     (d["parsed"].get(x) for x in ("people", "plans", "contracts", "holders", "starts")))][:15]
        C.save_json(CHECK_OUT, {"generated_at": C.now_kst().isoformat(timespec="minutes"), "stats": STATS,
                                "kinds": kinds, "change_causes": causes.most_common(), "person_types": types.most_common(),
                                "items_by_cat": Counter(i["cat"] for i in items),
                                "items": sorted(items, key=lambda x: x["date"], reverse=True)[:400],
                                "empty_parse": empty, "samples": samples,
                                "price_test": {"005930": close_on(prices, "005930", str(today - timedelta(days=3)), today),
                                               "sample": dict(list(prices.get("005930", {}).get("px", {}).items())[-3:])}})
        C.log(f"검증 파일 저장: 항목 {len(items)}개 {dict(Counter(i['cat'] for i in items))}")
        return 0

    data_path = C.OUT_PATH
    if not data_path.exists():
        C.warn("data.json 이 없어 대주주 항목을 붙이지 않았습니다 (수집 단계 실패)")
        return 0
    import json
    data = json.loads(data_path.read_text(encoding="utf-8"))
    try:
        items += dividend_items(data, today)
    except C.OutOfTime:
        C.warn("시간 부족으로 배당 수령 추정 일부 생략")
    items.sort(key=lambda x: (x["date"], x.get("amount") or 0), reverse=True)
    pend = sum(1 for d in store["docs"].values() if d.get("pv") != PARSE_VER)
    data["holders"] = {"items": items, "stats": {"docs": len(store["docs"]), "pending": pend,
                                                 "scan_from": store.get("scan", {}).get("from"),
                                                 "by_cat": Counter(i["cat"] for i in items)}}
    data_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    C.save_json(C.ROOT / "reports" / "holders_status.json", {   # 공개 저장소용: 건수만 (개인정보 없음)
        "generated_at": C.now_kst().isoformat(timespec="minutes"), "items": len(items),
        "by_cat": Counter(i["cat"] for i in items), "docs": len(store["docs"]), "pending": pend,
        "scan_from": store.get("scan", {}).get("from"), "scan_to": store.get("scan", {}).get("to"),
        "no_amount": sum(1 for i in items if not i.get("amount")),
        "over_1eok": Counter(i["cat"] for i in items if (i.get("amount") or 0) >= 1e8),
        "over_10eok": Counter(i["cat"] for i in items if (i.get("amount") or 0) >= 1e9),
        "max_by_cat": {c: max((i.get("amount") or 0) for i in items if i["cat"] == c) for c in {i["cat"] for i in items}},
        "dividend_top": [{"corp": i["corp_name"], "rcept_no": i["rcept_no"], "amount": i["amount"], "shares": i["shares"], "how": i["how"],
                          "note": re.sub(r"[가-힣]{2,4}\s*\(", "(", i.get("note", ""))[:160], "relation": i.get("relation")} for i in sorted((x for x in items if x["cat"] == "배당 수령"),
                                                                              key=lambda x: -(x.get("amount") or 0))[:15]],
        "dividend_anomalies": len(DIV_ANOM), "dividend_anomaly_samples": DIV_ANOM[:40], "stats": STATS})
    C.log(f"대주주 항목 {len(items):,}개 저장 (원문 대기 {pend:,}건, 목록 {store.get('scan', {}).get('from')}부터)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
