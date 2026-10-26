#!/usr/bin/env python3
"""특정 법인 공시가 화면에 어떻게 들어갔는지 점검 (수동) → reports/event_check.json
   python scripts/event_check.py 법인명일부 [YYYYMMDD]"""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

q, day = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
data = json.loads(C.OUT_PATH.read_text(encoding="utf-8"))
evs = [e for e in data["events"] if q in e["corp_name"] and (not day or e["date"] == day)]
det_cache = C.load_json(C.CACHE_DIR / "details.json", {})
out = {"query": q, "day": day, "events": []}
for e in evs[:10]:
    rec = {"event": e, "cache": det_cache.get(e["rcept_no"])}
    txt, err = C.dart_document(e["rcept_no"])
    rows, corr = C.doc_tables(txt) if txt else ([], [])
    rec["doc_err"] = err
    rec["rows"] = [r for r in rows if any(c.strip() for c in r)][:70]
    rec["corr_rows"] = corr[:30]
    spec = C.CFG["details"]["specs"].get(e["category"])
    rec["reextract"] = C.extract_detail(rows, spec, corr) if spec and rows else None
    out["events"].append(rec)
C.save_json(C.ROOT / "reports" / "event_check.json", out)
print(len(evs), "events")
