"""P0a 能力演示：用已做好的 LLMService 调 Qwen 跑一轮真实对话，验证 LLM 链路通。"""
import asyncio

from src.config import load_config
from src.llm.service import LLMService


async def main():
    cfg = load_config("config")
    # 网关当前可用模型（Qwen 系列暂无 channel，用 DeepSeek-V4-Flash；后续可换回）
    cfg.llm.model = "DeepSeek-V4-Flash"
    svc = LLMService(cfg.llm)

    question = "你好，请用一句话介绍你自己，再说一下 2 加 3 等于几。"
    print("用户：", question)
    print("Qwen：", end="", flush=True)

    # 用流式接口，逐字打印，直观看到效果
    async for chunk in svc.chat_stream([{"role": "user", "content": question}]):
        text = getattr(chunk, "content", None)
        if text:
            print(text, end="", flush=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
