#!/usr/bin/env python3
"""메자닌 일정 점검: 원문에서 일정 관련 행만 출력 (발행대상자 행 제외 — 공개 로그)"""
import os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402
for rn in os.environ["MZ_RCEPT"].split(","):
    txt, err = C.dart_document(rn)
    if not txt:
        print(rn, "원문 없음"); continue
    rows, corr = C.doc_tables(txt)
    print(f"== {rn}")
    for r in rows:
        line = " | ".join(c[:45] for c in r if c)
        n = C.norm(line)
        if "납입능력" in n or "발행대상" in n or len(line) > 260:
            continue
        if re.search(r"납입일|청약일|청구기간|행사기간|교환청구|시작일|종료일|만기일|이사회결의일\(", n):
            print("   ", line)
    for r in corr:
        if re.search(r"납입|청구|행사|교환|시작|만기", C.norm(r[0])):
            print("   [정정후]", r[0][:50], "→", r[1][:40])
