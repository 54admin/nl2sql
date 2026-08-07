"""意图判别器：把用户问题分到 doc / data / attr 三类。

为什么需要它（代码层硬护栏，不只靠 prompt 教 LLM）：
审计实证（trace 2ef130f57334）——用户问"禾枫移交生产的缺陷有哪些"（文档类），
LLM 带着上一轮"查项目数据"的上下文惯性，把"禾枫"当项目名去 execute_sql 模糊匹配了 11 次，
全程没碰 knowledge_search。prompt 里明明写了"移交缺陷→查文档"，但 LLM 会偶发走偏。
加这道判别器：在 agent_loop 里，当问题被判为 doc 类、本轮还没调过 knowledge_search、
LLM 却去 execute_sql 时，拦下 execute_sql、回灌硬提示强制先调 knowledge_search——
不赌 LLM 判断，代码层兜底。

判别逻辑：关键词信号词匹配（纯本地，零额外 LLM 调用，不增延迟）。
- doc 类优先：命中"移交/验收/缺陷清单/制度/规定/手册/纪要/复盘/口径/政策..."等文档信号
  → 该查 knowledge_search，不该查 execute_sql。
- attr 类次之：命中"为什么/原因/怎么回事/主要原因"等归因信号 → 走 cause_text，不查知识库。
- 其余 → data 类，正常查 execute_sql。

精度优先（宁漏判不误拦）：只有明确命中文档信号才拦 execute_sql；
模糊/不含信号的问题放行，由 LLM + prompt 判断，不在代码层武断拦截。
"""
from __future__ import annotations

# 文档类信号词：用户要的是"某份文件/制度/资料里写了什么"
# （规章制度、管理办法、移交资料、验收文档、缺陷清单、会议纪要、口径说明等）
_DOC_SIGNALS = (
    # 文档载体类型
    "移交", "验收", "资料", "文档", "手册", "指南", "规程", "纪要", "复盘", "材料",
    # 制度/规定类
    "制度", "规定", "办法", "标准", "规范", "流程", "要求", "职责", "权限", "政策",
    # 说明/口径类
    "口径", "定义", "解释", "说明", "谁负责", "怎么规定",
    # 缺陷/问题清单专项（用户高频，且是查文档不是查数据）
    "缺陷清单", "问题清单", "整改", "督办",
)

# 数据类信号词：用户要的是具体数值/统计/排名
_DATA_SIGNALS = (
    "多少", "完成率", "排名", "得分", "环比", "同比", "占比", "总计", "平均",
    "最高", "最低", "最好", "最差", "明细", "汇总", "几个", "几次", "哪几个",
    "排名", "榜单",
)

# 归因类信号词：用户要的是"为什么"，走 cause_text
_ATTR_SIGNALS = (
    "为什么", "原因", "怎么回事", "主要原因", "分析原因", "为什么下降", "为什么上升",
)


def classify_intent(question: str) -> str:
    """判别用户问题意图，返回 'doc' | 'data' | 'attr'。

    doc: 查文档/制度/资料/口径（应走 knowledge_search）
    attr: 归因（走 cause_text，已查回数据推理）
    data: 查业务数据（走 execute_sql）

    doc 优先于 attr 优先于 data（文档信号最特殊，先判）。
    无任何信号命中 → data（默认走数据查询，最常见的场景）。
    """
    if any(s in question for s in _DOC_SIGNALS):
        return "doc"
    if any(s in question for s in _ATTR_SIGNALS):
        return "attr"
    if any(s in question for s in _DATA_SIGNALS):
        return "data"
    return "data"  # 无明确信号默认数据类（最高频场景），由 LLM + prompt 兜底判断


def is_doc_question(question: str) -> bool:
    """便捷谓词：是否文档类问题（agent_loop 护栏用）。"""
    return classify_intent(question) == "doc"
