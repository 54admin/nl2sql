"""飞书消息卡片构造（JSON 2.0 + CardKit streaming_mode + collapsible_panel 折叠工具步骤）。

设计（用户最终拍板：折工具，不折思考）：
- 流式态：body 只有 answer 一个元素。工具步骤用 card_element/create insert_before answer
  实时往上冒（50-77s 的 run 必须有进度反馈，否则像卡死）；answer 单元素 acontent 打字机。
  思考不在流式态展示——往折叠面板里实时打字在真实链路里不可靠（占位符"(思考中…)"不更新）。
- done 后：全量重建，把所有工具步骤(+思考)折进一个默认收起的 collapsible_panel（无图标），
  答案始终在面板下方可见。这样 11 步过程也不撑爆卡片，默认收起不碍眼，想看点开即可。

折叠组件真名 collapsible_panel（实测真实发卡 success）；collapsible_section 报 not support tag。
"""
from __future__ import annotations

import re

ANSWER_EID = "answer"
PROC_EID = "proc"


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


def _panel(header: str, elements: list[dict]) -> dict:
    """极简 collapsible_panel（流式 card_element.create 与全量 card.aupdate 通用，默认收起）。
    实测：流式 insert 校验严——不能有 background_style/border/padding（报 300315 unknown property），
    只接受 tag/expanded/header.title/elements。故流式与全量态统一用此极简结构，保证两边都能发。"""
    return {"tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "markdown", "content": header}},
            "elements": elements}


def progress_markdown(titles: list[str]) -> str:
    """流式态『操作过程』清单内容：已发生的步骤逐条 ✓，单调增长。
    每个已展示步骤都是『已发生』（工具 call 已发出 / result 已返回）→ 全部 ✓；
    『正在跑』的（LLM 思考/工具执行中）还没产生事件，不在清单里——
    故无需 ⏳ 猜测，清单即『已完成』流水，干净准确。done 后再折进 collapsible_panel。"""
    if not titles:
        return "🔧 操作过程…"
    return "🔧 操作过程\n" + "\n".join(f"✓ {t}" for t in titles)


def _proc_panel(proc_items: list[tuple[str, str]], reasoning: str = "") -> dict:
    """done 后汇总折叠面板：所有工具步骤(+思考)折进一个默认收起面板，答案在面板外可见。"""
    inner: list[dict] = []
    if reasoning and reasoning.strip():
        inner.append({"tag": "markdown", "content": "**💭 思考过程**\n\n" + reasoning.strip()})
    for _token, line in proc_items:
        inner.append({"tag": "markdown", "content": line})
    return _panel(f"🔧 操作过程（{len(proc_items)} 步）", inner)


def build_streaming_card() -> dict:
    """创建卡片实体：streaming_mode=true，body = [操作过程清单 PROC_EID, 答案 ANSWER_EID]。
    过程清单全程就地 acontent 追加 ✓（不再每步 insert 折叠框）；答案 acontent 打字机。
    update_multi=true 是 card_element 操作的硬性要求。"""
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
            {"tag": "markdown", "content": "🔧 操作过程…", "element_id": PROC_EID},
            {"tag": "markdown", "content": "", "element_id": ANSWER_EID},
        ]},
    }


def build_final_card(proc_items: list[tuple[str, str]], answer: str,
                     reasoning: str = "") -> dict:
    """done 后全量替换兜底：操作过程(+思考)折进 collapsible_panel（默认收起）+ hr + 答案。
    答案始终在面板下方可见；过程多(11步)也只占一个折叠框。summary 用答案摘要。
    proc_items: [(icon_token, line), ...]；reasoning 非空时一并折进面板顶部。"""
    elements: list[dict] = []
    if proc_items or (reasoning and reasoning.strip()):
        elements.append(_proc_panel(proc_items, reasoning))
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": answer or "(空)", "element_id": ANSWER_EID})
    return {"schema": "2.0", "update_multi": True,
            "config": {"streaming_mode": False, "summary": {"content": _summary_text(answer)}},
            "body": {"elements": elements}}


