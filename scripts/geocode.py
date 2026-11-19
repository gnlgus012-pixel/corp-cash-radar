#!/usr/bin/env python3
"""
지도용 좌표 — 법인 본사 주소(DART 기업개황)를 위도·경도로 바꿔 data["geo"] 에 붙인다.

대상: 화면의 법인(companies) + 대주주 탭(자금 공시·락업·주식담보)에 나오는 법인
주소: DART company.json 'adres' (collect.py 의 30일 캐시 재사용, 없는 법인만 새로 조회)
좌표: KAKAO_REST_KEY 가 있으면 카카오 주소검색(건물 단위), 없으면 OpenStreetMap Nominatim
      Nominatim 은 도로명 주소 → 동 → 시군구 순으로 내려가며 찾고, 어느 단계에서 찾았는지(lv)를 남긴다.
      lv: 1 건물(도로명·지번) / 2 동·읍·면 / 3 시군구
캐시: cache/geo.json (주소 → 좌표). 주소가 바뀐 법인만 다시 찾는다.

실행
  python scripts/geocode.py            data.json 에 geo 를 붙인다 (암호화 전)
  python scripts/geocode.py --probe 40 무작위 40개사만 찾아 단계별 성공률을 출력 (저장 안 함, 회사명·주소만 출력)
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

GEO_PATH = C.CACHE_DIR / "geo.json"
KAKAO_KEY = os.environ.get("KAKAO_REST_KEY", "").strip()
MAX_NEW = int(os.environ.get("GEO_MAX_NEW", "400"))          # 1회 새로 찾을 주소 수 (Nominatim 1초 1건)
UA = "corp-cash-radar/1.0 (+https://github.com/gnlgus012-pixel/corp-cash-radar)"
NOM = "https://nominatim.openstreetmap.org/search"
STATS: Counter = Counter()
_last = [0.0]


def clean(addr: str) -> str:
    a = html.unescape(addr or "").replace("\xa0", " ")
    a = re.sub(r"&nbsp;?", " ", a)
    return re.sub(r"\s+", " ", a).strip(" -,")


SIDO = {"서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시", "광주": "광주광역시",
        "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도", "강원": "강원특별자치도",
        "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도",
        "경남": "경상남도", "제주": "제주특별자치도", "서울시": "서울특별시"}


# 행정구역 통합으로 없어진 이름 (DART 주소가 옛 이름 그대로인 법인이 있음)
LEGACY = {"청원군": "청주시", "여주군": "여주시", "당진군": "당진시", "연기군": "세종특별자치시", "마산시": "창원시",
          "진해시": "창원시", "포천군": "포천시", "양주군": "양주시", "화성군": "화성시", "김포군": "김포시", "광주군": "광주시",
          "이천군": "이천시", "안성군": "안성시", "용인군": "용인시", "파주군": "파주시", "평택군": "평택시"}
GEO_VER = 2   # 주소 정리 규칙이 바뀌면 올림 → 예전에 못 찾은 주소를 다시 찾는다


def parts(addr: str) -> dict:
    """도로명(번호까지) · 지번 · 동 · 시군구 후보"""
    a = clean(addr)
    a = re.sub(r"^(서울시|서울특별시|부산시|대구시|인천시|광주시|대전시|울산시)(?=[가-힣]+구\b)", r"\1 ", a)   # '서울시마포구'
    for k, v in LEGACY.items():
        a = a.replace(k, v)
    first = a.split(" ", 1)
    if first and first[0] in SIDO:
        a = SIDO[first[0]] + (" " + first[1] if len(first) > 1 else "")
    paren = re.findall(r"\(([^)]*)\)", a)
    base = re.sub(r"\([^)]*\)", " ", a)
    base = re.sub(r"\s+", " ", base).strip()
    toks = base.split()
    # 시도 + 시군구(+구): '경기도 성남시 분당구', '서울특별시 중구'
    sgg = []
    for t in toks[:4]:
        if re.search(r"(특별시|광역시|특별자치시|특별자치도|도|시|군|구)$", t):
            sgg.append(t)
        else:
            break
    road = re.match(r"^(.+?(?:로|길)\s*\d+(?:-\d+)?)(?=[\s,(]|$)", base)
    jibun = None if road else re.match(r"^(.+?(?:동|리|가)\s*(?:산\s*)?\d+(?:-\d+)?)(?=[\s,(]|$)", base)
    dong = None
    for p in paren:
        m = re.match(r"\s*([가-힣0-9]+(?:동|가|읍|면|리))\b", p)
        if m:
            dong = m.group(1)
            break
    if not dong:
        m = re.search(r"\s([가-힣0-9]+(?:동|읍|면))\s", " " + base + " ")
        dong = m.group(1) if m else None
    return {"full": a, "road": road.group(1) if road else None, "jibun": jibun.group(1) if jibun else None,
            "dong": " ".join(sgg + [dong]) if dong and sgg else None, "sgg": " ".join(sgg) if sgg else None}


def nominatim(q: str):
    wait = 1.1 - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    STATS["nom_calls"] += 1
    try:
        r = C.http_get(NOM, params={"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "kr",
                                    "accept-language": "ko"}, headers={"User-Agent": UA})
        if r.status_code != 200:
            STATS[f"nom_http_{r.status_code}"] += 1
            return None
        j = r.json()
        return (round(float(j[0]["lat"]), 6), round(float(j[0]["lon"]), 6)) if j else None
    except Exception as ex:  # noqa: BLE001
        STATS["nom_err"] += 1
        C.log(f"Nominatim 오류 {ex.__class__.__name__}")
        return None


def kakao(q: str):
    STATS["kakao_calls"] += 1
    try:
        r = C.http_get("https://dapi.kakao.com/v2/local/search/address.json", params={"query": q},
                       headers={"Authorization": f"KakaoAK {KAKAO_KEY}"})
        if r.status_code != 200:
            STATS[f"kakao_http_{r.status_code}"] += 1
            return None
        docs = r.json().get("documents") or []
        return (round(float(docs[0]["y"]), 6), round(float(docs[0]["x"]), 6)) if docs else None
    except Exception:  # noqa: BLE001
        STATS["kakao_err"] += 1
        return None


def locate(addr: str):
    """→ (lat, lng, lv, src) 또는 None"""
    p = parts(addr)
    if KAKAO_KEY:
        for q in (p["road"], p["jibun"], p["full"]):
            if q:
                hit = kakao(q)
                if hit:
                    return (*hit, 1, "kakao")
    for q, lv in ((p["road"], 1), (p["jibun"], 1), (p["dong"], 2), (p["sgg"], 3)):
        if q:
            hit = nominatim(q)
            if hit:
                return (*hit, lv, "osm")
    return None


def inside_korea(lat, lng) -> bool:
    return 33.0 <= lat <= 38.7 and 124.5 <= lng <= 131.0


# ───────── 수도권 사무소: 본점(등기)이 지방인데 실제 본사·사무소가 서울·경기에 있는 법인 ─────────
OFFICE_PATH = C.CACHE_DIR / "offices.json"
OFFICE_VER = 1
OFFICE_TTL_DAYS = 60
CAPITAL_RX = re.compile(r"^(서울|경기|인천)")
OFFICE_CAT_OK = re.compile(r"회사|기업|사무실|연구소|건설업|제조업|제약|IT|금융|공공기관")
OFFICE_NAME_BAD = re.compile(r"대리점|매장|판매점|직영점|서비스센터|물류센터|창고|공장|주유소|충전소|ATM|지점$|영업소|출장소|교육원|연수원|기숙사|어린이집")


def nm(s: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\s+", "", s or "")


def kakao_keyword(q: str) -> list:
    STATS["kakao_kw_calls"] += 1
    try:
        r = C.http_get("https://dapi.kakao.com/v2/local/search/keyword.json", params={"query": q, "size": 15},
                       headers={"Authorization": f"KakaoAK {KAKAO_KEY}"})
        if r.status_code != 200:
            STATS[f"kakao_kw_http_{r.status_code}"] += 1
            return []
        return r.json().get("documents") or []
    except Exception:  # noqa: BLE001
        STATS["kakao_kw_err"] += 1
        return []


def find_offices(name: str, home: tuple | None) -> list:
    """카카오 장소 검색으로 '법인명' 또는 '법인명 ○○' 이름의 수도권 회사 장소를 찾는다 (등기 본점과 5km 넘게 떨어진 곳만)"""
    n0 = nm(name)
    out = []
    for d in kakao_keyword(name):
        pn = d.get("place_name") or ""
        # 이름이 정확히 같거나 '법인명 + 공백/괄호'로 시작해야 함 ('SK' 가 'SK텔레콤' 에 걸리지 않게)
        if not (nm(pn) == n0 or re.match(re.escape(name) + r"[\s(]", pn) or re.match(re.escape(n0) + r"[\s(]", pn)):
            continue
        addr = d.get("road_address_name") or d.get("address_name") or ""
        if not CAPITAL_RX.match(addr) or OFFICE_NAME_BAD.search(pn) or not OFFICE_CAT_OK.search(d.get("category_name") or ""):
            continue
        lat, lng = round(float(d["y"]), 6), round(float(d["x"]), 6)
        if home and _km(home, (lat, lng)) < 5:
            continue
        if any(_km((o["lat"], o["lng"]), (lat, lng)) < 0.3 for o in out):
            continue
        out.append({"pn": pn, "a": addr, "lat": lat, "lng": lng, "cat": (d.get("category_name") or "").split(" > ")[-1]})
    return out[:3]


def _km(a, b) -> float:
    import math
    r = math.pi / 180
    h = math.sin((b[0] - a[0]) * r / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin((b[1] - a[1]) * r / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def phone_area(phones: list) -> str | None:
    """대표·공시담당자 전화 지역번호로 본 수도권 거점 (02 서울, 031 경기, 032 인천)"""
    for ph in phones or []:
        m = re.match(r"\(?0(2|31|32)\)?[-\s)]", ph.strip())
        if m:
            return {"2": "서울", "31": "경기", "32": "인천"}[m.group(1)]
    return None


def collect_corps(data: dict) -> dict:
    """corp_code → {name, cls, stock_code, addr}"""
    out = {}
    for c in data.get("companies") or []:
        out[c["corp_code"]] = {"name": c["name"], "stock_code": c.get("stock_code"),
                               "addr": (c.get("info") or {}).get("addr"),
                               "phones": [x for x in ((c.get("info") or {}).get("phone"), (c.get("contact") or {}).get("phone")) if x]}
    extra = {}
    for sec in ("holders", "lockup", "pledge"):
        for i in (data.get(sec) or {}).get("items") or []:
            cc = i.get("corp_code")
            if cc and cc not in out:
                extra[cc] = {"name": i.get("corp_name"), "stock_code": i.get("stock_code"), "addr": None}
    if extra:
        info = C.update_company_info({cc: {"watch": False} for cc in extra})
        for cc, v in extra.items():
            v["addr"] = (info.get(cc) or {}).get("addr")
        out.update(extra)
    return out


def main() -> int:
    if not C.OUT_PATH.exists():
        C.warn("data.json 이 없어 지도 좌표를 건너뜁니다.")
        return 0
    data = json.loads(C.OUT_PATH.read_text(encoding="utf-8"))
    probe = int(sys.argv[sys.argv.index("--probe") + 1]) if "--probe" in sys.argv else 0
    if probe:
        cs = [c for c in data.get("companies") or [] if (c.get("info") or {}).get("addr")]
        random.seed(7)
        lv = Counter()
        for c in random.sample(cs, min(probe, len(cs))):
            r = locate(c["info"]["addr"])
            lv[r[2] if r else 0] += 1
            C.log(f"{(r[2] if r else '실패')} | {c['name']} | {clean(c['info']['addr'])[:60]} | "
                  f"{(str(r[0]) + ',' + str(r[1])) if r else ''}")
        C.log(f"단계별: {dict(lv)}  호출: {dict(STATS)}")
        return 0

    corps = collect_corps(data)
    cache = C.load_json(GEO_PATH, {})
    new = 0
    try:
        for cc, v in corps.items():
            a = clean(v.get("addr") or "")
            hit = cache.get(a)
            # 카카오 키가 생기면 OSM 으로 찾은 주소 전부를 카카오로 다시 찾는다
            # (OSM 은 건물 단위로 찾았다고 해도 같은 도로의 다른 번지에 찍히는 경우가 있음 — 예: 종로 26 이 2km 동쪽)
            redo = bool(KAKAO_KEY and hit and hit.get("src") != "kakao" and not hit.get("kakao_tried"))
            retry = bool(hit and hit.get("miss") and hit.get("v", 1) < GEO_VER and re.search(r"[가-힣]", a))
            if not a or (hit and not redo and not retry):
                continue
            if new >= MAX_NEW:
                STATS["pending"] += 1
                continue
            C.check_time()
            if redo:
                p = parts(a)
                r = next(((*k, 1, "kakao") for q in (p["road"], p["jibun"], p["full"]) if q for k in [kakao(q)] if k), None)
                new += 1
                if r and inside_korea(r[0], r[1]):
                    cache[a] = {"lat": r[0], "lng": r[1], "lv": 1, "src": "kakao"}
                    STATS["kakao_upgraded"] += 1
                else:
                    hit["kakao_tried"] = 1
                continue
            r = locate(a)
            new += 1
            cache[a] = {"lat": r[0], "lng": r[1], "lv": r[2], "src": r[3]} if r and inside_korea(r[0], r[1]) \
                else {"miss": 1, "v": GEO_VER}
            if new % 50 == 0:
                C.save_json(GEO_PATH, cache)
    except C.OutOfTime:
        C.warn("시간 제한으로 지도 좌표 찾기를 중간에 멈췄습니다 — 다음 실행에서 이어서 찾습니다.")
    C.save_json(GEO_PATH, cache)

    # 수도권 사무소 (카카오 키가 있을 때만): 본점이 서울·경기·인천 밖인 법인
    offices = C.load_json(OFFICE_PATH, {})
    if KAKAO_KEY:
        today = C.now_kst().date()
        n_off = 0
        try:
            for cc, v in corps.items():
                a = parts(v.get("addr") or "")["full"]
                if not a or CAPITAL_RX.match(a) or not re.search(r"[가-힣]", a):
                    continue
                hit = offices.get(cc)
                if hit and hit.get("v") == OFFICE_VER and (today - C.date.fromisoformat(hit["t"])).days < OFFICE_TTL_DAYS:
                    continue
                C.check_time()
                g = cache.get(clean(v.get("addr") or "")) or {}
                offices[cc] = {"v": OFFICE_VER, "t": today.isoformat(),
                               "items": find_offices(v["name"], (g["lat"], g["lng"]) if "lat" in g else None)}
                n_off += 1
        except C.OutOfTime:
            C.warn("시간 제한으로 수도권 사무소 찾기를 중간에 멈췄습니다 — 다음 실행에서 이어서 찾습니다.")
        C.save_json(OFFICE_PATH, offices)
        STATS["office_lookups"] = n_off

    pts = {}
    for cc, v in corps.items():
        g = cache.get(clean(v.get("addr") or ""))
        if g and "lat" in g:
            pts[cc] = {"n": v["name"], "s": v.get("stock_code") or "", "a": clean(v["addr"]),
                       "lat": g["lat"], "lng": g["lng"], "lv": g["lv"]}
            o = (offices.get(cc) or {}).get("items")
            if o:
                pts[cc]["o"] = o
            pa = phone_area(v.get("phones"))
            if pa and not CAPITAL_RX.match(parts(v["addr"])["full"]):
                pts[cc]["ph"] = pa      # 본점은 지방인데 대표·공시담당 전화가 수도권 번호
    lvc = Counter(p["lv"] for p in pts.values())
    stats = {"corps": len(corps), "located": len(pts), "by_level": {str(k): v for k, v in sorted(lvc.items())},
             "no_addr": sum(1 for v in corps.values() if not v.get("addr")),
             "miss": sum(1 for v in corps.values() if (cache.get(clean(v.get("addr") or "")) or {}).get("miss")),
             "pending": STATS["pending"], "new": new, "src": "kakao" if KAKAO_KEY else "osm",
             "offices": sum(1 for p in pts.values() if p.get("o")), "phone_capital": sum(1 for p in pts.values() if p.get("ph"))}
    data["geo"] = {"corps": pts, "stats": stats}
    C.OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    C.save_json(C.ROOT / "reports" / "geo_status.json",   # 공개용: 건수만
                {"generated_at": C.now_kst().isoformat(timespec="minutes"), **stats, "calls": dict(STATS)})
    C.log(f"지도 좌표 {len(pts):,}/{len(corps):,}개사 · 단계별 {dict(lvc)} · 신규 {new} · 대기 {STATS['pending']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
