#!/usr/bin/env python3
"""주식담보 원문 형식 확인용 (일회성). 결과는 Actions artifact 로만 (개인명 포함 → 저장소에 안 남김)"""
from __future__ import annotations
import html, json, re, sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

OUT = {"major": [], "periodic": [], "exec": [], "volume": {}}
TAG = re.compile(r"<[^>]+>")


def lines_of(txt):
    t = re.sub(r"(?i)</(P|TR|TABLE|TITLE|SPAN)>", "\n", txt)
    t = html.unescape(TAG.sub(" ", t))
    return [re.sub(r"\s+", " ", x).strip() for x in t.split("\n") if x.strip()]


def ctx(lines, rx, n=6, cap=40):
    keep = set()
    for i, l in enumerate(lines):
        if rx.search(l):
            keep |= set(range(max(0, i - n), min(len(lines), i + n + 1)))
    return [lines[i][:300] for i in sorted(keep)][:cap * 3]


def lst(**kw):
    out, p = [], 1
    while True:
        j = C.dart_get("list.json", page_no=p, page_count=100, **kw)
        out += j.get("list") or []
        if p >= int(j.get("total_page") or 1) or p >= 30:
            return out, int(j.get("total_count") or 0)
        p += 1


def main():
    today = C.now_kst().date()
    hs = C.load_json(C.CACHE_DIR / "holders.json", {"docs": {}})
    pl = [d for d in hs.get("docs", {}).values() if d.get("kind") == "pledge"]
    corps = []
    for d in sorted(pl, key=lambda x: x["date"], reverse=True):
        if d["corp_code"] not in [c[0] for c in corps]:
            corps.append((d["corp_code"], d["corp_name"]))
    OUT["volume"]["pledge_docs_cached"] = len(pl)
    OUT["volume"]["pledge_corps"] = len(corps)
    # 1) 대량보유 — 최근 7일 물량 + 표본
    rows, tot = lst(bgn_de=C.ymd(today - timedelta(days=7)), end_de=C.ymd(today), pblntf_ty="D")
    nm = Counter(re.sub(r"\[.*?\]|\s", "", r["report_nm"]) for r in rows)
    OUT["volume"]["D_7d_total"] = tot
    OUT["volume"]["D_7d_names"] = nm.most_common(15)
    maj = [r for r in rows if "대량보유" in r["report_nm"]]
    ex = [r for r in rows if "소유상황보고서" in r["report_nm"]]
    # 담보 회사의 대량보유 보고서 우선
    for cc, cn in corps[:5]:
        rr, _ = lst(corp_code=cc, bgn_de=C.ymd(today - timedelta(days=365)), end_de=C.ymd(today), pblntf_ty="D")
        maj = [r for r in rr if "대량보유" in r["report_nm"]][:2] + maj
        ex = [r for r in rr if "소유상황보고서" in r["report_nm"]][:1] + ex
    seen = set()
    for r in maj[:14]:
        if r["rcept_no"] in seen: continue
        seen.add(r["rcept_no"])
        txt, err = C.dart_document(r["rcept_no"])
        if not txt:
            OUT["major"].append({"r": r, "err": err}); continue
        L = lines_of(txt)
        OUT["major"].append({"corp": r["corp_name"], "nm": r["report_nm"], "rcept": r["rcept_no"], "flr": r["flr_nm"],
                             "n_lines": len(L), "ctx": ctx(L, re.compile(r"담보|계약"), 5),
                             "tables": [x[:10] for x in C.doc_rows(txt) if re.search(r"담보|계약", " ".join(x))][:40]})
    for r in ex[:4]:
        txt, err = C.dart_document(r["rcept_no"])
        if not txt: continue
        L = lines_of(txt)
        OUT["exec"].append({"corp": r["corp_name"], "nm": r["report_nm"], "ctx": ctx(L, re.compile(r"담보|변동\s*사유|보고사유"), 4)})
    # 2) 분기·반기·사업보고서 — 담보 회사 최신 정기보고서
    for cc, cn in corps[:6]:
        rr, _ = lst(corp_code=cc, bgn_de=C.ymd(today - timedelta(days=400)), end_de=C.ymd(today), pblntf_ty="A")
        rr = [r for r in rr if re.search(r"분기보고서|반기보고서|사업보고서", r["report_nm"])]
        if not rr: continue
        r = rr[0]
        txt, err = C.dart_document(r["rcept_no"])
        if not txt:
            OUT["periodic"].append({"corp": cn, "err": err}); continue
        L = lines_of(txt)
        OUT["periodic"].append({"corp": cn, "nm": r["report_nm"], "rcept": r["rcept_no"], "mb": round(len(txt) / 1e6, 1),
                                "ctx": ctx(L, re.compile(r"(주식|지분|최대주주|특수관계).{0,40}담보|담보.{0,40}(주식|최대주주|특수관계|질권)"), 4),
                                "tables": [x[:10] for x in C.doc_rows(txt) if re.search(r"담보", " ".join(x)) and re.search(r"주식|주\b|최대주주|질권", " ".join(x))][:40]})
    # 정기보고서 물량 (최근 분기 마감 45일)
    _, tq = lst(bgn_de=C.ymd(today - timedelta(days=7)), end_de=C.ymd(today), pblntf_ty="A")
    OUT["volume"]["A_7d_total"] = tq
    Path("pledge_probe.json").write_text(json.dumps(OUT, ensure_ascii=False, indent=1), encoding="utf-8")
    C.log("probe done")


if __name__ == "__main__":
    main()
