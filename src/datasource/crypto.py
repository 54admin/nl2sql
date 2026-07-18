"""数据源密码加解密：Fernet 对称加密，密钥走环境变量 NL2SQL_DS_KEY。
密钥用 Fernet.generate_key() 生成（44 字节 base64），放环境变量，不入库不入 git。"""
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = os.environ.get("NL2SQL_DS_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 NL2SQL_DS_KEY（数据源密码加密密钥，"
                           "用 Fernet.generate_key() 生成）")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plain: str) -> str:
    """明文 → Fernet 密文（str）。"""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Fernet 密文 → 明文。"""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
