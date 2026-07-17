from dataclasses import dataclass

from src.llm.service import LLMService, collect_stream_result


@dataclass
class FakeChunk:
    """模拟 langchain AIMessageChunk 的工具调用流式分片。"""
    tool_call_chunks: list = None
    tool_calls: list = None
    content: str = ""


def test_collect_stream_result_merges_streamed_args():
    # Qwen3 场景：首块 args=None，后续块增量拼接
    chunks = [
        FakeChunk(tool_call_chunks=[{"name": "execute_sql", "args": None,
                                     "index": 0, "id": "call_1"}]),
        FakeChunk(tool_call_chunks=[{"args": '{"sql": "SELECT', "index": 0}]),
        FakeChunk(tool_call_chunks=[{"args": ' 1"}', "index": 0}]),
    ]
    result = collect_stream_result(chunks)
    assert result["name"] == "execute_sql"
    assert result["id"] == "call_1"
    assert result["arguments"] == '{"sql": "SELECT 1"}'


def test_collect_stream_result_fallback_on_empty():
    # 流式块全空，从 tool_calls[0].args 兜底
    chunks = [
        FakeChunk(tool_call_chunks=[{"name": "finish", "args": None,
                                     "index": 0, "id": "call_2"}],
                  tool_calls=[{"name": "finish", "args": {"ok": True}, "id": "call_2"}]),
    ]
    result = collect_stream_result(chunks)
    assert result["arguments"] == '{"ok": true}'


def test_collect_stream_result_no_tool_call():
    chunks = [FakeChunk(tool_call_chunks=None, content="你好")]
    result = collect_stream_result(chunks)
    assert result is None  # 无工具调用


def test_llm_service_lazy_client_no_langchain_import():
    # LLMService 实例化不应触发 langchain_openai 导入（测试环境无该包）
    import sys
    before = set(sys.modules.keys())
    from src.config import LLMConfig
    svc = LLMService()
    after = set(sys.modules.keys())
    assert "langchain_openai" not in after - before
    assert svc._client is None
