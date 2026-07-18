import pytest

from src.datasource import crypto


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    """每个测试注入一个固定密钥。"""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


@pytest.mark.asyncio
async def test_roundtrip():
    enc = crypto.encrypt("p@ssw0rd")
    assert enc != "p@ssw0rd"
    assert crypto.decrypt(enc) == "p@ssw0rd"


@pytest.mark.asyncio
async def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("NL2SQL_DS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NL2SQL_DS_KEY"):
        crypto.encrypt("x")
