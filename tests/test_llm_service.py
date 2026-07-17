from src.llm.service import LLMService


def test_llm_service_lazy_client():
    """LLMService 实例化不应触发 openai 导入（配置惰性，_get_client 首次调用才 import）。"""
    import sys
    before = set(sys.modules.keys())
    svc = LLMService()
    after = set(sys.modules.keys())
    assert "openai" not in after - before
    assert svc._client is None
