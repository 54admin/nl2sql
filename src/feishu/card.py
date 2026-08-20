"""飞书消息卡片构造（JSON 2.0 + CardKit streaming_mode + collapsible_panel 折叠）。

设计（过程展示 v3：无 emoji 图标、编号步骤、意图行、工具间耗时；流式清单不带 SQL，SQL 只进
done 后折叠面板；不生成图表）：
- 流式态：body 只有 [PROC 思考过程清单, ANSWER 答案] 两个元素。清单依次是
  「*下一步：意图*（斜体）+ 编号步骤行（加粗动作 · 结果 · 距上步Ns）」；思考期只有一行
  纯文本「思考中…」（无秒数、无定时刷新，不装动画）。
- done 后：全量重建一次——思考全文（截断护栏）+ 全部步骤行折进默认收起面板，
  答案在面板外；参考来源平铺（片段内嵌，不放 RAGFlow 链接——要登录态，多数人 403）。

折叠组件真名 collapsible_panel（实测真实发卡 success）；collapsible_section 报 not support tag。
"""
from __future__ import annotations

import json
import re

ANSWER_EID = "answer"
PROC_EID = "proc"

_REASONING_PANEL_CAP = 2000   # 折叠面板内思考全文截断上限（字）
_CARD_JSON_SAFE = 28000       # 整卡 JSON 安全上限（字符）：超限 card.aupdate 会被飞书拒绝，
                              # 终态折叠就出不来；超了先丢思考再截步骤，保重建请求必过


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


def _truncate_reasoning(reasoning: str) -> str:
    s = (reasoning or "").strip()
    if len(s) <= _REASONING_PANEL_CAP:
        return s
    return s[:_REASONING_PANEL_CAP] + f"…（已截断，共 {len(s)} 字）"


def progress_markdown(steps: list[dict], thinking: bool = False) -> str:
    """流式态『思考过程』清单（纯文字、无 emoji、无 SQL、无思考行/意图行——用户拍板）。
    steps: [{head 动作名, done, short 结果摘要, gap 距上一步秒数(done 时有)}]：
      N. **动作** · 结果 · Ns     ← 完成；失败为 · 失败；进行中为 · 执行中（无耗时）
    thinking 参数保留兼容但不再渲染（思考期零输出，步骤出现即反馈）。"""
    lines = ["**思考过程**"]
    for i, s in enumerate(steps, 1):
        head = f"{i}. **{s['head']}**"
        if s.get("done"):
            head += f" · {s.get('short') or '完成'}"
            if s.get("gap") is not None:
                head += f" · {s['gap']}s"
        else:
            head += " · 执行中"
        lines.append(head)
    return "\n".join(lines)


def _proc_panel(proc_items: list[tuple[str, str]], reasoning: str = "") -> dict:
    """done 后汇总折叠面板：思考链全文（截断护栏）+ 全部工具步骤折进一个默认收起面板，
    答案在面板外可见。"""
    inner: list[dict] = []
    rs = _truncate_reasoning(reasoning)
    if rs:
        inner.append({"tag": "markdown", "content": "**思考过程**\n\n" + rs})
    for _token, line in proc_items:
        inner.append({"tag": "markdown", "content": line})
    return _panel(f"思考过程（{len(proc_items)} 步）", inner)


def build_streaming_card() -> dict:
    """创建卡片实体：streaming_mode=true，body = [执行过程清单 PROC_EID, 答案 ANSWER_EID]。
    清单全程就地 acontent 更新；答案 acontent 打字机。update_multi=true 是 card_element 操作的硬性要求。"""
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
            {"tag": "markdown", "content": "**思考过程**", "element_id": PROC_EID},
            {"tag": "markdown", "content": "", "element_id": ANSWER_EID},
        ]},
    }


def _citations_markdown(citations: list[dict]) -> str:
    """参考来源 markdown：文档名 + 命中片段预览。不放 RAGFlow 链接——要 RAGFlow 登录态，
    多数人没权限点了也是 403；片段已内嵌，文档名可去 RAGFlow 搜。
    用「」包裹片段（纯字符，避开飞书卡片 markdown 对 >引用块/缩进 code 的兼容差异）；单行化截断 200 字。"""
    parts = ["**参考来源**"]
    for c in citations:
        doc = c.get("document", "未知文档")
        sim = c.get("similarity")
        sim_str = f"（相似度 {sim}）" if sim is not None else ""
        snippet = (c.get("content") or "").replace("\n", " ").strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        parts.append(f"**{doc}{sim_str}**" + (f"\n「{snippet}」" if snippet else ""))
    return "\n\n".join(parts)


def _citations_element(citations: list[dict]) -> dict:
    """参考来源元素：平铺 markdown，文档名 + 命中片段预览。"""
    return {"tag": "markdown", "content": _citations_markdown(citations)}


