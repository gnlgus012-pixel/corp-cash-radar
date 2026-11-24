#!/usr/bin/env python3
"""메자닌 납입일 점검: 공시 원문에서 '납입'·'청약'이 들어간 표 행만 출력 (회사명·날짜만, 개인 이름 없음)"""
import os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
for rn in os.environ["MZ_RCEPT"].split(","):
    txt, err = C.dart_document(rn)
    if not txt:
        print(rn, "원문 없음", err); continue
    rows, corr = C.doc_tables(txt)
    got = C.find_value(rows, "납입일", "date")
    print(f"== {rn} 추출 납입일={got} 정정대조행={len(corr)}")
    for r in rows:
        line = " | ".join(c[:50] for c in r if c)
        if re.search(r"납입|청약일|이사회결의일|발행일", C.norm(line)) and len(line) < 300:
            print("   ", line)
    for r in corr:
        if re.search(r"납입|청약", C.norm(r[0])):
            print("   [정정후]", r[0][:60], "→", r[1][:60])
