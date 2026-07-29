"""飞书机器人通道适配器（旁路接入，不碰 HTTP/SSE 主链路）。

WebSocket 长连接收消息 → orchestrator 事件流 → 飞书卡片流式输出。"""
from src.feishu.adapter import FeishuAdapter

__all__ = ["FeishuAdapter"]