def build_session_list_card(sessions: list[dict], current_sid: str | None) -> dict:
    """会话列表卡片（非流式）：标题 + 每会话一个「进入」按钮。
    按钮 value={kind:switch, sid}，点击触发 card.action.trigger → _on_card_sync 切会话。
    最多 20 条；标题/时间拼进按钮文案，当前会话标 ✅ 并 primary 高亮。"""
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
    # 流式卡片：只有一个 answer 元素（无折叠面板——思考不再流式展示）
    c = build_streaming_card()
    assert c["schema"] == "2.0" and c["update_multi"] is True, "update_multi 必须为 true"
    assert len(c["body"]["elements"]) == 2, "流式卡片=操作过程清单+answer 两个元素"
    assert c["body"]["elements"][0]["element_id"] == PROC_EID, "首元素是操作过程清单"
    assert c["body"]["elements"][1]["element_id"] == ANSWER_EID, "末元素是 answer"
    assert c["config"]["streaming_mode"] is True, "流式态"
    # progress_markdown：空=占位；有步骤=逐条 ✓
    assert progress_markdown([]) == "🔧 操作过程…", "空清单占位"
    assert progress_markdown(["查询元数据", "执行查询"]).count("✓") == 2, "每步一个 ✓"
    # 最终卡片：有步骤+思考 → 折叠面板放全部步骤，答案在面板下方
    steps = [("code_outlined", "`14:01:02` **执行查询**\n\n```sql\nSELECT 1\n```"),
             ("data-sheet_outlined", "`14:01:05` **查询完成**：返回 5 行")]
    f = build_final_card(steps, "答案是**重点**内容", reasoning="我先想想……\n第二步……")
    assert f["body"]["elements"][0]["tag"] == "collapsible_panel", "首元素是折叠面板"
    assert f["body"]["elements"][0]["expanded"] is False, "面板默认收起"
    assert "icon" not in f["body"]["elements"][0]["header"], "面板 header 不带图标（用户嫌丑）"
    inner = f["body"]["elements"][0]["elements"]
    assert "我先想想" in inner[0]["content"], "思考在面板内首个元素"
    assert any("执行查询" in e["content"] for e in inner), "工具步骤也在面板内"
    assert f["body"]["elements"][-1]["element_id"] == ANSWER_EID, "末元素是答案（面板外可见）"
    assert f["body"]["elements"][-2]["tag"] == "hr", "面板与答案间有分隔线"
    assert f["config"]["summary"]["content"] == "答案是重点内容", f"summary 去markdown: {f['config']['summary']['content']}"
    # 纯答案（无步骤无思考）：直接答案，无面板
    g = build_final_card([], "直接答", reasoning="")
    assert g["body"]["elements"][0].get("element_id") == ANSWER_EID, "无步骤时首元素直接是答案"
    # 只有步骤没有思考
    h = build_final_card(steps, "答案", reasoning="")
    assert h["body"]["elements"][0]["tag"] == "collapsible_panel", "有步骤就有面板"
    assert "**💭 思考过程**" not in h["body"]["elements"][0]["elements"][0]["content"], "无思考则面板内无思考段"
    # 会话列表卡片
    sl = build_session_list_card([{"id": "s1", "title": "t1", "created_at": "2026-07-31T10:00:00"},
                                  {"id": "s2", "title": None, "created_at": "2026-07-30T09:00:00"}], "s1")
    assert sl["config"]["streaming_mode"] is False, "会话列表卡片非流式"
    btns = sl["body"]["elements"][1]["actions"]
    assert btns[0]["value"] == {"kind": "switch", "sid": "s1"} and btns[0]["type"] == "primary", "当前会话按钮 primary+switch"
    assert btns[1]["type"] == "default", "非当前会话按钮 default"
    print("card self-check OK ✓")
