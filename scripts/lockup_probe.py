"""1회성: 사람이 안 잡힌 IPO 신고서의 의무보유 표 구조 확인 (인명은 가림)"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
import lockup as L  # noqa: E402

RNS = ["20261029000504", "20261020000360", "20261016000426", "20261007000327", "20260916000234",
       "20260710000294", "20260623000235", "20260313000882", "20251210000477", "20251201000475"]
KW = re.compile(r"의무보유|보호예수|매각제한|계속보유|확약|유통")
NAME = re.compile(r"^[가-힣]{2,4}(\s*외\s*\d+\s*인)?$")
SAFE = re.compile(r"주주|대표|최대|특수|관계|본인|임원|직원|기간|개월|년|합계|소계|상장|보통주|우선주|확약|보유|제한|등기|미등기|배우자|자녀|부|모|형제|친인척|계열|기타|해당|비고|구분|성명|주식|수량|비율|일반")


def mask(c):
    c = c.strip()
    if NAME.match(c) and not SAFE.search(c):
        return "〇" * len(re.sub(r"\s*외.*", "", c)) + (re.search(r"\s*외.*", c).group(0) if "외" in c else "")
    return c[:60]


def main():
    out = []
    C.CFG["details"]["max_doc_mb"] = 60
    for rn in RNS:
        txt, err = C.dart_document(rn)
        if not txt:
            out.append({"rn": rn, "err": str(err)}); continue
        rows = [r for r in C.doc_rows(txt) if any(c.strip() for c in r)]
        flat = [L.nz(" ".join(r)) for r in rows]
        hits = [i for i, f in enumerate(flat) if KW.search(f) and len(f) < 400]
        blocks, last = [], -99
        for i in hits:
            if i <= last:
                continue
            blocks.append({"i": i, "rows": [[mask(c) for c in rows[k]] for k in range(i, min(len(rows), i + 8))]})
            last = i + 7
            if len(blocks) >= 30:
                break
        p = L.parse_ipo(rows)
        out.append({"rn": rn, "n_rows": len(rows), "ipo": p["ipo"], "n_people": len(p["people"]), "blocks": blocks})
    C.save_json(C.ROOT / "reports" / "lockup_probe.json", out)


main()
