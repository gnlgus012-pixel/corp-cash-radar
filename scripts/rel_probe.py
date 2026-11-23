#!/usr/bin/env python3
"""
기업 관계도 시험 — DART 정기보고서의 '최대주주 현황'(hyslrSttus)과 '타법인 출자 현황'(otrCprInvstmntSttus)을 받아 본다.
개인 이름이 들어가므로 결과는 암호화해서만 로그에 남긴다 (공개 저장소).
  REL_CORPS="00126380,..."  python scripts/rel_probe.py
"""
import base64, gzip, json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

REPORTS = [("2026", "11014", "2026 3분기"), ("2026", "11012", "2026 반기"), ("2026", "11013", "2026 1분기"), ("2025", "11011", "2025 사업보고서")]
out = {}
for cc in [x for x in os.environ.get("REL_CORPS", "").split(",") if x]:
    out[cc] = {}
    for ep in ("hyslrSttus", "otrCprInvstmntSttus"):
        for y, rc, lab in REPORTS:
            j = C.dart_get(f"{ep}.json", corp_code=cc, bsns_year=y, reprt_code=rc)
            rows = [x for x in (j.get("list") or []) if ep != "otrCprInvstmntSttus" or (x.get("inv_prm") or "-") not in ("-", "합계")]
            if j.get("status") == "000" and rows:   # 1·3분기 보고서의 타법인 출자는 '-' 빈 줄만 오는 경우가 있음 → 다음 보고서로
                out[cc][ep] = {"report": lab, "list": j["list"]}
                C.log(f"{cc} {ep}: {lab} {len(rows)}건")   # 건수만 공개
                break
        else:
            C.log(f"{cc} {ep}: 없음")
raw = gzip.compress(json.dumps(out, ensure_ascii=False).encode())
enc = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", "env:SITE_PASSWORD"], input=raw, capture_output=True, check=True).stdout
print("ENC_BEGIN"); print(base64.b64encode(enc).decode()); print("ENC_END")
