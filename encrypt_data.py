# -*- coding: utf-8 -*-
"""
把 docs/data.json 加密成 docs/data.enc.txt（AES-GCM + PBKDF2），
网页端用访问密码经 WebCrypto 解密后渲染 —— 实现"密码门"。

密码只来自环境变量 PAGE_PASSWORD（GitHub Secret）。
本文件位于公开仓库，**严禁在此写默认密码**，否则密码门失效。
"""
import os
import json
import base64
import hashlib
import sys

HERE = os.getcwd()   # 以运行目录为准（docs/data.json 相对当前目录）
SRC = os.path.join(HERE, 'docs', 'data.json')
DST = os.path.join(HERE, 'docs', 'data.enc.txt')


def encrypt_bytes(key: bytes, plaintext: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    return iv, ct


def main():
    pwd = (os.environ.get('PAGE_PASSWORD') or '').strip()
    if not pwd:
        print('错误：未设置环境变量 PAGE_PASSWORD（页面访问密码）。'
              '请在仓库 Secrets 中配置，勿使用默认值。', file=sys.stderr)
        sys.exit(1)
    with open(SRC, 'rb') as f:
        plaintext = f.read()

    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), salt, 100000, dklen=32)
    iv, ct = encrypt_bytes(key, plaintext)   # 不用 AAD，保证 WebCrypto 可解

    blob = {
        'v': 1,
        'kdf': 'PBKDF2-SHA256',
        'iter': 100000,
        'salt': base64.b64encode(salt).decode(),
        'iv': base64.b64encode(iv).decode(),
        'ct': base64.b64encode(ct).decode(),
    }
    with open(DST, 'w', encoding='utf-8') as f:
        json.dump(blob, f)
    print(f'data.enc.txt 已生成：{len(plaintext)} 字节明文 -> {os.path.getsize(DST)} 字节密文')


if __name__ == '__main__':
    main()
