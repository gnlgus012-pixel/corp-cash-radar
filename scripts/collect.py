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


# ───────────────────────── 공통 유틸 ─────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"[경고] {msg}", file=sys.stderr, flush=True)
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
    params["crtfc_key"] = DART_KEY
    last_err = None
    for attempt in range(3):
        try:
            r = http_get(f"{DART}/{endpoint}", params=params)
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


def fetch_disclosures(bgn: date, end: date) -> list[dict]:
    rows = []
    for ty in CFG["dart"]["pblntf_types"]:
        page = 1
        while True:
            j = dart_get("list.json", bgn_de=ymd(bgn), end_de=ymd(end), pblntf_ty=ty,
                         last_reprt_at="Y", page_no=page, page_count=100)
            rows.extend(j.get("list") or [])
            total_page = int(j.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
    return rows


def update_events() -> list[dict]:
    """최근 lookback_days 일 자금 이벤트 공시. 증분 수집 + 캐시."""
    store_path = CACHE_DIR / "events.json"
    store = load_json(store_path, {"last_end": None, "events": {}})
    today = now_kst().date()
    floor = today - timedelta(days=CFG["lookback_days"])
    bgn = floor
    if store.get("last_end"):
        bgn = max(floor, parse_ymd(store["last_end"]) - timedelta(days=3))  # 정정 반영용 3일 겹침

    added = 0
    cur = bgn
    while cur <= today:  # DART 목록 조회는 기간 제한이 있어 30일 단위로 나눔
        chunk_end = min(cur + timedelta(days=29), today)
        log(f"공시 목록 조회 {cur} ~ {chunk_end}")
        for row in fetch_disclosures(cur, chunk_end):
            rule = classify(row.get("report_nm", ""))
            if not rule:
                continue
            rn = row["rcept_no"]
            if rn not in store["events"]:
                added += 1
            store["events"][rn] = {
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
        cur = chunk_end + timedelta(days=1)

    store["events"] = {k: v for k, v in store["events"].items() if v["date"] >= ymd(floor)}
    store["last_end"] = ymd(today)
    save_json(store_path, store)
    log(f"자금 이벤트 {len(store['events']):,}건 (신규 {added:,}건)")
    return sorted(store["events"].values(), key=lambda e: (e["date"], e["rcept_no"]), reverse=True)


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
    for r in rows:
        sj = r.get("sj_div", "")
        aid = r.get("account_id", "") or ""
        nm = norm(r.get("account_nm", ""))
        cur = to_int(r.get("thstrm_amount"))
        if sj == "BS":
            if f["cash"] is None and (aid == "ifrs-full_CashAndCashEquivalents" or nm == "현금및현금성자산"):
                f["cash"] = cur
                f["cash_prev"] = to_int(r.get("frmtrm_amount"))
            elif f["st_fin"] is None and nm in ("단기금융상품", "단기금융자산"):
                f["st_fin"] = cur
            elif f["assets"] is None and (aid == "ifrs-full_Assets" or nm == "자산총계"):
                f["assets"] = cur
            elif f["liabilities"] is None and (aid == "ifrs-full_Liabilities" or nm == "부채총계"):
                f["liabilities"] = cur
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
    return f


def fetch_latest_fin(corp_code: str) -> dict | None:
    y = now_kst().year
    candidates = [(y, "11014"), (y, "11012"), (y, "11013"), (y - 1, "11011"), (y - 1, "11014")]
    for year, code in candidates:
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


def update_financials(universe: dict) -> dict:
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
            if hit and (now - datetime.fromisoformat(hit["fetched_at"])).days < ttl:
                continue
            if fetched >= cap:
                skipped += 1
                continue
            cache[cc] = {"fetched_at": now.isoformat(), "fin": fetch_latest_fin(cc)}
            fetched += 1
            if fetched % 50 == 0:
                log(f"재무 수집 {fetched}개사…")
                save_json(cache_path, cache)
    except DartError as e:
        warn(f"재무 수집 중단: {e}")
    save_json(cache_path, cache)
    if skipped:
        warn(f"재무 미수집 {skipped}개사 — 다음 실행에서 이어서 수집합니다.")
    log(f"재무 신규 수집 {fetched}개사")
    return {cc: (cache.get(cc) or {}).get("fin") for cc in universe}


# ───────────────────────── 점수·분류 ─────────────────────────
def pct_rank(values: dict) -> dict:
    items = sorted(((k, v) for k, v in values.items() if v is not None), key=lambda x: x[1])
    n = len(items)
    return {k: (i / (n - 1) if n > 1 else 1.0) for i, (k, _) in enumerate(items)}


def eok(n) -> str:
    return "-" if n is None else f"{n / 1e8:,.0f}억"


def score_companies(universe: dict, fins: dict, events: list[dict]) -> list[dict]:
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
        net = cash - (f["borrowings"] or 0)
        ratio = cash / f["assets"] if f["assets"] else None
        growth = ((f["cash"] - f["cash_prev"]) / f["cash_prev"]
                  if f["cash"] is not None and f["cash_prev"] else None)
        metrics[cc] = {"cash_total": cash, "net_cash": net, "cash_ratio": ratio, "cash_growth": growth}

    r_net = pct_rank({k: (m or {}).get("net_cash") for k, m in metrics.items()})
    r_ratio = pct_rank({k: (m or {}).get("cash_ratio") for k, m in metrics.items()})
    r_growth = pct_rank({k: (m or {}).get("cash_growth") for k, m in metrics.items()})
    seg_by_id = {s["id"]: s for s in PRODUCTS["segments"]}

    out = []
    for cc, c in universe.items():
        evs = ev_by_corp.get(cc, [])
        m = metrics[cc]
        inflow_cats = {e["category"] for e in evs if e["type"] == "유입"}
        ev_pts = min(len(inflow_cats) * sc["points_per_inflow_category"], w["inflow_event"])
        detail = {
            "net_cash": round(r_net.get(cc, 0) * w["net_cash"], 1) if m else 0,
            "cash_ratio": round(r_ratio.get(cc, 0) * w["cash_ratio"], 1) if m and m["cash_ratio"] is not None else 0,
            "cash_growth": round(r_growth.get(cc, 0) * w["cash_growth"], 1) if m and m["cash_growth"] is not None else 0,
            "inflow_event": ev_pts,
        }
        score = round(sum(detail.values()), 1)

        recent_inflow = [e for e in evs if e["type"] == "유입" and e["date"] >= short_floor]
        if recent_inflow:
            seg = "short"
        elif m and m["net_cash"] > 0 and (m["cash_ratio"] or 0) >= seg_cfg["long_min_cash_ratio"]:
            seg = "long"
        elif m and m["net_cash"] > 0:
            seg = "mid"
        else:
            seg = "watch"

        # 규칙 기반 영업 포인트
        points = []
        for e in evs[:3]:
            d = f"{e['date'][:4]}.{e['date'][4:6]}.{e['date'][6:]}"
            points.append(f"{d} {e['category']} 공시 — {e['hint']}")
        if m:
            f = fins[cc]
            line = f"순현금 {eok(m['net_cash'])} (현금성 {eok(m['cash_total'])} − 차입 {eok(f['borrowings'])})"
            if m["cash_ratio"] is not None:
                line += f", 자산 대비 현금 {m['cash_ratio']:.0%}"
            points.append(line)
            if m["cash_growth"] is not None and abs(m["cash_growth"]) >= 0.3:
                points.append(f"현금성자산 전기말 대비 {m['cash_growth']:+.0%} 변동")
        else:
            points.append("DART 재무 미확보 — 공시·뉴스 기반으로만 판단")
        seg_info = seg_by_id.get(seg, {})
        if seg_info.get("products"):
            points.append(f"제안 상품군: {', '.join(seg_info['products'])}")

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


def collect_news(companies: list[dict]) -> list[dict]:
    if not (NAVER_ID and NAVER_SECRET):
        warn("네이버 API 키가 없어 뉴스 수집을 건너뜁니다.")
        return []
    nc = CFG["news"]
    topics = []
    for kw in nc["topic_keywords"]:
        topics.append({"keyword": kw, "items": naver_news(kw, nc["per_topic"])})
        time.sleep(0.1)
    for c in companies[: nc["top_companies"]]:
        items = naver_news(c["name"], nc["per_company"] * 2)
        c["news"] = [i for i in items if norm(c["name"]) in norm(i["title"] + i["desc"])][: nc["per_company"]]
        time.sleep(0.1)
    log(f"뉴스 수집 완료 (주제 {len(topics)}개, 법인 {min(len(companies), nc['top_companies'])}개)")
    return topics


# ───────────────────────── 금리 (ECOS) ─────────────────────────
def fetch_rates() -> list[dict]:
    if not ECOS_KEY:
        warn("ECOS API 키가 없어 금리 수집을 건너뜁니다.")
        return []
    today = now_kst().date()
    out = []
    for it in CFG["rates"]["items"]:
        if it["cycle"] == "M":
            s, e = (today - timedelta(days=730)).strftime("%Y%m"), today.strftime("%Y%m")
        else:
            s, e = ymd(today - timedelta(days=60)), ymd(today)
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{ECOS_KEY}/json/kr/1/100/"
               f"{it['stat']}/{it['cycle']}/{s}/{e}/{it['item']}")
        try:
            j = http_get(url).json()
            rows = (j.get("StatisticSearch") or {}).get("row") or []
            series = [[r["TIME"], float(r["DATA_VALUE"])] for r in rows if r.get("DATA_VALUE") not in (None, "")]
        except Exception as ex:
            warn(f"금리 '{it['name']}' 조회 오류: {ex}")
            continue
        if not series:
            warn(f"금리 '{it['name']}' 데이터 없음 — 통계코드 확인 필요 ({j.get('RESULT')})")
            continue
        series.sort()
        out.append({"name": it["name"], "date": series[-1][0], "latest": series[-1][1],
                    "prev": series[-2][1] if len(series) > 1 else None, "series": series[-40:]})
    log(f"금리 {len(out)}개 항목")
    return out


# ───────────────────────── 메인 ─────────────────────────
def main() -> int:
    if not DART_KEY:
        print("DART_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 1
    started = now_kst()
    try:
        events = update_events()
    except DartError as e:
        print(f"[치명] {e}", file=sys.stderr)
        return 1

    watch, corp_map = {}, {}
    if WATCHLIST_PATH.exists() and len(WATCHLIST_PATH.read_text(encoding="utf-8-sig").strip().splitlines()) > 1:
        try:
            corp_map = load_corp_codes()
            watch = load_watchlist(corp_map)
        except DartError as e:
            warn(f"관심법인 매칭 실패: {e}")

    universe = build_universe(events, corp_map, watch)
    log(f"분석 대상 {len(universe):,}개사 (관심법인 {len(watch)}개)")

    fins = update_financials(universe)
    companies = score_companies(universe, fins, events)
    topics = collect_news(companies)
    rates = fetch_rates()

    out = {
        "meta": {
            "generated_at": now_kst().isoformat(timespec="minutes"),
            "elapsed_sec": int((now_kst() - started).total_seconds()),
            "lookback_days": CFG["lookback_days"],
            "is_sample": False,
            "warnings": WARNINGS,
        },
        "segments": PRODUCTS["segments"],
        "rates": rates,
        "events": events,
        "companies": companies,
        "topics": topics,
    }
    save_json(OUT_PATH, out)
    log(f"저장 완료: {OUT_PATH.relative_to(ROOT)} (법인 {len(companies):,} / 이벤트 {len(events):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
