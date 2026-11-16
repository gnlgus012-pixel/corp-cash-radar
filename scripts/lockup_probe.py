"""1회성: 상장 전(추정)으로 남은 IPO 묶음의 신고서별 납입일 확인 (회사명·접수번호만)"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
import lockup as L  # noqa: E402

store = C.load_json(L.STORE_PATH, {"docs": {}})
docs = store["docs"]
want = {"유캐스트", "케이엠에프", "마더스제약", "데스틴파워", "하나에어로다이내믹스", "태석광학", "엠비디"}
out = []
for cl in L.ipo_clusters(docs):
    if docs[cl[0]]["corp_name"] not in want:
        continue
    rows = []
    for rn in cl:
        d = docs[rn]; p = d.get("parsed") or {}
        rows.append({"rn": rn, "nm": d.get("report_nm"), "date": d["date"], "pv": d.get("pv"), "ipo": p.get("ipo"),
                     "pay": p.get("pay_date"), "n": len(p.get("people") or []), "tries": d.get("tries")})
    out.append({"corp": docs[cl[0]]["corp_name"], "docs": rows})
# 정정 신고서 원문에서 납입기일 주변 행
for o in out:
    rn = o["docs"][0]["rn"]
    txt, err = C.dart_document(rn)
    if not txt:
        o["err"] = str(err); continue
    rr = [r for r in C.doc_rows(txt) if any(c.strip() for c in r)]
    hits = [i for i, r in enumerate(rr) if re.search(r"납\s*입\s*(기\s*일|일)", " ".join(r))][:6]
    o["pay_rows"] = [[c[:40] for c in rr[k]] for i in hits for k in range(i, min(len(rr), i + 3))]
C.save_json(C.ROOT / "reports" / "lockup_probe.json", out)
