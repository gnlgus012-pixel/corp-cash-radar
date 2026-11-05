#!/usr/bin/env python3
"""
상장 주관사 — IPO 증권신고서(지분증권)의 '인수인 정보' (lockup.py 다음)

  대상 : lockup.py 가 모아 둔 IPO 신고서 목록(cache/lockup.json, 최근 3년여) — 회사별 최신 신고서 묶음
  출처 : OpenDART 증권신고서 주요정보 › 지분증권 (estkRs.json) 의 인수인 그룹
         → 대표주관회사 · 공동주관회사 · 인수회사 / 인수 주식수 · 금액
  결과 : data["underwriter"] = {corp_code: {lead: [...], co: [...], under: [...], rcept_no, date}}
         reports/underwriter_status.json (회사명·증권사명만)

--probe : 응답 구조(그룹 제목·키·첫 행)를 reports/underwriter_probe.json 에 남김 (증권사·회사명만 있는 표)
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
import lockup as L  # noqa: E402

STORE_PATH = C.CACHE_DIR / "underwriter.json"
VER = "u1"
PROBE = "--probe" in sys.argv

ROLE_RX = re.compile(r"대표주관|공동주관|주관회사|인수회사|인수단|^인수인?$|인수$")
FIRM_RX = re.compile(r"증권|투자|금융|Securities|SECURITIES|캐피탈|㈜|\(주\)|주식회사")


def norm_firm(s: str) -> str:
    s = re.sub(r"㈜|\(주\)|주식회사|\s+", "", s or "")
    return s.strip()


def pick(rows: list[dict]) -> dict:
    """인수인 표 행 → 역할별 증권사 (키 이름에 기대지 않고 값으로 판단)"""
    out = {"lead": [], "co": [], "under": []}
    for r in rows:
        vals = [str(v).strip() for v in r.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        role = next((v for v in vals if ROLE_RX.search(re.sub(r"\s", "", v))), "")
        firm = next((v for v in vals if v != role and FIRM_RX.search(v) and not re.fullmatch(r"[\d,.\-]+", v)), "")
        firm = norm_firm(firm)
        if not firm:
            continue
        r2 = re.sub(r"\s", "", role)
        key = "lead" if "대표" in r2 else "co" if "공동" in r2 else "under"
        if firm not in out[key]:
            out[key].append(firm)
    # 대표주관회사가 따로 없으면 '주관회사'를 대표로
    if not out["lead"] and out["co"]:
        out["lead"], out["co"] = out["co"][:1], out["co"][1:]
    return out


def underwriter_groups(j: dict) -> dict:
    """estkRs 응답 → {rcept_no: [인수인 행]}"""
    res = {}
    for g in j.get("group") or []:
        if "인수" not in (g.get("title") or ""):
            continue
        for r in g.get("list") or []:
            res.setdefault(r.get("rcept_no", ""), []).append(r)
    return res


def main() -> int:
    if not C.DART_KEY:
        return 1
    lk = C.load_json(L.STORE_PATH, {"docs": {}})
    docs = lk.get("docs", {})
    store = C.load_json(STORE_PATH, {"corps": {}})
    corps = store.setdefault("corps", {})
    probe = []
    stats = Counter()
    try:
        for cl in L.ipo_clusters(docs):
            head = docs[cl[0]]
            hp = head.get("parsed") or {}
            if head.get("pv") == L.PARSE_VER and not hp.get("ipo"):
                continue                       # 상장 공모가 아닌 신고서 (유상증자 등)
            cc = head["corp_code"]
            if corps.get(cc, {}).get("v") == VER and corps[cc].get("rcept_no") == cl[0]:
                continue
            if stats["calls"] >= 1500 or (PROBE and stats["calls"] >= 4):
                break
            lo = min(docs[r]["date"] for r in cl)
            j = C.dart_get("estkRs.json", corp_code=cc, bgn_de=lo, end_de=head["date"])
            stats["calls"] += 1
            if PROBE:
                probe.append({"corp": head["corp_name"], "rcept": cl[0], "status": j.get("status"),
                              "groups": [{"title": g.get("title"), "keys": sorted((g.get("list") or [{}])[0].keys()),
                                          "rows": (g.get("list") or [])[:4]}
                                         for g in j.get("group") or [] if "인수" in (g.get("title") or "")]})
            groups = underwriter_groups(j)
            src = next((r for r in cl if groups.get(r)), None) or next(iter(groups), None)
            if not src:
                stats["empty"] += 1
                corps[cc] = {"v": VER, "rcept_no": cl[0], "lead": [], "co": [], "under": [], "date": head["date"],
                             "corp_name": head["corp_name"]}
                continue
            got = pick(groups[src])
            corps[cc] = {"v": VER, "rcept_no": cl[0], "src": src, "date": head["date"],
                         "corp_name": head["corp_name"], **got}
            stats["ok" if got["lead"] else "no_lead"] += 1
            if stats["calls"] % 50 == 0:
                C.save_json(STORE_PATH, store)
    except (C.OutOfTime, C.DartError) as ex:
        C.warn(f"주관사 수집 중단 ({ex.__class__.__name__}) — 다음 실행에서 이어서")
    C.save_json(STORE_PATH, store)
    if PROBE:
        C.save_json(C.ROOT / "reports" / "underwriter_probe.json", probe)
    out = {cc: {k: v[k] for k in ("lead", "co", "under", "date", "rcept_no")} for cc, v in corps.items() if v.get("lead")}
    lead_cnt = Counter(f for v in out.values() for f in v["lead"])
    C.save_json(C.ROOT / "reports" / "underwriter_status.json", {
        "generated_at": C.now_kst().isoformat(timespec="minutes"), "corps": len(out), "known": len(corps),
        "stats": stats, "lead_top": lead_cnt.most_common(20)})
    if C.OUT_PATH.exists():
        data = json.loads(C.OUT_PATH.read_text(encoding="utf-8"))
        data["underwriter"] = out
        C.OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    C.log(f"상장 주관사 {len(out)}개사")
    return 0


if __name__ == "__main__":
    sys.exit(main())
