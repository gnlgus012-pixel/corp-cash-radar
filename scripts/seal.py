#!/usr/bin/env python3
"""
화면 데이터 암호화 / 복호화

  python scripts/seal.py seal docs/data/data.json docs/data/data.enc   # 암호화 후 원본 삭제
  python scripts/seal.py open docs/data/data.enc  docs/data/data.json  # 복호화 (누락 점검 등 내부용)

비밀번호는 환경변수 SITE_PASSWORD (GitHub Secrets). 없으면 암호화하지 않고 원본을 그대로 둔다.

파일 형식: b"RDR1" + salt(16) + iv(12) + AES-256-GCM( gzip(JSON) )
키: PBKDF2-HMAC-SHA256(비밀번호, salt, 250,000회) — 화면(index.html)의 복호화와 같은 방식.
salt 는 고정값이라 '이 기기에서 기억'으로 저장한 키가 매일 새 파일에도 그대로 맞는다 (파일마다 iv는 새로 생성).
"""
from __future__ import annotations

import gzip
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"RDR1"
SALT = b"corp-cash-radar1"   # 16 bytes, 고정 (index.html 과 동일)
ITER = 250_000


def derive(password: str) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=ITER).derive(password.encode("utf-8"))


def seal(src: Path, dst: Path, password: str) -> None:
    raw = gzip.compress(src.read_bytes(), compresslevel=9)
    iv = os.urandom(12)
    ct = AESGCM(derive(password)).encrypt(iv, raw, None)
    dst.write_bytes(MAGIC + SALT + iv + ct)
    src.unlink()
    print(f"암호화 완료: {dst} ({len(raw) / 1e6:.2f}MB 압축 → {dst.stat().st_size / 1e6:.2f}MB), 원본 삭제")


def open_(src: Path, dst: Path, password: str) -> None:
    b = src.read_bytes()
    assert b[:4] == MAGIC, "형식이 다름"
    salt, iv, ct = b[4:20], b[20:32], b[32:]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(password.encode("utf-8"))
    dst.write_bytes(gzip.decompress(AESGCM(key).decrypt(iv, ct, None)))
    print(f"복호화 완료: {dst}")


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("seal", "open"):
        print(__doc__)
        return 2
    pw = os.environ.get("SITE_PASSWORD", "")
    src, dst = Path(sys.argv[2]), Path(sys.argv[3])
    if not pw:
        msg = "SITE_PASSWORD 가 없어 암호화하지 않았습니다 — 사이트가 비밀번호 없이 공개 상태입니다."
        print(msg)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=암호화 안 됨::{msg}")
        return 0 if sys.argv[1] == "seal" else 1
    if sys.argv[1] == "seal":
        if not src.exists():
            print(f"{src} 없음 — 건너뜀")
            return 0
        seal(src, dst, pw)
    else:
        open_(src, dst, pw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