def build_final_card(proc_items: list[tuple[str, str]], answer: str,
                     citations: list | None = None,
                     reasoning: str = "") -> dict:
    """done 后全量替换（card.update）：思考链全文 + 工具步骤折进 collapsible_panel（默认收起，
    完整 SQL/预览表点开才见）+ hr + 答案（markdown 表格呈现数据，不生成图表）+ hr + 参考来源。
    体积护栏：整卡 JSON 超 _CARD_JSON_SAFE 先丢思考、再从尾部截步骤行，保 aupdate 必过——
    超限被拒的话终态折叠就永远出不来。答案本体不截（用户的数据）。"""
    citations = citations or []

    def _build(rs: str, items: list[tuple[str, str]]) -> dict:
        elements: list[dict] = []
        if items or rs:
            elements.append(_proc_panel(items, rs))
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": answer or "(空)", "element_id": ANSWER_EID})
        if citations:
            elements.append({"tag": "hr"})
            elements.append(_citations_element(citations))
        return {"schema": "2.0", "update_multi": True,
                "config": {"streaming_mode": False, "summary": {"content": _summary_text(answer)}},
                "body": {"elements": elements}}

    out = _build(reasoning, proc_items)
    if len(json.dumps(out, ensure_ascii=False)) <= _CARD_JSON_SAFE:
        return out
    # 一级降级：丢思考全文（步骤清单是更重要的过程记录）
    items = list(proc_items)
    out = _build("", items)
    # 二级降级：从尾部逐步丢步骤行，直到整卡回到安全体积
    while items and len(json.dumps(out, ensure_ascii=False)) > _CARD_JSON_SAFE:
        items.pop()
        out = _build("", items)
    return out


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
            {"tag": "markdown", "content": "**历史会话**——点按钮切换到对应会话："},
            {"tag": "action", "actions": actions},
        ]},
    }


if __name__ == "__main__":
    # 流式卡片：PROC+answer 两个元素
    c = build_streaming_card()
    assert c["schema"] == "2.0" and c["update_multi"] is True, "update_multi 必须为 true"
    assert len(c["body"]["elements"]) == 2, "流式卡片=执行过程+answer 两个元素"
    assert c["body"]["elements"][0]["element_id"] == PROC_EID, "首元素是执行过程清单"
    assert c["body"]["elements"][1]["element_id"] == ANSWER_EID, "末元素是 answer"
    assert c["config"]["streaming_mode"] is True, "流式态"
    # progress_markdown：无 emoji 无 SQL 无思考行/意图行；编号步骤+结果+距上一步
    pm = progress_markdown([
        {"head": "查询元数据", "done": True, "short": "2 张表", "gap": 3.0},
        {"head": "执行查询", "done": False},
    ], thinking=True)
    assert "1. **查询元数据** · 2 张表 · 3.0s" in pm, pm
    assert "2. **执行查询** · 执行中" in pm, pm
    assert "```sql" not in pm and "SELECT" not in pm, "流式清单不带 SQL（只进面板）"
    assert "思考中" not in pm and "下一步" not in pm, "无思考行/意图行"
    assert not any(ch in pm for ch in "⏳✓✗💭🔧"), "无 emoji 图标"
    fail = progress_markdown([{"head": "执行查询", "done": True, "short": "失败", "gap": 4.2}])
    assert "1. **执行查询** · 失败 · 4.2s" in fail, "失败步骤文字化"
    no_intent = progress_markdown([{"head": "检索知识库", "done": True, "short": "命中 3 段", "gap": 9}])
    assert "下一步" not in no_intent, "意图提取不出则整行不显示"
    # 最终卡片：思考+步骤折进默认收起面板，答案在外，参考来源平铺，无图表
    steps = [("code_outlined", "`14:01:02` **执行查询**\n\n```sql\nSELECT 1\n```"),
             ("data_outlined", "`14:01:05` **查询完成**：返回 5 行")]
    cit = [{"document": "运维手册.pdf", "similarity": 0.82,
            "document_id": "d1", "dataset_id": "ds1", "url": "http://x/d1",
            "content": "每日巡检应检查变桨系统油位及渗漏情况。"}]
    f = build_final_card(steps, "答案是**重点**内容", citations=cit, reasoning="想了很久的思考链")
    fe = f["body"]["elements"]
    assert fe[0]["tag"] == "collapsible_panel" and fe[0]["expanded"] is False, "首元素=默认收起面板"
    assert "思考过程（2 步）" in fe[0]["header"]["title"]["content"], "面板标题带步数"
    panel_md = "".join(e.get("content", "") for e in fe[0]["elements"])
    assert "SELECT 1" in panel_md and "想了很久的思考链" in panel_md, "SQL 与思考全文在面板内"
    ans = next(e for e in fe if e.get("element_id") == ANSWER_EID)
    assert ans["content"] == "答案是**重点**内容", "答案在面板外可见"
    assert fe[-1]["tag"] == "markdown" and "运维手册.pdf" in fe[-1]["content"], "参考来源平铺"
    assert "[运维手册.pdf](http://x/d1)" not in fe[-1]["content"], "不放 RAGFlow 链接"
    assert not any(e.get("tag") == "chart" for e in fe), "不生成图表"
    assert f["config"]["summary"]["content"] == "答案是重点内容", "summary 去 markdown"
    # 体积护栏
    big = build_final_card(steps, "答案", reasoning="思" * 60000)
    assert len(json.dumps(big, ensure_ascii=False)) <= 28000, "超长思考被压回安全体积"
    huge = [("code_outlined", "`14:01` **执行查询**\n\n```sql\nSELECT " + "x" * 600 + "\n```")] * 80
    big2 = build_final_card(huge, "答案", reasoning="思" * 30000)
    assert len(json.dumps(big2, ensure_ascii=False)) <= 28000, "步骤巨多也被压回安全体积"
    assert big2["body"]["elements"][0]["tag"] == "collapsible_panel", "降级后仍是折叠面板"
    # 会话列表卡片
    sl = build_session_list_card([{"id": "s1", "title": "t1", "created_at": "2026-07-31T10:00:00"},
                                  {"id": "s2", "title": None, "created_at": "2026-07-30T09:00:00"}], "s1")
    assert sl["config"]["streaming_mode"] is False, "会话列表卡片非流式"
    btns = sl["body"]["elements"][1]["actions"]
    assert btns[0]["value"] == {"kind": "switch", "sid": "s1"} and btns[0]["type"] == "primary"
    assert btns[1]["type"] == "default"
    print("card self-check OK ✓")
