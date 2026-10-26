#!/usr/bin/env python3
"""
법인 자금 레이더 — 데이터 수집기

DART(공시·재무) + 네이버 뉴스 + 한국은행 ECOS(금리)를 모아
docs/data/data.json 하나로 만든다. 화면(docs/index.html)은 이 파일만 읽는다.

필요한 환경변수 (GitHub Secrets에 등록)
  DART_API_KEY          필수
  NAVER_CLIENT_ID       선택 (없으면 뉴스 생략)
  NAVER_CLIENT_SECRET   선택
  ECOS_API_KEY          선택 (없으면 금리 생략)
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
CACHE_DIR = ROOT / "cache"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.csv"
OUT_PATH = ROOT / "docs" / "data" / "data.json"

KST = timezone(timedelta(hours=9))
DART = "https://opendart.fss.or.kr/api"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

DART_KEY = os.environ.get("DART_API_KEY", "").strip()
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
ECOS_KEY = os.environ.get("ECOS_API_KEY", "").strip()

REPORT_LABEL = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업보고서"}
MARKET_LABEL = {"Y": "유가", "K": "코스닥", "N": "코넥스", "E": "기타"}

WARNINGS: list[str] = []
STATS: dict = {"dart_calls": 0, "dart_sec": 0.0, "list_pages": {}, "fin_calls": 0, "detail_filter": {}}

# 실행 시간 예산(분). 넘으면 그때까지 모은 것만 저장하고 정상 종료 → 다음 실행이 이어서 수집
_START = time.monotonic()
TIME_BUDGET_SEC = float(os.environ.get("RADAR_TIME_BUDGET_MIN") or 0) * 60


class OutOfTime(Exception):
    pass


def check_time() -> None:
    if TIME_BUDGET_SEC and time.monotonic() - _START > TIME_BUDGET_SEC:
        raise OutOfTime()


# ───────────────────────── 공통 유틸 ─────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)


IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def warn(msg: str) -> None:
    print(f"[경고] {msg}", file=sys.stderr, flush=True)
    if IN_ACTIONS:
        print(f"::warning title=수집 경고::{msg}", flush=True)
    WARNINGS.append(msg)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def now_kst() -> datetime:
    return datetime.now(KST)


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def norm(s: str) -> str:
    """공백·가운뎃점 제거 (보고서명 매칭용)"""
    return re.sub(r"[\s·ㆍ•∙]", "", s or "")


def to_int(s) -> int | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def clean_text(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


CFG = load_json(CONFIG_DIR / "config.json", {})
PRODUCTS = load_json(CONFIG_DIR / "products.json", {"segments": []})

_session = requests.Session()


def http_get(url, params=None, headers=None):
    """모든 외부 호출이 지나가는 단일 지점 (테스트에서 교체 가능)"""
    return _session.get(url, params=params, headers=headers, timeout=30)


# ───────────────────────── DART ─────────────────────────
class DartError(Exception):
    pass


def dart_get(endpoint: str, **params) -> dict:
    check_time()
    params["crtfc_key"] = DART_KEY
    last_err = None
    for attempt in range(3):
        try:
            t0 = time.monotonic()
            r = http_get(f"{DART}/{endpoint}", params=params)
            STATS["dart_calls"] += 1
            STATS["dart_sec"] += time.monotonic() - t0
            if endpoint.startswith("fnltt"):
                STATS["fin_calls"] += 1
            r.raise_for_status()
            j = r.json()
        except Exception as e:  # 네트워크·파싱 오류는 재시도
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue
        time.sleep(CFG.get("dart", {}).get("pause_sec", 0.15))
        st = j.get("status")
        if st in ("000", "013"):  # 정상 / 데이터 없음
            return j
        if st == "020":
            raise DartError("DART 일일 요청 한도 초과(020) — 다음 실행에서 이어서 수집됩니다.")
        if st == "012":
            raise DartError("DART 접근 불가 IP(012) — 실행 서버 IP가 차단됐을 수 있습니다.")
        if st in ("010", "011", "901"):
            raise DartError(f"DART 인증키 오류({st}): {j.get('message')}")
        if st == "800":
            raise DartError("DART 시스템 점검 중(800)")
        warn(f"DART {endpoint} 응답 {st}: {j.get('message')}")
        return j
    raise DartError(f"DART {endpoint} 호출 실패: {last_err}")


def load_corp_codes() -> dict:
    """법인명/종목코드 → 고유번호 매핑 (관심법인 매칭용, 7일 캐시)"""
    cache_path = CACHE_DIR / "corpcode.json"
    cached = load_json(cache_path, None)
    if cached:
        age = now_kst() - datetime.fromisoformat(cached["fetched_at"])
        if age.days < 7:
            return cached["map"]
    r = http_get(f"{DART}/corpCode.xml", params={"crtfc_key": DART_KEY})
    content = r.content
    if not content.startswith(b"PK"):
        msg = content[:200].decode("utf-8", "ignore")
        if cached:
            warn(f"고유번호 파일 갱신 실패, 이전 캐시 사용: {msg}")
            return cached["map"]
        raise DartError(f"고유번호 파일 다운로드 실패: {msg}")
    z = zipfile.ZipFile(io.BytesIO(content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        cc = (el.findtext("corp_code") or "").strip()
        if cc:
            m[cc] = {"name": (el.findtext("corp_name") or "").strip(),
                     "stock_code": (el.findtext("stock_code") or "").strip()}
    save_json(cache_path, {"fetched_at": now_kst().isoformat(), "map": m})
    log(f"고유번호 {len(m):,}건 갱신")
    return m


def classify(report_nm: str) -> dict | None:
    n = norm(report_nm)
    for kw in CFG["dart"].get("exclude_keywords", []):
        if norm(kw) in n:
            return None
    for rule in CFG["event_rules"]:
        if norm(rule["keyword"]) in n:
            return rule
    return None


def _fetch_list(bgn: date, end: date, ty: str, detail: str | None = None) -> list[dict]:
    rows, page = [], 1
    key = ty + (f"/{detail}" if detail else "")
    while True:
        params = dict(bgn_de=ymd(bgn), end_de=ymd(end), pblntf_ty=ty, last_reprt_at="Y",
                      page_no=page, page_count=100)
        if detail:
            params["pblntf_detail_ty"] = detail
        j = dart_get("list.json", **params)
        STATS["list_pages"][key] = STATS["list_pages"].get(key, 0) + 1
        rows.extend(j.get("list") or [])
        if page >= int(j.get("total_page") or 1):
            return rows
        page += 1


ACTIVE_DETAIL: dict[str, list[str]] = {}  # 검증을 통과한 상세유형 필터만 여기에 들어감


def verify_detail_filters() -> None:
    """config의 detail_filter(예: 거래소공시 I → I001만)를 최근 며칠 실제 데이터로 검증.
    전체 조회에서 잡힌 자금 이벤트가 필터 조회에도 전부 있을 때만 적용, 아니면 전체 조회 유지."""
    filters = CFG["dart"].get("detail_filter") or {}
    if not filters:
        return
    today = now_kst().date()
    a = today - timedelta(days=CFG["dart"].get("detail_verify_days", 7) - 1)
    for ty, details in filters.items():
        full = {r["rcept_no"] for r in _fetch_list(a, today, ty) if classify(r.get("report_nm", ""))}
        filt = set()
        n_rows = 0
        for d in details:
            rows = _fetch_list(a, today, ty, d)
            n_rows += len(rows)
            filt |= {r["rcept_no"] for r in rows if classify(r.get("report_nm", ""))}
        result = {"검증기간": f"{a}~{today}", "전체조회_이벤트": len(full), "필터조회_이벤트": len(full & filt),
                  "필터조회_전체행": n_rows}
        if full and full <= filt:
            ACTIVE_DETAIL[ty] = details
            result["적용"] = True
            log(f"상세유형 필터 {ty}→{details} 검증 통과 (이벤트 {len(full)}건 모두 포함) — 적용")
        else:
            result["적용"] = False
            reason = "검증할 이벤트가 없음" if not full else f"{len(full - filt)}건 누락"
            warn(f"상세유형 필터 {ty}→{details} 미적용 ({reason}) — 전체 조회 유지")
        STATS["detail_filter"][ty] = result


def fetch_disclosures(bgn: date, end: date) -> list[dict]:
    rows = []
    for ty in CFG["dart"]["pblntf_types"]:
        if ty in ACTIVE_DETAIL:
            for d in ACTIVE_DETAIL[ty]:
                rows.extend(_fetch_list(bgn, end, ty, d))
        else:
            rows.extend(_fetch_list(bgn, end, ty))
    return rows


def _event_from_row(row: dict, rule: dict) -> dict:
    rn = row["rcept_no"]
    return {
        "rcept_no": rn,
        "date": row.get("rcept_dt", ""),
        "corp_code": row.get("corp_code", ""),
        "corp_name": row.get("corp_name", ""),
        "stock_code": (row.get("stock_code") or "").strip(),
        "corp_cls": row.get("corp_cls", ""),
        "report_nm": row.get("report_nm", "").strip(),
        "type": rule["type"],
        "category": rule["category"],
        "hint": rule.get("hint", ""),
        "correction": "정정" in (row.get("report_nm") or ""),
        "url": DART_VIEW + rn,
    }


EVENTS_PATH_NAME = "events.json"


def load_event_store() -> dict:
    store = load_json(CACHE_DIR / EVENTS_PATH_NAME, {})
    store.setdefault("events", {})
    if "last_end" in store and not store.get("newest"):  # 구버전 캐시 호환
        store["newest"] = store.pop("last_end")
        dates = [e["date"] for e in store["events"].values()]
        store["oldest"] = min(dates) if dates else store["newest"]
    store.setdefault("newest", None)
    store.setdefault("oldest", None)
    # 설정(event_rules)이 바뀌면 캐시된 공시에도 반영: 재분류, 규칙에서 빠진 건 제거
    for rn in list(store["events"]):
        ev = store["events"][rn]
        rule = classify(ev["report_nm"])
        if not rule:
            del store["events"][rn]
            continue
        ev.update(type=rule["type"], category=rule["category"], hint=rule.get("hint", ""))
    return store


def update_events() -> tuple[list[dict], int]:
    """자금 이벤트 공시 수집.
    - 최근 → 첫 실행은 initial_days 일치만, 이후 실행은 마지막 수집일 3일 전부터 (정정 반영)
    - 과거 → 실행마다 backfill_days_per_run 일씩 거슬러 올라가 lookback_days 까지 채움
    - 7일 단위로 진행 상황을 기록하므로 중간에 멈춰도 다음 실행이 이어서 수집
    반환: (이벤트 목록 최신순, 실제 수집 범위 일수)
    """
    dc = CFG["dart"]
    store = load_event_store()
    today = now_kst().date()
    floor = today - timedelta(days=CFG["lookback_days"] - 1)
    added = 0
    path = CACHE_DIR / EVENTS_PATH_NAME

    def ingest(a: date, b: date) -> None:
        nonlocal added
        log(f"공시 목록 조회 {a} ~ {b}")
        for row in fetch_disclosures(a, b):
            rule = classify(row.get("report_nm", ""))
            if rule:
                added += row["rcept_no"] not in store["events"]
                store["events"][row["rcept_no"]] = _event_from_row(row, rule)

    # 분류 규칙(event_rules·exclude_keywords)이 바뀌면 이미 훑은 기간도 다시 훑어야 새 규칙에 맞는 과거 공시가 잡힌다
    import hashlib
    rules_hash = hashlib.md5(json.dumps([CFG["event_rules"], dc.get("exclude_keywords", []), dc["pblntf_types"]],
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    if store["newest"] and store.get("rules_hash") != rules_hash:
        store["rescan_from"] = store.get("oldest") or ymd(floor)
        log(f"분류 규칙 변경 감지 — {store['rescan_from']}부터 공시 목록 다시 조회")
    store["rules_hash"] = rules_hash

    try:
        # ⓪ 규칙 변경 후 재조회 (중간에 멈추면 다음 실행이 이어서)
        if store.get("rescan_from"):
            cur = max(floor, parse_ymd(store["rescan_from"]))
            stop = parse_ymd(store["newest"])
            while cur <= stop:
                b = min(cur + timedelta(days=6), stop)
                ingest(cur, b)
                cur = b + timedelta(days=1)
                store["rescan_from"] = ymd(cur)
                save_json(path, store)
            store.pop("rescan_from", None)
            save_json(path, store)

        # ① 최근 구간 (과거 → 오늘 방향)
        first_run = not store["newest"]
        if not first_run:
            cur = max(floor, parse_ymd(store["newest"]) - timedelta(days=3))
        else:
            cur = max(floor, today - timedelta(days=dc["initial_days"] - 1))
            store["oldest"] = ymd(cur)
        while cur <= today:
            b = min(cur + timedelta(days=6), today)
            ingest(cur, b)
            store["newest"] = ymd(b)
            save_json(path, store)
            cur = b + timedelta(days=1)

        # ② 과거 채우기 (오늘 → 과거 방향) — 첫 실행은 가볍게 끝내기 위해 생략
        oldest = max(parse_ymd(store["oldest"]), floor)
        target = oldest if first_run else max(floor, oldest - timedelta(days=dc["backfill_days_per_run"]))
        while oldest > target:
            a = max(target, oldest - timedelta(days=7))
            ingest(a, oldest - timedelta(days=1))
            oldest = a
            store["oldest"] = ymd(oldest)
            save_json(path, store)
    except OutOfTime:
        warn("시간 제한으로 공시 수집을 중간에 멈췄습니다 — 다음 실행에서 이어서 수집합니다.")
    finally:
        if store.get("oldest") and parse_ymd(store["oldest"]) < floor:
            store["oldest"] = ymd(floor)
        store["events"] = {k: v for k, v in store["events"].items() if v["date"] >= ymd(floor)}
        save_json(path, store)

    coverage = (today - parse_ymd(store["oldest"])).days + 1 if store.get("oldest") and store.get("newest") else 0
    if coverage < CFG["lookback_days"]:
        warn(f"공시는 최근 {coverage}일치까지 수집됨 — 실행마다 {dc['backfill_days_per_run']}일씩 늘려 "
             f"{CFG['lookback_days']}일까지 채웁니다.")
    log(f"자금 이벤트 {len(store['events']):,}건 (신규 {added:,}건, 범위 {coverage}일)")
    events = sorted(store["events"].values(), key=lambda e: (e["date"], e["rcept_no"]), reverse=True)
    return events, coverage


def load_watchlist(corp_map: dict) -> dict:
    """watchlist.csv 의 법인명 또는 종목코드 → 고유번호"""
    if not WATCHLIST_PATH.exists():
        return {}
    by_stock = {v["stock_code"]: k for k, v in corp_map.items() if v["stock_code"]}
    by_name: dict[str, list[str]] = {}
    for k, v in corp_map.items():
        by_name.setdefault(norm(v["name"]), []).append(k)
    out = {}
    with WATCHLIST_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row.get("name_or_code") or "").strip()
            if not key:
                continue
            cc = None
            if key.isdigit() and len(key) == 6:
                cc = by_stock.get(key)
            elif key.isdigit() and len(key) == 8:
                cc = key if key in corp_map else None
            else:
                cands = by_name.get(norm(key), [])
                listed = [c for c in cands if corp_map[c]["stock_code"]]
                cc = (listed or cands or [None])[0]
                if len(cands) > 1:
                    warn(f"관심법인 '{key}' 동명 법인 {len(cands)}개 — 종목코드로 지정 권장")
            if cc:
                out[cc] = row.get("memo", "")
            else:
                warn(f"관심법인 '{key}' 을(를) DART에서 찾지 못했습니다.")
    return out


def build_universe(events: list[dict], corp_map: dict, watch: dict) -> dict:
    allowed = set(CFG["universe"]["markets"])
    u: dict[str, dict] = {}
    for cc, memo in watch.items():
        info = corp_map.get(cc, {})
        u[cc] = {"corp_code": cc, "name": info.get("name", cc), "stock_code": info.get("stock_code", ""),
                 "market": "", "watch": True, "memo": memo}
    for ev in events:  # 최신순
        cc = ev["corp_code"]
        if cc in u:
            u[cc]["market"] = u[cc]["market"] or ev["corp_cls"]
            continue
        if ev["corp_cls"] in allowed:
            u[cc] = {"corp_code": cc, "name": ev["corp_name"], "stock_code": ev["stock_code"],
                     "market": ev["corp_cls"], "watch": False, "memo": ""}
    return u


def parse_financials(rows: list[dict]) -> dict:
    """전체 재무제표 행 → 필요한 계정만 추출 (원 단위)"""
    f = {"cash": None, "cash_prev": None, "st_fin": None, "assets": None, "liabilities": None,
         "borrowings": None, "ocf": None, "revenue": None}
    borrow_total = 0
    borrow_found = False
    borrow_excl = ("할인", "상환", "전환권", "이자", "미지급", "발행차금", "충당")
    alt = None          # 금융회사: 현금및예치금
    financial = False   # 은행·증권·보험·카드·캐피탈·금융지주 (영업상 예수·대출·보험 부채 보유)
    fin_markers = ("예수부채", "고객예탁금", "대출채권", "보험계약부채", "보험계약마진", "책임준비금", "투자계약부채")
    for r in rows:
        sj = r.get("sj_div", "")
        aid = r.get("account_id", "") or ""
        nm = norm(r.get("account_nm", ""))
        cur = to_int(r.get("thstrm_amount"))
        if sj == "BS":
            if f["cash"] is None and (aid == "ifrs-full_CashAndCashEquivalents" or nm == "현금및현금성자산"):
                f["cash"] = cur
                f["cash_prev"] = to_int(r.get("frmtrm_amount"))
            elif alt is None and re.match(r"현금", re.sub(r"^[ⅠⅡⅢⅣIVX0-9.\-]+", "", nm)) and ("예치금" in nm or nm.endswith("현금및예금")):
                # '현금및예치금', 'Ⅰ.현금및예치금', '현금및상각후원가측정예치금' 등
                alt = (cur, to_int(r.get("frmtrm_amount")))
            elif f["st_fin"] is None and nm in ("단기금융상품", "단기금융자산"):
                f["st_fin"] = cur
            elif f["assets"] is None and (aid == "ifrs-full_Assets" or nm == "자산총계"):
                f["assets"] = cur
            elif f["liabilities"] is None and (aid == "ifrs-full_Liabilities" or nm == "부채총계"):
                f["liabilities"] = cur
            elif any(k in nm for k in fin_markers):
                financial = True
            elif ("차입금" in nm or "사채" in nm) and not any(x in nm for x in borrow_excl):
                if cur is not None:
                    borrow_total += cur
                    borrow_found = True
        elif sj == "CF":
            if f["ocf"] is None and (aid == "ifrs-full_CashFlowsFromUsedInOperatingActivities"
                                     or ("영업활동" in nm and "현금흐름" in nm)):
                f["ocf"] = cur
        elif sj in ("IS", "CIS"):
            if f["revenue"] is None and (aid == "ifrs-full_Revenue" or nm in ("매출액", "수익(매출액)", "영업수익")):
                f["revenue"] = to_int(r.get("thstrm_add_amount")) or cur
    f["borrowings"] = borrow_total if borrow_found else 0
    if f["cash"] is None and not alt:  # 진단용: 현금 계정 후보 이름 기록
        f["cash_candidates"] = sorted({r.get("account_nm", "") for r in rows
                                       if r.get("sj_div") == "BS" and "현금" in r.get("account_nm", "")})[:8]
    if f["cash"] is None and alt:
        f["cash"], f["cash_prev"] = alt
        f["cash_label"] = "현금및예치금"
        financial = True
    if financial:
        f["financial"] = True
    if rows and rows[0].get("rcept_no"):
        f["rcept_no"] = rows[0]["rcept_no"]
    f["v"] = FIN_VER
    return f


FIN_VER = "fin7"  # 2026-10-22 별도 기준 시험 적용 후 연결 기준으로 원복 → 전체 재수집
FIN_INDUSTRY = ("641", "649", "651", "652", "653", "661", "662")  # 은행·여신·보험·증권 등 (지주회사 64992는 계정으로 판별)


def report_candidates(today: date) -> list[tuple[int, str]]:
    """최신 보고서부터. 기간 종료 후 일정 기간이 지나지 않은 보고서는 아직 없으므로 조회 생략."""
    y = today.year
    periods = [(y, "11014", date(y, 9, 30), 30), (y, "11012", date(y, 6, 30), 30),
               (y, "11013", date(y, 3, 31), 30), (y - 1, "11011", date(y - 1, 12, 31), 45),
               (y - 1, "11014", date(y - 1, 9, 30), 30)]
    return [(yy, code) for yy, code, end, lag in periods if today >= end + timedelta(days=lag)]


def fetch_latest_fin(corp_code: str) -> dict | None:
    for year, code in report_candidates(now_kst().date()):
        for fs in ("CFS", "OFS"):  # 연결 우선, 없으면 별도
            j = dart_get("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=str(year),
                         reprt_code=code, fs_div=fs)
            if j.get("status") == "000" and j.get("list"):
                f = parse_financials(j["list"])
                if f["cash"] is None and f["assets"] is None:
                    continue
                f["period"] = f"{year} {REPORT_LABEL[code]}"
                f["fs"] = "연결" if fs == "CFS" else "별도"
                return f
    return None


def update_financials(universe: dict, infos: dict | None = None) -> dict:
    cache_path = CACHE_DIR / "financials.json"
    cache = load_json(cache_path, {})
    ttl = CFG["financials"]["refresh_days"]
    cap = CFG["financials"]["max_companies_per_run"]
    order = sorted(universe.values(), key=lambda c: not c["watch"])  # 관심법인 먼저
    fetched = skipped = 0
    now = now_kst()
    try:
        for c in order:
            cc = c["corp_code"]
            hit = cache.get(cc)
            ind = ((infos or {}).get(cc) or {}).get("induty_code", "")
            stale_parse = bool(hit and hit.get("fin") and hit["fin"].get("v") != FIN_VER)
            if hit and not stale_parse and (now - datetime.fromisoformat(hit["fetched_at"])).days < ttl:
                continue
            if fetched >= cap:
                skipped += 1
                continue
            cache[cc] = {"fetched_at": now.isoformat(), "fin": fetch_latest_fin(cc)}
            fetched += 1
            if fetched % 50 == 0:
                log(f"재무 수집 {fetched}개사…")
                save_json(cache_path, cache)
    except OutOfTime:
        warn("시간 제한으로 재무 수집을 중간에 멈췄습니다 — 다음 실행에서 이어서 수집합니다.")
    except DartError as e:
        warn(f"재무 수집 중단: {e}")
    save_json(cache_path, cache)
    if skipped:
        warn(f"재무 미수집 {skipped}개사 — 다음 실행에서 이어서 수집합니다.")
    log(f"재무 신규 수집 {fetched}개사")
    return {cc: (cache.get(cc) or {}).get("fin") for cc in universe}



# ───────────────────────── 공시 원문 세부내용 (금액·일정) ─────────────────────────
# 목록 API에는 금액·납입일이 없어 공시 원문(document.xml)의 표에서 항목명으로 값을 찾는다.
# 항목명은 config.json 의 detail_specs 에서 관리 (정규식, 공백 제거한 항목명 기준).

TABLE_RE = re.compile(r"<TABLE\b.*?</TABLE>", re.S | re.I)
TR_RE = re.compile(r"<TR\b.*?</TR>", re.S | re.I)
CELL_RE = re.compile(r"<(TD|TH|TE|TU)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
DATE_RE = re.compile(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")


def _cell_text(h: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def doc_tables(text: str) -> tuple[list[list[str]], list[list[str]]]:
    """원문 XML → (본문 표 행들, 정정 대조표의 [항목, 정정후 값] 행들).
    정정공시의 '정정전/정정후' 대조표는 본문에서 빼서 정정 전 값이 잡히지 않게 하고, 정정후 값만 따로 모은다."""
    rows, corr = [], []
    for t in TABLE_RE.findall(text):
        trs = [[_cell_text(m[1]) for m in CELL_RE.findall(tr)] for tr in TR_RE.findall(t)]
        trs = [r for r in trs if any(r)]
        flat = norm(" ".join(" ".join(r) for r in trs))
        if "정정전" in flat and "정정후" in flat:
            after = None
            for r in trs:
                nr = [norm(c) for c in r]
                if "정정후" in nr:
                    after = nr.index("정정후") - len(nr)  # 오른쪽 기준 위치 (항목 셀 병합 대비)
                    continue
                if after is not None and len(r) >= 2 and len(r) >= -after:
                    label = " ".join(c for c in r[: len(r) + after - 1] if c)
                    corr.append([label, r[after]])
            continue
        rows.extend(trs)
    return rows, corr


def doc_rows(text: str) -> list[list[str]]:
    return doc_tables(text)[0]


def parse_amount(s: str) -> int | None:
    if not s or DATE_RE.search(s):
        return None
    m = re.search(r"-?\d[\d,]*", s.replace(" ", ""))
    if not m:
        return None
    try:
        v = int(m.group().replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def parse_date(s: str) -> str | None:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def find_value(rows: list[list[str]], label_re: str, kind: str):
    """항목명이 있는 셀 '뒤쪽' 셀에서 값(금액/날짜/글자)을 찾는다. 첫 번째로 찾은 값을 반환."""
    rx = re.compile(label_re)
    for cells in rows:
        for j, c in enumerate(cells):
            if not rx.search(norm(c)):
                continue
            for v in cells[j + 1:]:
                if kind == "amount":
                    r = parse_amount(v)
                elif kind == "date":
                    r = parse_date(v)
                else:
                    r = v if v and v not in ("-", "해당사항없음") and not rx.search(norm(v)) else None
                if r is not None:
                    return r
            break
    return None


def extract_detail(rows: list[list[str]], spec: dict, corr: list[list[str]] | None = None) -> dict:
    if corr and spec.get("use_correction"):
        rows = corr + rows  # 발행조건확정 등: 정정후 값이 확정치
    d = {"amount": None, "amount_label": spec.get("amount_label", "금액"), "dates": [], "texts": []}
    ff = find_value(rows, r"최초제출일", "date")
    if ff:
        d["first_filed"] = ff
    if spec.get("amount_sum"):
        vals = [find_value(rows, rx, "amount") for rx in spec["amount_sum"]]
        vals = [v for v in vals if v]
        d["amount"] = sum(vals) if vals else None
    for rx in spec.get("amount", []):
        if d["amount"] is None:
            d["amount"] = find_value(rows, rx, "amount")
    for label, rx in spec.get("dates", []):
        v = find_value(rows, rx, "date")
        if v:
            d["dates"].append([label, v])
    for t in spec.get("texts", []):
        label, rx, kind = t[:3]
        maxlen = t[3] if len(t) > 3 else 40
        v = find_value(rows, rx, kind)
        if v is not None:
            d["texts"].append([label, v if kind != "text" else str(v)[:maxlen]])
    uses = [[label, find_value(rows, rx, "amount")] for label, rx in spec.get("uses", [])]
    if any(v for _, v in uses):
        d["uses"] = [[k, v] for k, v in uses if v]
    ct = parse_contact(rows)
    if ct:
        d["contact"] = ct
    return d


CONTACT_RX = re.compile(r"작\s*성\s*책\s*임\s*자|공\s*시\s*책\s*임\s*자|공\s*시\s*담\s*당\s*자")


def parse_contact(rows: list[list[str]]) -> dict | None:
    """공시 표지의 작성책임자(직책·성명·전화). 확인된 형식:
    '(직 책) 전략기획실장 (성 명) 강동혁' / '(직 책) 부사장 | (성 명) 이 상 호' / '이 사 | 황 욱'(표시 없음), 전화는 다음 줄"""
    def squash(x):
        x = re.sub(r"\s+", " ", (x or "").strip(" :()|"))
        return x.replace(" ", "") if re.fullmatch(r"(?:[가-힣] )+[가-힣]", x) else x
    for i, cells in enumerate(rows):
        if not CONTACT_RX.search(" ".join(cells)):
            continue
        rest = [c for c in cells if c.strip() and not CONTACT_RX.search(c)]
        line = re.sub(r"\s+", " ", " ".join(rest))
        m_t = re.search(r"직\s*책\s*\)?\s*:?\s*(.*?)\s*(?:\(\s*성\s*명|성\s*명\s*\)|$)", line)
        m_n = re.search(r"성\s*명\s*\)?\s*:?\s*([^()]*?)\s*(?:\(|전\s*화|$)", line)
        title, name = (squash(m_t.group(1)) if m_t else None), (squash(m_n.group(1)) if m_n else None)
        if not (m_t or m_n) and len(rest) >= 2 and not re.search(r"\d{3}", rest[0] + rest[1]):
            title, name = squash(rest[0]), squash(rest[1])
        near = re.sub(r"\s+", " ", " ".join(" ".join(r) for r in rows[i:i + 3]))
        m_tel = re.search(r"전\s*화\s*(?:번\s*호)?\s*\)?\s*:?\s*\(?(0\d{1,2})\)?\s*[-.]?\s*(\d{3,4})\s*[-.]?\s*(\d{4})(?!\d)", near)
        tel = "-".join(m_tel.groups()) if m_tel else None
        if name and (len(name) > 20 or re.search(r"\d", name)):
            name = None
        if name or tel:
            return {"title": title or None, "name": name or None, "phone": tel}
    return None


def dart_document(rcept_no: str) -> tuple[str | None, str]:
    """공시 원문 본문 텍스트. 반환 (텍스트, 오류메시지)"""
    check_time()
    t0 = time.monotonic()
    try:
        r = http_get(f"{DART}/document.xml", params={"crtfc_key": DART_KEY, "rcept_no": rcept_no})
        c = r.content
    except Exception as e:
        return None, f"요청 오류: {e}"
    finally:
        STATS["dart_calls"] += 1
        STATS["dart_sec"] += time.monotonic() - t0
        STATS["doc_calls"] = STATS.get("doc_calls", 0) + 1
        time.sleep(CFG.get("dart", {}).get("pause_sec", 0.15))
    if not c.startswith(b"PK"):
        head = c[:300].decode("utf-8", "ignore")
        if "<status>020</status>" in head or '"020"' in head:
            raise DartError("DART 일일 요청 한도 초과(020)")
        return None, "응답 오류: " + re.sub(r"\s+", " ", head)[:120]
    try:
        z = zipfile.ZipFile(io.BytesIO(c))
        names = z.namelist()
        main_name = next((n for n in names if n.startswith(rcept_no)), names[0])
        raw = z.read(main_name)
    except Exception as e:
        return None, f"압축 해제 오류: {e}"
    if len(raw) > CFG["details"]["max_doc_mb"] * 1024 * 1024:
        return None, f"원문이 너무 큼 ({len(raw) // 1024 // 1024}MB)"
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc), ""
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore"), ""


def _probe_rows(rows: list[list[str]], spec: dict) -> list[list[str]]:
    """검증용: 앞부분 40행 + 규칙 항목명이 들어간 행과 그 다음 행"""
    rxs = [re.compile(x) for x in spec.get("amount_sum", []) + spec.get("amount", [])]
    rxs += [re.compile(r) for _, r in spec.get("dates", [])] + [re.compile(t[1]) for t in spec.get("texts", [])]
    keep = set(range(min(40, len(rows))))
    for i, cells in enumerate(rows):
        if any(rx.search(norm(" ".join(cells))) for rx in rxs):
            keep |= {i, i + 1}
    return [rows[i][:8] for i in sorted(keep) if i < len(rows)][:120]


def _anomaly(e: dict, det: dict) -> str | None:
    if "err" in det:
        return None
    pub = f"{e['date'][:4]}-{e['date'][4:6]}-{e['date'][6:]}"
    a = det.get("amount")
    if a is not None and a < 1e8 and e["category"] != "배당":
        return "금액 1억 미만"
    if a is not None and a > 2e13:
        return "금액 20조 초과"
    if a is None:
        return "금액 없음"
    old = (date.fromisoformat(pub) - timedelta(days=60)).isoformat()
    if det.get("old"):
        return None
    if any(v < old for k, v in det.get("dates", []) if "종료" not in k and "만기" not in k and "기준" not in k):
        return "일정이 공시일보다 60일 이상 과거"
    return None


RULES_VER = ""
CONTACT_VER = "ct2"


def update_details(events: list[dict]) -> dict:
    """이벤트별 금액·일정을 원문에서 추출해 event['detail']에 붙인다. 결과는 캐시(재수집 안 함)."""
    global RULES_VER
    dc = CFG["details"]
    specs = dc["specs"]
    # 유형별 추출 규칙이 바뀌면 그 유형의 캐시만 다시 추출 (다른 유형은 재다운로드 안 함)
    import hashlib
    meta = ("key_date", "merge_filings_days", "chain_lookup")
    VER = {c: "c" + hashlib.md5((json.dumps({k: v for k, v in sp.items() if k not in meta}, sort_keys=True,
                                             ensure_ascii=False) + "|parser-3").encode()).hexdigest()[:8]
           for c, sp in specs.items()}
    RULES_VER = "chain-1"
    path = CACHE_DIR / "details.json"
    cache = load_json(path, {})
    # 이전 방식(전체 규칙 하나의 버전) 캐시 이전: 규칙이 그대로인 유형은 유지
    legacy_ok = set(dc.get("legacy_ok_categories", []))
    cat_of = {e["rcept_no"]: e["category"] for e in events}
    for rn, d in cache.items():
        if d.get("v") == "a45a2344" and cat_of.get(rn) in legacy_ok:
            d["v"] = VER[cat_of[rn]]
    probe: dict[str, list] = {}
    fetched = pending = 0
    try:
        for e in events:  # 최신순
            spec = specs.get(e["category"])
            if not spec:
                continue
            rn = e["rcept_no"]
            if rn in cache and cache[rn].get("v") == VER[e["category"]]:
                c0 = cache[rn]
                # 원문 조회 오류(공시 직후 DART에 파일이 아직 없음 014 등)는 영구 저장하지 않고 다음 실행에서 다시 시도 (최대 6회)
                if not (c0.get("err") and "너무 큼" not in c0["err"] and c0.get("tries", 1) < 6):
                    continue
                STATS["detail_retry"] = STATS.get("detail_retry", 0) + 1
            if fetched >= dc["max_docs_per_run"]:
                pending += 1
                continue
            text, err = dart_document(rn)
            fetched += 1
            if text is None:
                cache[rn] = {"err": err, "v": VER[e["category"]], "tries": (cache.get(rn) or {}).get("tries", 0) + 1}
                continue
            rows, corr = doc_tables(text)
            det = extract_detail(rows, spec, corr)
            det["v"] = VER[e["category"]]
            cache[rn] = det
            if dc.get("probe"):
                ck = "작성책임자: " + ("찾음" if det.get("contact") else "못찾음") + " / " + e["report_nm"][:12]
                if len(probe.setdefault(ck, [])) < 3:
                    hit_rows = [r for r in rows if CONTACT_RX.search(" ".join(r))]
                    probe[ck].append({"rcept_no": rn, "corp_name": e["corp_name"], "contact": det.get("contact"),
                                      "rows": [r[:6] for r in (hit_rows or rows[:15])][:8]})
            if dc.get("probe") and len(probe.setdefault(e["category"], [])) < dc.get("probe_per_category", 2):
                probe[e["category"]].append({"rcept_no": rn, "report_nm": e["report_nm"], "corp_name": e["corp_name"],
                                             "date": e["date"], "extracted": det, "rows": _probe_rows(rows, spec),
                                             "corr_rows": corr[:40]})
            if fetched % 100 == 0:
                log(f"공시 원문 {fetched}건 처리…")
                save_json(path, cache)
    except OutOfTime:
        warn("시간 제한으로 공시 세부내용 수집을 중간에 멈췄습니다 — 다음 실행에서 이어서 수집합니다.")
    except DartError as ex:
        warn(f"공시 세부내용 수집 중단: {ex}")
    if dc.get("probe"):
        reprobe = 0
        per: dict[str, int] = {}
        try:
            for e in events:
                det = cache.get(e["rcept_no"])
                spec = specs.get(e["category"])
                if not det or not spec:
                    continue
                why = _anomaly(e, det)
                if not why:
                    continue
                if e["report_nm"].startswith("[발행조건확정]"):
                    continue  # 금리만 정정된 신고서는 진단 가치가 낮음
                key = f"{e['category']} | {why}"
                if per.get(key, 0) >= dc.get("probe_anomaly_per_type", 3) or reprobe >= dc.get("probe_anomaly_max", 45):
                    continue
                text, err = dart_document(e["rcept_no"])
                reprobe += 1
                if text is None:
                    continue
                per[key] = per.get(key, 0) + 1
                prow, pcorr = doc_tables(text)
                probe.setdefault("이상치: " + key, []).append(
                    {"rcept_no": e["rcept_no"], "report_nm": e["report_nm"], "corp_name": e["corp_name"],
                     "date": e["date"], "extracted": det, "rows": _probe_rows(prow, spec), "corr_rows": pcorr[:40]})
        except (OutOfTime, DartError):
            pass
        log(f"이상치 원문 재조회 {reprobe}건")
    save_json(path, cache)
    if pending:
        warn(f"공시 세부내용 미수집 {pending}건 — 다음 실행에서 이어서 수집합니다.")

    # 목록은 '최종보고서만' 받으므로, 금리만 정정된 최종 신고서([발행조건확정] 등)는 금액·납입일이 없다.
    # chain_lookup 규칙이 있는 카테고리는 같은 회사의 이전 신고서를 찾아 원문에서 보완한다.
    chain_n = 0
    try:
        for e in events:
            spec = specs.get(e["category"]) or {}
            det = cache.get(e["rcept_no"])
            if not spec.get("chain_lookup") or not det or "err" in det or det.get("amount"):
                continue
            if det.get("chain_checked") == RULES_VER:
                continue
            pub = parse_ymd(e["date"])
            j = dart_get("list.json", corp_code=e["corp_code"], bgn_de=ymd(pub - timedelta(days=60)), end_de=ymd(pub),
                         pblntf_ty="C", last_reprt_at="N", page_no=1, page_count=100)
            prev = [r for r in (j.get("list") or [])
                    if r["rcept_no"] != e["rcept_no"] and norm(spec["chain_lookup"]) in norm(r.get("report_nm", ""))
                    and not r.get("report_nm", "").startswith("[발행조건확정]")]
            prev.sort(key=lambda r: r["rcept_no"], reverse=True)  # 최근 신고서부터 (수요예측 정정 → 최초)
            for r in prev[:3]:
                text, err = dart_document(r["rcept_no"])
                if text is None:
                    continue
                rows, corr = doc_tables(text)
                pdet = extract_detail(rows, spec, corr)
                if dc.get("probe") and len(probe.setdefault("이전 신고서: " + e["category"], [])) < 4:
                    probe["이전 신고서: " + e["category"]].append(
                        {"rcept_no": r["rcept_no"], "report_nm": r["report_nm"], "corp_name": e["corp_name"],
                         "extracted": pdet, "rows": _probe_rows(rows, spec), "corr_rows": corr[:40]})
                if pdet.get("amount"):
                    det["amount"] = pdet["amount"]
                    have = dict(det.get("dates", []))
                    for k, v in pdet.get("dates", []):
                        have.setdefault(k, v)
                    det["dates"] = [[k, v] for k, v in have.items()]
                    det["from_rcept_no"] = r["rcept_no"]
                    chain_n += 1
                    break
            det["chain_checked"] = RULES_VER
    except (OutOfTime, DartError) as ex:
        warn(f"이전 신고서 보완 중단: {ex}")
    save_json(path, cache)
    if chain_n:
        log(f"이전 신고서에서 금액·일정 보완 {chain_n}건")

    # 같은 회사의 연속 신고서(최초 → 수요예측 정정 → 발행조건확정) 묶기:
    # 빠진 금액·일정은 묶음 안의 다른 신고서 값으로 채우고, 묶음당 최신 1건만 대표(dup=False)로 둔다.
    merge_cats = {c for c, sp in specs.items() if sp.get("merge_filings_days")}
    groups: dict[tuple, list] = {}
    for e in sorted(events, key=lambda x: x["date"]):
        if e["category"] not in merge_cats or "err" in cache.get(e["rcept_no"], {"err": 1}):
            continue
        win = specs[e["category"]]["merge_filings_days"]
        key = (e["corp_code"], e["category"])
        g = groups.setdefault(key, [])
        if g and (date.fromisoformat(f"{e['date'][:4]}-{e['date'][4:6]}-{e['date'][6:]}")
                  - date.fromisoformat(f"{g[-1][0]['date'][:4]}-{g[-1][0]['date'][4:6]}-{g[-1][0]['date'][6:]}")).days <= win:
            g[-1].append(e)
        else:
            g.append([e])
    merged_n = 0
    for chains in groups.values():
        for chain in chains:
            dets = [cache[x["rcept_no"]] for x in chain]
            amount = next((d["amount"] for d in reversed(dets) if d.get("amount")), None)
            dates: dict = {}
            for d in dets:  # 뒤 신고서 값이 우선
                dates.update(dict(d.get("dates", [])))
            for x in chain:
                d = cache[x["rcept_no"]]
                if d.get("amount") is None and amount:
                    d["amount"] = amount
                    merged_n += 1
                d["dates"] = [[k, v] for k, v in dates.items()]
                d["dup"] = x is not chain[-1]
    if merged_n:
        log(f"연속 신고서 묶음으로 금액 보완 {merged_n}건")

    # 같은 건의 일정 변경 정정(같은 회사·유형·금액): 최신 공시만 대표, 이전 공시는 superseded 표시
    latest: dict[tuple, str] = {}
    for e in events:
        d = cache.get(e["rcept_no"])
        if not d or "err" in d or not d.get("amount") or not specs.get(e["category"]):
            continue
        k = (e["corp_code"], e["category"], d["amount"])
        if e["rcept_no"] > latest.get(k, ""):
            latest[k] = e["rcept_no"]
    for e in events:
        d = cache.get(e["rcept_no"])
        if d and "err" not in d and d.get("amount") and specs.get(e["category"]):
            sup = latest[(e["corp_code"], e["category"], d["amount"])]
            d["superseded_by"] = sup if sup != e["rcept_no"] else None
            if d["superseded_by"]:
                d["dup"] = True

    # 카테고리별 추출 성공률 (검증용)
    hit: dict[str, dict] = {}
    for e in events:
        det = cache.get(e["rcept_no"])
        if det is None or not specs.get(e["category"]):
            continue
        h = hit.setdefault(e["category"], {"원문": 0, "오류": 0, "금액": 0})
        if "err" in det:
            h["오류"] += 1
            errs = STATS.setdefault("detail_errors", {})
            k = det["err"][:60]
            errs[k] = errs.get(k, 0) + 1
            continue
        # 과거 발행분 정정 판정: 실제 돈이 오가는 날(key_date)이 공시일보다 60일 이상 과거이면 과거 건.
        # key_date가 없으면 정정공시이면서 최초제출일이 60일 이상 과거인 경우만 과거 건.
        pub = f"{e['date'][:4]}-{e['date'][4:6]}-{e['date'][6:]}"
        cut = (date.fromisoformat(pub) - timedelta(days=60)).isoformat()
        kd = dict(det.get("dates", [])).get(specs[e["category"]].get("key_date", ""))
        if kd:
            det["old"] = kd < cut
        else:
            det["old"] = bool(e["correction"] and det.get("first_filed") and det["first_filed"] < cut)
        e["detail"] = det
        h["원문"] += 1
        if det["old"]:
            h["과거공시 정정"] = h.get("과거공시 정정", 0) + 1
        why = _anomaly(e, det)
        if why:
            h["이상:" + why] = h.get("이상:" + why, 0) + 1
        h["금액"] += det["amount"] is not None
        for label, _ in det["dates"]:
            h[label] = h.get(label, 0) + 1
        for label, _ in det["texts"]:
            h[label] = h.get(label, 0) + 1
    STATS["detail_hits"] = hit
    if dc.get("probe"):
        save_json(ROOT / "reports" / "probe.json", probe)
    log(f"공시 원문 신규 {fetched}건 처리")
    # 작성책임자가 없는 법인 → 그 법인의 최신 주요사항보고서·증권신고서(표지 있음) 원문에서 보충
    has = {e["corp_code"] for e in events if (cache.get(e["rcept_no"]) or {}).get("contact")}
    cover_rx = re.compile(r"주요사항보고서|증권신고서")
    tried = 0
    try:
        for e in sorted(events, key=lambda x: x["rcept_no"], reverse=True):
            if e["corp_code"] in has or not cover_rx.search(e["report_nm"]):
                continue
            d = cache.get(e["rcept_no"])
            if d is not None and d.get("ct_v") == CONTACT_VER:
                continue
            if tried >= dc.get("max_contact_docs_per_run", 800):
                break
            text, err = dart_document(e["rcept_no"])
            tried += 1
            ct = parse_contact(doc_rows(text)) if text else None
            d = cache.setdefault(e["rcept_no"], {})
            d["ct_v"] = CONTACT_VER
            if ct:
                d["contact"] = ct
                has.add(e["corp_code"])
    except (OutOfTime, DartError) as ex:
        warn(f"작성책임자 보충 중단: {ex}")
    save_json(path, cache)
    if tried:
        log(f"작성책임자 보충 원문 {tried}건 조회")
    # 법인별 최신 공시의 작성책임자
    contacts: dict[str, dict] = {}
    for e in sorted(events, key=lambda x: x["rcept_no"], reverse=True):
        ct = (cache.get(e["rcept_no"]) or {}).get("contact")
        if ct and e["corp_code"] not in contacts:
            nm = ct.get("name") or ""
            if re.fullmatch(r"[가-힣 ]+", nm) and len(nm.replace(" ", "")) <= 4:
                nm = nm.replace(" ", "")  # '송 세열' → '송세열'
            contacts[e["corp_code"]] = ct | {"name": nm or None, "rcept_no": e["rcept_no"], "date": e["date"],
                                             "report_nm": e["report_nm"]}
    log(f"작성책임자 확보 {len(contacts):,}개사")
    STATS["contacts"] = len(contacts)
    return contacts


def fmt_event_detail(e: dict) -> str:
    det = e.get("detail")
    if not det:
        return ""
    parts = []
    if det.get("amount"):
        parts.append(f"{det['amount_label']} {eok(det['amount'])}" if det["amount"] >= 1e8
                     else f"{det['amount_label']} {det['amount']:,}원")
    parts += [f"{k} {v}" for k, v in det.get("dates", [])[:2]]
    if det.get("uses"):
        top = max(det["uses"], key=lambda x: x[1])
        parts.append(f"주 용도 {top[0]}")
    purpose = next((v for k, v in det.get("texts", []) if k in ("목적", "사유")), None)
    if purpose:
        parts.append(f"목적 {purpose[:30]}")
    return ", ".join(parts)


def update_company_info(universe: dict) -> dict:
    """DART 기업개황(company.json): 대표이사·주소·전화·홈페이지 등. 30일 캐시, 1회 최대 N개사."""
    cfg = CFG.get("company_info", {})
    path = CACHE_DIR / "company.json"
    cache = load_json(path, {})
    ttl, cap = cfg.get("refresh_days", 30), cfg.get("max_per_run", 1000)
    now = now_kst()
    fetched = pending = 0
    try:
        for cc in sorted(universe, key=lambda k: not universe[k]["watch"]):
            hit = cache.get(cc)
            if hit and (now - datetime.fromisoformat(hit["fetched_at"])).days < ttl:
                continue
            if fetched >= cap:
                pending += 1
                continue
            j = dart_get("company.json", corp_code=cc)
            fetched += 1
            info = None
            if j.get("status") == "000":
                info = {k: (j.get(src) or "").strip() for k, src in
                        [("ceo", "ceo_nm"), ("addr", "adres"), ("phone", "phn_no"), ("fax", "fax_no"),
                         ("url", "hm_url"), ("ir_url", "ir_url"), ("est_dt", "est_dt"), ("acc_mt", "acc_mt"),
                         ("bizr_no", "bizr_no"), ("induty_code", "induty_code")]}
                info = {k: v for k, v in info.items() if v}
            cache[cc] = {"fetched_at": now.isoformat(), "info": info}
            if fetched % 200 == 0:
                save_json(path, cache)
    except OutOfTime:
        warn("시간 제한으로 기업개황 수집을 중간에 멈췄습니다 — 다음 실행에서 이어서 수집합니다.")
    except DartError as e:
        warn(f"기업개황 수집 중단: {e}")
    save_json(path, cache)
    if pending:
        warn(f"기업개황 미수집 {pending}개사 — 다음 실행에서 이어서 수집합니다.")
    log(f"기업개황 신규 {fetched}개사")
    return {cc: (cache.get(cc) or {}).get("info") for cc in universe}


def ref_period(fin: dict) -> tuple[str, str]:
    """비교 기준: 현재 재무의 직전 사업보고서"""
    y = int(fin["period"][:4])
    return (str(y - 1), "11011")


def update_ref_assets(fins: dict) -> dict:
    """단위 오류 확인용 — 직전 사업보고서 자산총계 (fnlttMultiAcnt, 1회 100개사). 결과는 법인·기간별 캐시."""
    path = CACHE_DIR / "ref_assets.json"
    cache = load_json(path, {})
    need: dict[tuple, list] = {}
    for cc, f in fins.items():
        if f and f.get("assets") and not f.get("financial"):
            y, r = ref_period(f)
            if f"{cc}:{y}" not in cache:
                need.setdefault((y, r), []).append(cc)
    calls = 0
    try:
        for (y, r), ccs in need.items():
            for i in range(0, len(ccs), 100):
                batch = ccs[i:i + 100]
                j = dart_get("fnlttMultiAcnt.json", corp_code=",".join(batch), bsns_year=y, reprt_code=r)
                calls += 1
                got: dict[str, dict] = {}
                for row in j.get("list") or []:
                    if norm(row.get("account_nm", "")) == "자산총계" and row.get("sj_div", "BS") == "BS":
                        got.setdefault(row["corp_code"], {})[row.get("fs_div", "")] = to_int(row.get("thstrm_amount"))
                for cc in batch:
                    cache[f"{cc}:{y}"] = got.get(cc, {})
                save_json(path, cache)
    except (OutOfTime, DartError) as e:
        warn(f"비교용 자산총계 조회 중단: {e}")
    save_json(path, cache)
    if calls:
        log(f"비교용 자산총계 조회 {calls}회")
    out = {}
    for cc, f in fins.items():
        if f and f.get("assets") and not f.get("financial"):
            y, _ = ref_period(f)
            ref = cache.get(f"{cc}:{y}") or {}
            out[cc] = ref.get("CFS" if f.get("fs") == "연결" else "OFS") or ref.get("CFS") or ref.get("OFS")
    return out


def data_flags(f: dict, m: dict | None, ref_assets: int | None) -> list[dict]:
    """재무 수치 이상 신호. level: 'error'(점수 제외) / 'warn'(확인 권장)"""
    if not f:
        return []
    fl = []
    a, li, b = f.get("assets") or 0, f.get("liabilities") or 0, f.get("borrowings") or 0
    if not a:
        fl.append({"code": "no_assets", "level": "error", "msg": "자산총계 0 또는 미기재 — 재무 미확보로 처리"})
        return fl
    if ref_assets and ref_assets > 0:
        ratio = a / ref_assets
        if ratio >= 30 or ratio <= 1 / 30:
            fl.append({"code": "unit", "level": "error",
                       "msg": f"단위 오류 의심 — 직전 사업보고서 자산총계 {eok(ref_assets)} 대비 {ratio:,.0f}배"
                       if ratio >= 1 else f"단위 오류 의심 — 직전 사업보고서 자산총계 {eok(ref_assets)} 대비 1/{1 / ratio:,.0f}"})
    if m and (m.get("cash_total") or 0) > a:
        fl.append({"code": "cash_gt_assets", "level": "error", "msg": "현금성자산이 자산총계보다 큼 — 공시 수치 확인 필요"})
    if b > li * 1.01:
        fl.append({"code": "borrow_gt_liab", "level": "warn",
                   "msg": f"차입금·사채({eok(b)})가 부채총계({eok(li)})보다 큼 — 계정 중복 합산 가능, 순현금 과소"})
    if b == 0 and li / a >= 0.5:
        fl.append({"code": "borrow_zero", "level": "warn",
                   "msg": f"부채비율이 높은데(부채/자산 {li / a:.0%}) 차입금 0 — 계정명 미인식 가능, 순현금 과대"})
    if li > a:
        fl.append({"code": "impaired", "level": "warn", "msg": "부채총계가 자산총계보다 큼 (자본잠식)"})
    if m and (m.get("cash_growth") or 0) > 5:
        fl.append({"code": "cash_jump", "level": "warn",
                   "msg": f"현금성자산 전기말 대비 {m['cash_growth'] + 1:,.0f}배 — 증자·매각 등 일회성 여부 확인"})
    if not f.get("period", "").startswith(str(now_kst().year)):
        fl.append({"code": "old", "level": "warn", "msg": f"재무 기준이 최신 아님 ({f.get('period')})"})
    return fl


# ───────────────────────── 점수·분류 ─────────────────────────
def pct_rank(values: dict) -> dict:
    items = sorted(((k, v) for k, v in values.items() if v is not None), key=lambda x: x[1])
    n = len(items)
    return {k: (i / (n - 1) if n > 1 else 1.0) for i, (k, _) in enumerate(items)}


def eok(n) -> str:
    return "-" if n is None else f"{n / 1e8:,.0f}억"


def score_companies(universe: dict, fins: dict, events: list[dict], ref: dict | None = None) -> list[dict]:
    today = now_kst().date()
    sc = CFG["scoring"]
    w = sc["weights"]
    seg_cfg = CFG["segments"]
    short_floor = ymd(today - timedelta(days=seg_cfg["short_window_days"]))

    ev_by_corp: dict[str, list] = {}
    for ev in events:
        ev_by_corp.setdefault(ev["corp_code"], []).append(ev)

    metrics = {}
    for cc in universe:
        f = fins.get(cc)
        if not f:
            metrics[cc] = None
            continue
        cash = (f["cash"] or 0) + (f["st_fin"] or 0)
        if f.get("financial"):
            # 금융회사는 예수·차입·보험부채가 영업 자체라 순현금·현금비중·현금증감으로 여유자금을 볼 수 없음 → 점수 미적용
            metrics[cc] = {"cash_total": cash, "net_cash": None, "cash_ratio": None, "cash_growth": None, "financial": True}
            continue
        net = cash - (f["borrowings"] or 0)
        ratio = cash / f["assets"] if f["assets"] else None
        growth = ((f["cash"] - f["cash_prev"]) / f["cash_prev"]
                  if f["cash"] is not None and f["cash_prev"] else None)
        metrics[cc] = {"cash_total": cash, "net_cash": net, "cash_ratio": ratio, "cash_growth": growth}
    flags = {cc: data_flags(fins.get(cc), metrics.get(cc), (ref or {}).get(cc)) for cc in universe}
    for cc, fl in flags.items():
        if any(x["level"] == "error" for x in fl) and metrics.get(cc):
            # 공시 수치 자체가 틀렸을 가능성이 큰 경우 → 순위 계산에서 제외 (값은 화면에 그대로 보여주고 '확인 필요' 표시)
            metrics[cc] = {**metrics[cc], "net_cash": None, "cash_ratio": None, "cash_growth": None, "excluded": True}

    # 현금성자산 총량(현금 + 단기금융상품) 순위 — 금융회사·수치 오류 의심은 제외 (순현금은 차입금 표기 오차가 커서 점수에서 뺌, 표시만)
    r_cash = pct_rank({k: m["cash_total"] for k, m in metrics.items()
                       if m and not m.get("financial") and not m.get("excluded") and m.get("cash_total")})
    r_ratio = pct_rank({k: (m or {}).get("cash_ratio") for k, m in metrics.items()})
    r_growth = pct_rank({k: (m or {}).get("cash_growth") for k, m in metrics.items()})
    seg_by_id = {s["id"]: s for s in PRODUCTS["segments"]}

    out = []
    for cc, c in universe.items():
        evs = ev_by_corp.get(cc, [])
        m = metrics[cc]
        live = [e for e in evs if not (e.get("detail") or {}).get("old") and not (e.get("detail") or {}).get("dup")]
        score_floor = ymd(today - timedelta(days=sc.get("inflow_window_days", 90)))
        inflow_cats = {e["category"] for e in live if e["type"] == "유입" and e["date"] >= score_floor}
        ev_pts = min(len(inflow_cats) * sc["points_per_inflow_category"], w["inflow_event"])
        detail = {
            "cash_total": round(r_cash[cc] * w["cash_total"], 1) if cc in r_cash else 0,
            "cash_ratio": round(r_ratio.get(cc, 0) * w["cash_ratio"], 1) if m and m["cash_ratio"] is not None else 0,
            "cash_growth": round(r_growth.get(cc, 0) * w["cash_growth"], 1) if m and m["cash_growth"] is not None else 0,
            "inflow_event": ev_pts,
        }
        score = round(sum(detail.values()), 1)

        recent_inflow = [e for e in live if e["type"] == "유입" and e["date"] >= short_floor]
        if recent_inflow:
            seg = "short"
        elif m and m["cash_ratio"] is not None and m["cash_ratio"] >= seg_cfg["long_min_cash_ratio"]:
            seg = "long"
        elif m and m["cash_ratio"] is not None and (m["cash_total"] or 0) > 0:
            seg = "mid"
        else:
            seg = "watch"

        # 규칙 기반 영업 포인트
        points = []
        for e in live[:3]:
            d = f"{e['date'][:4]}.{e['date'][4:6]}.{e['date'][6:]}"
            extra = fmt_event_detail(e)
            points.append(f"{d} {e['category']} 공시{f' ({extra})' if extra else ''} — {e['hint']}")
        if m and m.get("financial"):
            f = fins[cc]
            points.append(f"금융회사 — {f.get('cash_label', '현금및현금성자산')} {eok(m['cash_total'])} "
                          f"(예수·차입이 영업 자체라 순현금·현금비중 지표 미적용, 공시로만 판단)")
        elif m and m.get("excluded"):
            msg = next(x["msg"] for x in flags[cc] if x["level"] == "error")
            points.append(f"재무 공시 수치 확인 필요 — {msg} (여유자금 점수에서 재무 항목 제외)")
        elif m:
            f = fins[cc]
            line = f"현금성자산 {eok(m['cash_total'])} (현금 {eok(f['cash'])} + 단기금융상품 {eok(f.get('st_fin'))})"
            if m["cash_ratio"] is not None:
                line += f", 자산 대비 {m['cash_ratio']:.0%}"
            line += f" · 참고: 순현금 {eok(m['net_cash'])}"
            points.append(line)
            if m["cash_growth"] is not None and abs(m["cash_growth"]) >= 0.3:
                g = m["cash_growth"]
                gtxt = "+500% 초과" if g > 5 else f"{g:+.0%}"
                points.append(f"현금성자산 전기말 대비 {gtxt} 변동 (전기말 {eok(fins[cc]['cash_prev'])} → {eok(fins[cc]['cash'])})")
        else:
            points.append("DART 재무 미확보 — 공시·뉴스 기반으로만 판단")

        out.append({
            **c,
            "market_label": MARKET_LABEL.get(c["market"], "상장" if c["stock_code"] else "비상장"),
            "fin": fins.get(cc),
            "metrics": m,
            "score": score,
            "score_detail": detail,
            "segment": seg,
            "event_ids": [e["rcept_no"] for e in evs],
            "last_event_date": evs[0]["date"] if evs else "",
            "points": points,
            "news": [],
            **({"flags": flags[cc]} if flags.get(cc) else {}),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ───────────────────────── 뉴스 (네이버) ─────────────────────────
def naver_news(query: str, display: int) -> list[dict]:
    try:
        r = http_get("https://openapi.naver.com/v1/search/news.json",
                     params={"query": query, "display": display, "sort": "date"},
                     headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET})
        if r.status_code != 200:
            warn(f"뉴스 '{query}' 조회 실패 (HTTP {r.status_code})")
            return []
        items = r.json().get("items", [])
    except Exception as e:
        warn(f"뉴스 '{query}' 조회 오류: {e}")
        return []
    out = []
    for it in items:
        try:
            pub = parsedate_to_datetime(it.get("pubDate", "")).astimezone(KST).isoformat()
        except Exception:
            pub = ""
        out.append({
            "title": clean_text(it.get("title")),
            "desc": clean_text(it.get("description"))[:120],
            "url": it.get("originallink") or it.get("link"),
            "date": pub,
        })
    return out


def google_news(query: str, n: int) -> list[dict]:
    """구글 뉴스 RSS (키 불필요, 비공식 — 형식이 바뀌면 경고 후 건너뜀)"""
    try:
        r = http_get("https://news.google.com/rss/search",
                     params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
        if r.status_code != 200:
            warn(f"구글 뉴스 '{query}' 조회 실패 (HTTP {r.status_code})")
            return []
        root = ET.fromstring(r.content)
    except Exception as e:
        warn(f"구글 뉴스 '{query}' 조회 오류: {e}")
        return []
    out = []
    for it in root.iter("item"):
        title = clean_text(it.findtext("title"))
        src = clean_text(it.findtext("source") or "")
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(KST).isoformat()
        except Exception:
            pub = ""
        out.append({"title": title, "desc": src, "url": (it.findtext("link") or "").strip(), "date": pub})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:n]


def _name_in(name: str, text: str) -> bool:
    """법인명이 다른 단어의 일부가 아니라 그 자체로 나오는지 (예: '핑거' ≠ '골드핑거', '피노' ≠ '피노기오')"""
    rx = (r"(?<![가-힣A-Za-z0-9])" + re.escape(name) +
          r"(?=$|[^가-힣A-Za-z0-9]|[은는이가을를의에과와도로만측])")
    return re.search(rx, text) is not None


def clean_news(items: list[dict], must=(), exclude=(), name: str | None = None) -> list[dict]:
    """최근 max_age_days 이내 · 스팸 제외 · 필수어 포함 · 중복 제거 · 최신순"""
    nc = CFG["news"]
    cutoff = (now_kst() - timedelta(days=nc.get("max_age_days", 7))).isoformat()
    spam = nc.get("spam_words", [])
    blocked = nc.get("blocked_sources", [])

    def grams(t: str) -> set:
        t = norm(re.sub(r"[^\w가-힣]", "", t))
        return {t[k:k + 2] for k in range(len(t) - 1)}

    kept: list[set] = []
    out = []
    for i in sorted(items, key=lambda x: x["date"], reverse=True):
        t = i["title"]
        if not i["date"] or i["date"] < cutoff:
            continue
        if any(b.lower() in (i["desc"] + " " + i["url"]).lower() for b in blocked) or re.search(r"\bon X$", t):
            continue
        if any(w in t or w in i["desc"] for w in spam) or any(w in t for w in exclude):
            continue
        g = grams(t)  # 거의 같은 제목(여러 매체 전재)은 하나만
        if any(len(g & k) / max(1, min(len(g), len(k))) > nc.get("dup_threshold", 0.55) for k in kept):
            continue
        if must and not any(m.lower() in t.lower() for m in must):
            continue
        if name and not _name_in(name, t):
            continue
        if name and any(re.search(rx, t) for rx in nc.get("company_skip_regex", [])):
            continue  # 시세 자동 생성 기사 등
        kept.append(g)
        out.append(i)
    return out


def collect_news(companies: list[dict]) -> tuple[list[dict], str]:
    use_naver = bool(NAVER_ID and NAVER_SECRET)
    source = "네이버 뉴스" if use_naver else "구글 뉴스"
    nc = CFG["news"]
    days = nc.get("max_age_days", 7)

    def search(q: str, n: int) -> list[dict]:
        if use_naver:
            return naver_news(q, n)
        return google_news(f"{q} when:{days}d", n)  # 구글 뉴스 기간 연산자

    topics = []
    for t in nc["topics"]:
        raw = []
        for q in t["queries"]:
            raw += search(q, nc["fetch_per_query"])
            time.sleep(0.2)
        items = clean_news(raw, t.get("must", []), t.get("exclude", []))[: nc["per_topic"]]
        topics.append({"keyword": t["name"], "items": items})
    for c in companies[: nc["top_companies"]]:
        q = c["name"] if use_naver else '"' + c["name"] + '"'
        c["news"] = clean_news(search(q, nc["per_company"] * 4), name=c["name"])[: nc["per_company"]]
        time.sleep(0.2)
    n_items = sum(len(t["items"]) for t in topics)
    log(f"뉴스 수집 완료 ({source}, 최근 {days}일, 주제 {len(topics)}개 {n_items}건)")
    return topics, source


# ───────────────────────── 금리 (ECOS) ─────────────────────────
def fetch_rates() -> list[dict]:
    """한국은행 ECOS 금리. config rates.items 의 항목별 시계열(일별 history_days, 월별 history_months)."""
    if not ECOS_KEY:
        warn("ECOS API 키가 없어 금리 수집을 건너뜁니다.")
        return []
    rc = CFG["rates"]
    today = now_kst().date()
    out = []
    for it in rc["items"]:
        if it["cycle"] == "M":
            s_ = (today - timedelta(days=31 * rc.get("history_months", 60))).strftime("%Y%m")
            e_ = today.strftime("%Y%m")
        else:
            s_, e_ = ymd(today - timedelta(days=rc.get("history_days", 1100))), ymd(today)
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{ECOS_KEY}/json/kr/1/10000/"
               f"{it['stat']}/{it['cycle']}/{s_}/{e_}/{it['item']}")
        try:
            j = http_get(url).json()
            rows = (j.get("StatisticSearch") or {}).get("row") or []
            series = [[r["TIME"], round(float(r["DATA_VALUE"]), 3)] for r in rows
                      if r.get("DATA_VALUE") not in (None, "")]
        except Exception as ex:
            warn(f"금리 '{it['name']}' 조회 오류: {ex}")
            continue
        if not series:
            warn(f"금리 '{it['name']}' 데이터 없음 — 통계코드 확인 필요 ({j.get('RESULT')})")
            continue
        series.sort()
        if it.get("transform") == "fed_upper":
            # ECOS는 미국 정책금리를 목표범위 중간값(예: 3.875 = 3.75~4.00%)으로 제공 → 상단값으로 변환
            series = [[t, round(v + 0.125, 3) if round(v * 1000) % 250 == 125 else v] for t, v in series]
        # 일별 기준금리처럼 주말까지 채워진 계열은 값이 바뀌는 날 + 마지막 날만 남겨 용량 절감
        if it.get("step"):
            series = [p for i, p in enumerate(series) if i == 0 or i == len(series) - 1 or p[1] != series[i - 1][1]]
        out.append({k: it[k] for k in ("key", "name", "group", "cycle", "region", "brief") if k in it}
                   | {"date": series[-1][0], "latest": series[-1][1],
                      "prev": series[-2][1] if len(series) > 1 else None, "series": series})
    log(f"금리 {len(out)}개 항목")
    return out


def _walk_dicts(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk_dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_dicts(v)


BOND_DIAG = {"retry": 0, "status": Counter(), "sample": None, "no_data_reports": []}


def enrich_bond_dates(events: list[dict]) -> None:
    """회사채(증권신고서-채무증권): DART 증권신고서 주요정보 API(bdRs)로 납입일·모집총액. 접수번호별 캐시."""
    path = CACHE_DIR / "bond.json"
    cache = load_json(path, {})
    got = miss = 0
    try:
        for e in events:
            if e.get("category") != "회사채 발행" or e.get("detail"):
                continue
            rn = e["rcept_no"]
            if rn not in cache or "exact" not in cache[rn] and BOND_DIAG["retry"] < 400:
                BOND_DIAG["retry"] += 1
                j = dart_get("bdRs.json", corp_code=e["corp_code"], bgn_de=e["date"], end_de=e["date"])
                if j.get("status") == "013":  # 정정·발행조건확정은 최초 증권신고서 접수일로 조회됨 → 30일 전부터
                    bgn = (parse_ymd(e["date"]) - timedelta(days=30)).strftime("%Y%m%d")
                    j = dart_get("bdRs.json", corp_code=e["corp_code"], bgn_de=bgn, end_de=e["date"])
                BOND_DIAG["status"][j.get("status", "?")] += 1
                if not BOND_DIAG["sample"] and j.get("status") == "000":
                    BOND_DIAG["sample"] = {"keys": sorted({k for d in _walk_dicts(j) for k in d})[:80],
                                           "groups": [g.get("title") for g in j.get("group", []) if isinstance(g, dict)]}
                if j.get("status") == "013":
                    BOND_DIAG["no_data_reports"].append(e["report_nm"][:30]) if len(BOND_DIAG["no_data_reports"]) < 15 else None
                rows = [d for d in _walk_dicts(j) if any(k in d for k in ("pymd", "sbd", "slta"))]
                # 같은 접수번호 → 없으면 우리 공시 직전에 접수된 최초 신고서 (회차가 여럿이면 가장 가까운 것)
                prev = sorted((d for d in rows if str(d.get("rcept_no", "")) <= rn), key=lambda d: str(d.get("rcept_no", "")))
                pick = [d for d in rows if d.get("rcept_no") == rn] or ([prev[-1]] if prev else rows[:1])
                info = {}
                exact = any(d.get("rcept_no") == rn for d in pick)
                for d in pick:
                    for k in ("pymd", "sbd", "slta", "rpd", "bdnmn"):
                        if d.get(k) and k not in info:
                            info[k] = d[k]
                info["exact"] = exact
                cache[rn] = info
            info = cache[rn]
            pay = parse_date(info.get("pymd", "")) or (info.get("pymd") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", info.get("pymd", "")) else None)
            if not pay and re.fullmatch(r"\d{8}", info.get("pymd", "")):
                pay = f"{info['pymd'][:4]}-{info['pymd'][4:6]}-{info['pymd'][6:]}"
            if not pay:
                miss += 1
                continue
            got += 1
            e["detail"] = {"amount": to_int(info.get("slta")),
                           "amount_label": "발행" if info.get("exact") else "모집(최초 신고 기준, 증액 전일 수 있음)", "dates": [["납입일", pay]], "texts": []}
            if info.get("rpd"):
                rp = parse_date(info["rpd"])
                if rp:
                    e["detail"]["dates"].append(["만기일", rp])
    except (OutOfTime, DartError) as ex:
        warn(f"회사채 납입일 조회 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
    save_json(path, cache)
    STATS["bond_dates"] = {"납입일": got, "없음": miss}
    log(f"회사채 납입일 {got}건 확보, {miss}건 없음")


def dates_status(events: list[dict]) -> dict:
    """유형별 '실제 자금 일자' 확보 현황 (공개 저장소용 — 건수만)"""
    cash = ("납입일", "지급예정일", "지급예정일(추정)", "양수예정일", "양도예정일", "기간 시작", "투자 시작", "매도일",
            "취득일", "대여 시작", "상환일", "상환일(결의일)", "차입일", "차입일(결의일)")
    out = {}
    for e in events:
        det = e.get("detail") or {}
        if det.get("old") or det.get("dup"):
            continue
        o = out.setdefault(e["category"], {"n": 0, "with_date": 0, "kinds": Counter()})
        o["n"] += 1
        ks = [k for k, v in (det.get("dates") or []) if v and k in cash]
        pay = est_dividend_pay(e)
        if pay:
            ks.append("지급예정일(추정)")
        if ks:
            o["with_date"] += 1
        for k in set(ks):
            o["kinds"][k] += 1
    return out


def filter_ipo(events: list[dict]) -> list[dict]:
    """증권신고서(지분증권) 중 신규 상장 공모만 남긴다.
    - 같은 회사가 앞뒤 60일 안에 유상증자결정을 냈으면 상장사 공모 유상증자 → 제외 (유상증자로 이미 잡힘)
    - 스팩(기업인수목적회사)은 공모자금이 신탁에 묶여 운용 대상이 아니라 제외"""
    rights = {}
    for e in events:
        if e["category"] == "유상증자":
            rights.setdefault(e["corp_code"], []).append(parse_ymd(e["date"]))
    out, dropped = [], Counter()
    for e in events:
        if e["category"] == "IPO 공모":
            d = parse_ymd(e["date"])
            if any(abs((d - x).days) <= 60 for x in rights.get(e["corp_code"], [])):
                dropped["상장사 유상증자"] += 1
                continue
            if re.search(r"스팩|기업인수목적|SPAC", e["corp_name"], re.I):
                dropped["스팩"] += 1
                continue
        out.append(e)
    STATS["ipo_filter"] = {"kept": sum(1 for e in out if e["category"] == "IPO 공모"), **dropped}
    return out


IPO_DIAG = {"status": Counter(), "sample": None}


def enrich_ipo(events: list[dict]) -> None:
    """IPO 공모: DART 증권신고서 주요정보(estkRs)로 납입일·모집총액·청약일. 접수번호별 캐시."""
    path = CACHE_DIR / "ipo.json"
    cache = load_json(path, {})
    got = 0
    try:
        for e in events:
            if e.get("category") != "IPO 공모" or e.get("detail"):
                continue
            rn = e["rcept_no"]
            if (rn not in cache or cache[rn].get("v") != 3) and STATS.get("ipo_api", 0) < 300:
                STATS["ipo_api"] = STATS.get("ipo_api", 0) + 1
                # IPO 는 최초 증권신고서가 확정 공시보다 1~3달 앞서 접수되는 경우가 많아 150일 전부터 조회
                bgn = (parse_ymd(e["date"]) - timedelta(days=150)).strftime("%Y%m%d")
                j = dart_get("estkRs.json", corp_code=e["corp_code"], bgn_de=bgn, end_de=e["date"])
                IPO_DIAG["status"][j.get("status", "?")] += 1
                if not IPO_DIAG["sample"] and j.get("status") == "000":
                    IPO_DIAG["sample"] = {"keys": sorted({k for d in _walk_dicts(j) for k in d})[:90],
                                          "groups": [g.get("title") for g in j.get("group", []) if isinstance(g, dict)],
                                          "first_rows": [{k: v for k, v in d.items() if k not in ("corp_code",)} for d in list(_walk_dicts(j))[1:4]]}
                rows = [d for d in _walk_dicts(j) if d.get("pymd") or d.get("slta")]
                # 납입일(일반사항)과 모집총액(증권의종류)이 다른 묶음에 있어, 같은 접수번호의 행을 모두 모은다
                rcs = sorted({str(d.get("rcept_no", "")) for d in rows})
                target = rn if rn in rcs else (max([x for x in rcs if x <= rn], default="") or (rcs[-1] if rcs else ""))
                pick = [d for d in rows if str(d.get("rcept_no", "")) == target]
                info = {"v": 3, "exact": target == rn}
                for d in pick:
                    for k in ("pymd", "sbd", "slta", "stksen", "slmth"):
                        if d.get(k) and k not in info:
                            info[k] = d[k]
                cache[rn] = info
            info = cache[rn]
            pay = parse_date(info.get("pymd", "") or "")
            if not pay and re.fullmatch(r"\d{8}", str(info.get("pymd", ""))):
                pay = f"{info['pymd'][:4]}-{info['pymd'][4:6]}-{info['pymd'][6:]}"
            if not pay:
                continue
            got += 1
            sub = parse_date(info.get("sbd", "") or "")
            e["detail"] = {"amount": to_int(info.get("slta")), "amount_label": "공모" if info.get("exact") else "공모(최초 신고 기준)",
                           "dates": [["납입일", pay]] + ([["청약일", sub]] if sub else []), "texts": []}
    except (OutOfTime, DartError) as ex:
        warn(f"IPO 공모 정보 조회 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
    save_json(path, cache)
    STATS["ipo_dates"] = got


def est_dividend_pay(e: dict) -> str | None:
    """배당 지급예정일이 공시에 없을 때 추정 (최근 1년 공시에서 지급예정일이 있는 건의 실제 간격 기준)
    - 기준일이 12월 말인 결산배당(주총 후 지급): 기준일 + 110일 (중앙값, 대부분 4월 중순~하순)
    - 그 밖(분기·중간배당, 주총 후 기준일을 정하는 새 방식): 기준일 + 21일 (중앙값)"""
    if e.get("category") != "배당":
        return None
    dt = dict((e.get("detail") or {}).get("dates") or [])
    if dt.get("지급예정일") or not dt.get("기준일"):
        return None
    try:
        b = date.fromisoformat(dt["기준일"])
    except ValueError:
        return None
    interim = any(x in e.get("report_nm", "") for x in ("분기", "중간"))
    days = 110 if (b.month == 12 and b.day >= 28 and not interim) else 21
    return (b + timedelta(days=days)).isoformat()


def slim_event(e: dict) -> dict:
    """화면용 출력에서 반복·기본값 필드 제거 (링크는 접수번호로, 안내문구는 meta.hints 로 화면에서 복원)"""
    o = {k: v for k, v in e.items() if k not in ("url", "hint") and not (k == "correction" and not v)}
    if "detail" in o:
        o["detail"] = {k: v for k, v in o["detail"].items()
                       if k not in ("v", "chain_checked", "contact") and v not in (None, False, [], "")}
        pay = est_dividend_pay(e)
        if pay:  # 화면의 '자금 일정'에서 실제 지급일 대신 쓰는 추정치 (표시에 '추정' 명시)
            o["detail"]["dates"] = list(o["detail"].get("dates") or []) + [["지급예정일(추정)", pay]]
    return o


# ───────────────────────── 메인 ─────────────────────────
def main() -> int:
    if not DART_KEY:
        print("DART_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 1
    started = now_kst()
    try:
        try:
            verify_detail_filters()
        except OutOfTime:
            warn("시간 제한으로 상세유형 필터 검증을 건너뜀")
        events, coverage = update_events()
    except DartError as e:
        store = load_event_store()
        if not store["events"]:
            print(f"[치명] {e}", file=sys.stderr)
            return 1
        warn(f"공시 수집 중단, 이전 수집분으로 진행: {e}")
        events = sorted(store["events"].values(), key=lambda x: (x["date"], x["rcept_no"]), reverse=True)
        coverage = (now_kst().date() - parse_ymd(store["oldest"])).days + 1 if store.get("oldest") else 0

    events = filter_ipo(events)

    watch, corp_map = {}, {}
    if WATCHLIST_PATH.exists() and len(WATCHLIST_PATH.read_text(encoding="utf-8-sig").strip().splitlines()) > 1:
        try:
            corp_map = load_corp_codes()
            watch = load_watchlist(corp_map)
        except DartError as e:
            warn(f"관심법인 매칭 실패: {e}")

    universe = build_universe(events, corp_map, watch)
    log(f"분석 대상 {len(universe):,}개사 (관심법인 {len(watch)}개)")

    infos = update_company_info(universe)
    fins = update_financials(universe, infos)
    contacts = update_details(events) if CFG.get("details", {}).get("enabled") else {}
    enrich_bond_dates(events)
    enrich_ipo(events)
    save_json(ROOT / "reports" / "dates_status.json", {"generated_at": now_kst().isoformat(timespec="minutes"),
                                                      "by_category": dates_status(events), "bond_diag": BOND_DIAG,
                                                      "ipo_diag": IPO_DIAG})
    ref = update_ref_assets(fins)
    companies = score_companies(universe, fins, events, ref)
    for c in companies:
        if (c.get("metrics") or {}).get("financial"):
            c["financial"] = True
        if infos.get(c["corp_code"]):
            c["info"] = infos[c["corp_code"]]
        if contacts.get(c["corp_code"]):
            c["contact"] = contacts[c["corp_code"]]
    topics, news_source = collect_news(companies)
    rates = fetch_rates()

    out = {
        "meta": {
            "generated_at": now_kst().isoformat(timespec="minutes"),
            "elapsed_sec": int((now_kst() - started).total_seconds()),
            "lookback_days": max(coverage, 1),
            "news_source": news_source,
            "hints": {r["category"]: r.get("hint", "") for r in reversed(CFG["event_rules"])},
            "is_sample": False,
            "warnings": WARNINGS,
        },
        "segments": PRODUCTS["segments"],
        "rates": rates,
        "events": [slim_event(e) for e in events],
        "companies": companies,
        "topics": topics,
    }
    calls = STATS["dart_calls"]
    out["meta"]["stats"] = {
        "dart_calls": calls,
        "dart_avg_sec": round(STATS["dart_sec"] / calls, 2) if calls else None,
        "list_pages": STATS["list_pages"],
        "fin_calls": STATS["fin_calls"],
        "fin_ok": sum(1 for c in companies if c["fin"]),
        "detail_filter": STATS["detail_filter"],
        "doc_calls": STATS.get("doc_calls", 0),
        "detail_hits": STATS.get("detail_hits", {}),
        "detail_errors": STATS.get("detail_errors", {}),
        "contacts": STATS.get("contacts", 0),
    }
    save_json(OUT_PATH, out)
    summary = (f"공시 {coverage}일·{len(events):,}건 / 법인 {len(companies):,}개(재무 {out['meta']['stats']['fin_ok']}) / "
               f"DART 호출 {calls:,}회·평균 {out['meta']['stats']['dart_avg_sec']}초 / 목록 페이지 {STATS['list_pages']} / "
               f"소요 {out['meta']['elapsed_sec']}초 / 경고 {len(WARNINGS)}건")
    log("요약: " + summary)
    if IN_ACTIONS:
        print(f"::notice title=수집 요약::{summary}", flush=True)
    log(f"저장 완료: {OUT_PATH.relative_to(ROOT)} (법인 {len(companies):,} / 이벤트 {len(events):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
