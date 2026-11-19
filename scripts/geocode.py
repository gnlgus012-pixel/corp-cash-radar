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


def parts(addr: str) -> dict:
    """도로명(번호까지) · 지번 · 동 · 시군구 후보"""
    a = clean(addr)
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


def collect_corps(data: dict) -> dict:
    """corp_code → {name, cls, stock_code, addr}"""
    out = {}
    for c in data.get("companies") or []:
        out[c["corp_code"]] = {"name": c["name"], "stock_code": c.get("stock_code"),
                               "addr": (c.get("info") or {}).get("addr")}
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
            if not a or a in cache:
                continue
            if new >= MAX_NEW:
                STATS["pending"] += 1
                continue
            C.check_time()
            r = locate(a)
            new += 1
            cache[a] = {"lat": r[0], "lng": r[1], "lv": r[2], "src": r[3]} if r and inside_korea(r[0], r[1]) \
                else {"miss": 1}
            if new % 50 == 0:
                C.save_json(GEO_PATH, cache)
    except C.OutOfTime:
        C.warn("시간 제한으로 지도 좌표 찾기를 중간에 멈췄습니다 — 다음 실행에서 이어서 찾습니다.")
    C.save_json(GEO_PATH, cache)

    pts = {}
    for cc, v in corps.items():
        g = cache.get(clean(v.get("addr") or ""))
        if g and "lat" in g:
            pts[cc] = {"n": v["name"], "s": v.get("stock_code") or "", "a": clean(v["addr"]),
                       "lat": g["lat"], "lng": g["lng"], "lv": g["lv"]}
    lvc = Counter(p["lv"] for p in pts.values())
    stats = {"corps": len(corps), "located": len(pts), "by_level": {str(k): v for k, v in sorted(lvc.items())},
             "no_addr": sum(1 for v in corps.values() if not v.get("addr")),
             "miss": sum(1 for v in corps.values() if (cache.get(clean(v.get("addr") or "")) or {}).get("miss")),
             "pending": STATS["pending"], "new": new, "src": "kakao" if KAKAO_KEY else "osm"}
    data["geo"] = {"corps": pts, "stats": stats}
    C.OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    C.save_json(C.ROOT / "reports" / "geo_status.json",   # 공개용: 건수만
                {"generated_at": C.now_kst().isoformat(timespec="minutes"), **stats, "calls": dict(STATS)})
    C.log(f"지도 좌표 {len(pts):,}/{len(corps):,}개사 · 단계별 {dict(lvc)} · 신규 {new} · 대기 {STATS['pending']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
