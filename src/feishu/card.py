"""飞书消息卡片构造（JSON 2.0 + CardKit streaming_mode + standard_icon 原生图标）。

不用 emoji。流式期间每步过程是【独立 markdown 元素 + 对应图标】：card_element/create
insert_before 到 answer 前，各自带图标。answer 单独 acontent 打字机。
done 关流式后【全量替换兜底】——飞书 streaming 有平台超时，多轮对话一旦超时流式态会
丢过程/丢字，关流式后用 _tool_lines 重建确保最终过程+答案完整，summary 也一并更新。
"""
from __future__ import annotations

import re

ANSWER_EID = "answer"


def _icon(token: str, color: str | None = None) -> dict:
    ic = {"tag": "standard_icon", "token": token}
    if color:
        ic["color"] = color
    return ic


def _summary_text(text: str) -> str:
    """答案去 markdown 符号取前 50 字，作卡片 summary（会话列表/通知显示）。"""
    s = re.sub(r"[#*`|\[\]()_~>]", "", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:50] + "…") if len(s) > 50 else (s or "已完成")


def build_streaming_card() -> dict:
    """创建卡片实体：streaming_mode=true，body 只放 answer 元素（robot 图标）。
    update_multi=true 是 card_element/create 的硬性要求；summary 初始"生成中..."。"""
    return {
        "schema": "2.0",
        "update_multi": True,
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 40},
                "print_step": {"default": 2},
                "print_strategy": "fast",
            },
            "summary": {"content": "生成中..."},
        },
        "body": {"elements": [
            {"tag": "markdown", "content": "", "element_id": ANSWER_EID},
        ]},
    }


def proc_element(eid: str, token: str, line: str) -> dict:
    """单步过程元素：独立 markdown（card_element/create 的 elements 用）。
    token 留作接口兼容（_tool_call_line 仍返回），当前不渲染图标——用户偏好纯文本步骤。"""
    return {"tag": "markdown", "element_id": eid, "content": line}


def build_final_card(proc_items: list[tuple[str, str]], answer: str) -> dict:
    """done 后全量替换兜底：每步过程（独立图标）+ hr + 答案（succeed 图标）。
    proc_items: [(icon_token, line), ...]。summary 用答案摘要（更新"生成中..."）。"""
    elements = []
    for i, (token, line) in enumerate(proc_items):
        elements.append(proc_element(f"proc_{i}", token, line))
    if proc_items:
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": answer or "(空)", "element_id": ANSWER_EID})
    return {"schema": "2.0", "update_multi": True,
            "config": {"streaming_mode": False, "summary": {"content": _summary_text(answer)}},
            "body": {"elements": elements}}


def build_session_list_card(sessions: list[dict], current_sid: str | None) -> dict:
    """会话列表卡片（非流式）：标题 + 每会话一个「进入」按钮。
    按钮 value={kind:switch, sid}，点击触发 card.action.trigger → _on_card_sync 切会话。
    ponytail: 最多 20 条，更多再分页；标题/时间拼进按钮文案，当前会话标 ✅ 并 primary 高亮。"""
    actions = []
    for s in sessions[:20]:
        sid = s.get("id")
        title = s.get("title") or "（未命名）"
        is_cur = sid == current_sid
        created = (s.get("created_at") or "")[:16].replace("T", " ")
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text",
                     "content": f"{'✅ ' if is_cur else ''}{title}（{created}）"},
            "value": {"kind": "switch", "sid": sid},
            "type": "primary" if is_cur else "default",
        })
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "summary": {"content": "选择会话"}},
        "body": {"elements": [
            {"tag": "markdown", "content": "**📋 历史会话**——点按钮切换到对应会话："},
            {"tag": "action", "actions": actions},
        ]},
    }


if __name__ == "__main__":
    c = build_streaming_card()
    assert c["schema"] == "2.0" and c["update_multi"] is True, "update_multi 必须为 true"
    assert "icon" not in c["body"]["elements"][0], "answer 元素不带图标"
    f = build_final_card([("code_outlined", "**执行查询**"), ("data-sheet_outlined", "**完成**")], "答案是**重点**内容")
    assert "icon" not in f["body"]["elements"][0], "过程元素不带图标"
    assert f["body"]["elements"][2]["tag"] == "hr"
    assert "icon" not in f["body"]["elements"][3], "答案元素不带图标"
    assert f["config"]["summary"]["content"] == "答案是重点内容", f"summary 去markdown: {f['config']['summary']['content']}"
    sl = build_session_list_card([{"id": "s1", "title": "t1", "created_at": "2026-07-31T10:00:00"},
                                  {"id": "s2", "title": None, "created_at": "2026-07-30T09:00:00"}], "s1")
    assert sl["config"]["streaming_mode"] is False, "会话列表卡片非流式"
    btns = sl["body"]["elements"][1]["actions"]
    assert btns[0]["value"] == {"kind": "switch", "sid": "s1"} and btns[0]["type"] == "primary", "当前会话按钮 primary+switch"
    assert btns[1]["type"] == "default", "非当前会话按钮 default"
    print("card self-check OK ✓")
