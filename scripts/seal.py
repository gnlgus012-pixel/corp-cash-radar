#!/usr/bin/env python3
"""
화면 데이터 암호화 / 복호화

  python scripts/seal.py seal docs/data/data.json docs/data/data.enc   # 암호화 후 원본 삭제
  python scripts/seal.py open docs/data/data.enc  docs/data/data.json  # 복호화 (누락 점검 등 내부용)

비밀번호는 환경변수 SITE_PASSWORD (GitHub Secrets). 없으면 암호화하지 않고 원본을 그대로 둔다.

개인 계정 (선택): 환경변수 SITE_USERS 에 한 줄에 하나씩 "아이디:비밀번호".
  각 계정의 비밀번호로 사이트 키를 감싼 docs/data/keys.json 을 만든다 (아이디는 해시로만 저장).
  화면에서는 아이디+비밀번호로 열고, 접속 기록에 그 아이디가 남는다.
접속 기록 (선택): 환경변수 VISIT_LOG_URL (구글 Apps Script 웹앱 주소) — 암호화된 데이터 안에만 넣는다.

파일 형식: b"RDR1" + salt(16) + iv(12) + AES-256-GCM( gzip(JSON) )
키: PBKDF2-HMAC-SHA256(비밀번호, salt, 250,000회) — 화면(index.html)의 복호화와 같은 방식.
salt 는 고정값이라 '이 기기에서 기억'으로 저장한 키가 매일 새 파일에도 그대로 맞는다 (파일마다 iv는 새로 생성).
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
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


def user_id_hash(uid: str) -> str:
    return hashlib.sha256(("radar-user:" + uid.strip().lower()).encode("utf-8")).hexdigest()[:24]


def parse_users(text: str) -> dict[str, str]:
    users = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        uid, sep, pw = ln.partition(":")
        if not sep or not uid.strip() or not pw:
            print(f"::warning::SITE_USERS 형식 오류 줄 무시 (아이디:비밀번호 형식이어야 함)")
            continue
        users[uid.strip()] = pw
    return users


def write_keys(dst: Path, site_key: bytes, users: dict[str, str]) -> None:
    """각 개인 비밀번호로 사이트 키를 감싸 저장. 아이디별 salt 는 아이디 해시에서 만든다."""
    out = {}
    for uid, pw in users.items():
        h = user_id_hash(uid)
        salt = hashlib.sha256(("radar-salt:" + h).encode()).digest()[:16]
        kek = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw.encode("utf-8"))
        iv = os.urandom(12)
        wrapped = AESGCM(kek).encrypt(iv, json.dumps({"k": base64.b64encode(site_key).decode(), "u": uid}).encode(), None)
        out[h] = base64.b64encode(iv + wrapped).decode()
    dst.write_text(json.dumps({"v": 1, "users": out}, separators=(",", ":")), encoding="utf-8")
    print(f"개인 계정 {len(out)}개 키 저장: {dst}")


def seal(src: Path, dst: Path, password: str) -> None:
    body = src.read_bytes()
    log_url = os.environ.get("VISIT_LOG_URL", "").strip()
    if log_url:
        d = json.loads(body)
        d["visit_log_url"] = log_url
        body = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw = gzip.compress(body, compresslevel=9)
    iv = os.urandom(12)
    ct = AESGCM(derive(password)).encrypt(iv, raw, None)
    dst.write_bytes(MAGIC + SALT + iv + ct)
    src.unlink()
    keys = dst.parent / "keys.json"
    users = parse_users(os.environ.get("SITE_USERS", ""))
    if users:
        write_keys(keys, derive(password), users)
    elif keys.exists():
        keys.unlink()
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
