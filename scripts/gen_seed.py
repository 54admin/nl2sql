"""从代码常量生成 db/seed.sql —— 数据库初始化种子数据。

只 seed 不含敏感信息的配置：
  * nl_cfg_skills: skill 全量元信息（SEED_SKILLS 常量 seed，DB 即唯一真相源；skills/ 目录已删除）
  * nl_cfg_ragflow: default 占位行（地址/key 空，enabled=false，待 admin 填）
  * nl_cfg_limits: default 行（agent 运行上限，纯数字配置）
含密钥的表（nl_cfg_llm/nl_cfg_datasources/nl_cfg_feishu）由用户自配，不进 seed。

用法: python3 scripts/gen_seed.py  → 生成 db/seed.sql
由 scripts/gen_seed.py 的 SEED_SKILLS 常量生成（skill 出厂种子）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SEED_SKILLS = [
    {
        "scene": 'nl2sql',
        "tools": ['query_metadata', 'execute_sql', 'get_sql_template'],
        "mode": 'always_on',
        "order": 1,
        "content": '''【问数方法论】用户问业务数据（多少/对比/排名/趋势/明细/统计）时按此走：
1. 先调 query_metadata（无参数）看数据源有哪些表、字段、表注释、表间关联（relations）。整轮对话只调一次、记住结果。绝不凭空猜表名/字段名。
2. 从返回的表里挑需要的，用 execute_sql 跑只读查询。字段取值不确定（是 0/1 还是 是/否、时间字段什么格式）时，先 SELECT DISTINCT 查一次真实取值再写主查询。
3. 拿到结果用自然语言回答。别反复试错：连续查不到就如实说明，不换着条件硬试。
- 复杂查询（同比/环比/行转列/同行多指标对比排序）先调 get_sql_template 取现成样板，按 usage 改表名/参数套用；没合适样板再自己写。

澄清与前提核查（宁可问清，不要迎合；宁可指出错误，不要顺着查）：
1. 错误前提：问题里隐含的事实断言（如"6月偏差最大""新疆排名垫底"）先用数据核实——数据不支持就直接指出，给出真实情况和依据，绝不顺着错误前提查询作答。
2. 信息缺失：缺时间范围/主体/指标口径且会改变 SQL 或结论方向的，先 ask_user 问清再查。一次问全（多个疑问合并成一次提问），不拆多轮；用户已明确给的不重复问。options 从数据查真实取值（缺时间就 SELECT DISTINCT 查时间字段最近几个、第一个=最新值标推荐；缺主体就查主体字段实际值；实体对不上就列相近表/字段），2-4 个，别凭空编；数据真查不到候选才只传 question。
3. 口径分歧：存在多种合理解释且结论会因此相反的口径必须问——如"偏差"指绝对量还是比率、"占比"是占计划还是占总量、"偏差大"含不含方向。只有低风险口径才按最常见口径直接查、回答里注明。
4. 被忽略的变量：主动提醒用户忽略的因素——未发生月份（当月之后数据为 0 会被误读成骤降）、基数差异（大小场站直接比总量不公平）、缺同比/环比基数、量纲混用（万kWh/kWh、小数/百分比）。
5. 事实与推测分开：数值/名次/结论全部来自工具返回，推断标「（推测）」；与用户预期相反的结论直接给依据，不缓和、不迎合。

回答原则：
- 工具调用轮不要写长篇中间叙述（计划/发现/排除过程）——确需说明最多一句话；完整分析只放在最终答案里，避免答案区堆半成品文字。
- 直接答用户问的，只给问的那些数据；问一个数给一个数，问一张表给那批数据，不扩展成报告、不自作主张做额外建议。
- 表格必须列全所有结果行，禁止只列前 N 截断（除非用户明确说 top N）。
- 结果为空如实说「没有查到符合条件的数据」。
- 先简述查了什么（表/口径/行数），再给数据。
- Markdown 表格列类型规范：数值（金额/比率/得分/计数）放独立数字列，纯文本（名称/描述）放描述列，禁止混放；数字列只填数字（可带 % 或单位）。
- 展示形式按问题选：单值/明细用表格；排名/对比按高低排序呈现；趋势类给时间序列。
- 期间数据一次查全（供归因同比环比）：回答涉及偏差/异常/趋势/归因收尾时，把当期、上期（上月）、去年同期数据一并查回（一张 SQL 用期间 IN 条件或行转列一次拉全），归因阶段只引用不重查。
- 本轮涉及归因收尾时，输出格式以归因方法论为准。

SQL 原则：
- 全程只读：只写 SELECT，禁止 DDL（建/改/删表）、DML（增删改数据）、危险函数。
- 字段名用原始英文：SELECT/WHERE/ORDER BY/GROUP BY 引用字段必须用 query_metadata 返回的原始英文名；AS 中文别名仅用于列头友好，绝不能在 WHERE/ORDER BY 当字段引用。
- 表名用 query_metadata 返回的原始写法（可能是 schema.table），别自己改写。
- JOIN 优先：跨表用 JOIN，优先按 relations 关联口径，别堆子查询；UNION ALL 合并结构相同的结果集；步骤多/有中间复用用 WITH/CTE；聚合起有意义的别名。
- 比率型指标（完成率/损失率/偏差率/增长率等）业务表按小数存（如 0.152 表示 15.2%），查询时必须乘 100 展示，结果表和回答文本都显示乘后值；百分点类差值同理乘 100。
- 聚合空值用 coalesce(sum(coalesce(字段,0)),0)；数字保留原始精度不四舍五入。''',
    },
    {
        "scene": 'kb_qa',
        "tools": ['knowledge_search'],
        "mode": 'always_on',
        "order": 2,
        "content": '''【知识库答疑方法论】用户问「文档/资料/制度/规定/办法/标准/手册/口径/移交资料/缺陷清单/验收记录/会议纪要/复盘」类内容时按此走——这是查【文档】，不是查业务数据。

【先判断：是查数据还是查文档】这是第一步，判断错全盘皆错：
- 查【文档】（用 knowledge_search）：用户要的是「某份文件/制度/资料里写了什么」——
  规章制度、管理办法、技术标准、操作手册、指标口径、项目移交资料、验收文档、缺陷清单、
  会议纪要、复盘材料、采购规范、图纸说明、培训资料……这类内容在「文档」里，不在业务数据表里。
  关键词信号：制度/规定/办法/标准/手册/流程/要求/清单/资料/移交/验收/纪要/复盘/说明/口径/谁负责/怎么规定的。
- 查【数据】（用 execute_sql）：用户要的是「具体数值/统计/排名/对比/趋势」——
  多少/完成率/排名/偏差/得分/环比/明细/汇总。这类在业务数据表里。

【禾枫移交生产的缺陷有哪些 —— 这是查文档不是查数据】
"移交生产的缺陷"= 项目移交资料里记录的缺陷清单，这是【文档】内容（在移交资料/验收文档里），
不是业务数据表的 overdue_defect 字段（那是运行期的超期缺陷统计，跟"移交"无关）。
→ 该用 knowledge_search，不是 execute_sql。

【怎么查】
1. 直接调 knowledge_search：query 提炼成检索关键词（实体+事项，如「禾枫 移交 缺陷」「项目移交 验收 缺陷清单」），不要带具体数值/日期。
2. 拿到文档片段后，基于片段内容回答用户；片段不足/无匹配就如实说明「知识库里没有找到相关文档」，不编造。
3. 不要为了"兜底"去 execute_sql 查数据表——文档类问题查数据表是答非所问。

【知识库可能为空】如果 knowledge_search 返回「未配置/无匹配」，直接告诉用户「知识库里还没有相关文档，请先上传」。不要转去查数据表。''',
    },
    {
        "scene": 'attribution',
        "tools": ['execute_sql'],
        "mode": 'always_on',
        "order": 3,
        "content": '''【归因方法论】铁律·必须归因：只要本轮查了业务数据（execute_sql 有返回），收尾前必须做归因——不管用户有没有问"为什么"。跳过归因直接 finish 是违规；纯闲聊/纯查文档（knowledge_search）轮次除外。

归因前自检（必做）：finish 前检查——归因素材查了吗？对比数据（上期/去年同期）有吗？缺了先补（素材查素材表，对比数据靠问数阶段已查回的期间数据），再 finish。

表分离（铁律，防串表）：
- 问数查数表：指标数据只从 ods 指标表（query_metadata 返回的业务表）取，问数阶段不碰素材表。
- 归因查归因表：归因素材只查 app.app_oper_question_attri_wide_ai，整轮最多查 1 次；查不到就如实写「数据中未提及」，不换条件重查。该表的层级过滤用法（区域→mng_area / 省分公司→operation_area / 场站→dept_name）、Btype 各记录类型含义、years/content 用法以 query_metadata 返回的表级业务规则（rules）为准，按规则查，别猜字段口径、别猜层级从属。
- 归因阶段不回头查 ods 指标表——素材没查到也不回去翻数据表找原因。

同比环比（归因必带对比）：每条归因结论必须挂在对比上——
- 环比：当期 vs 上期（上月），写方向+幅度（如"较上月下降 12%"）。
- 同比：当期 vs 去年同期，写方向+幅度。
- 对比数据由问数阶段一次查全（见问数方法论"期间数据一次查全"），归因阶段只引用与解读，不另查指标表。

归因铁律：
- 每条原因要么提炼自归因素材（≤30字，不照搬长原文），要么标注「（推测）」，两者必须区分。
- 数值/名次/排名全部来自已查回数据，绝不编造。

输出格式·会议看板汇报风格：
1. 总结先行：开头一段话直接给核心结论，关键数字嵌进句子，不铺垫、不寒暄。
2. 数字嵌入正文：关键数据写在句子里，不脱离上下文甩裸数字。
3. 分条编号：用「一、二、三、」分层次，每条 = 标题 + 数据 + 简评。
4. 对比表述写出方向（上升/下降、正/负偏差、超/欠计划），环比同比都带幅度。
5. 异常突出：偏差大、排名突出的实体单独成段，点名到具体场站/项目。
6. 简洁有力：每句带数据或结论，删过渡废话（「经过分析」「综上所述」全删）。
7. 比率换算：库里小数存储（如 0.0043）展示为业务可读形式——百分数带%（0.43%），完成率/得分按业务口径展示（115.27%）；偏差带方向与单位（+15.9个百分点、+540万kWh）。

固定骨架（缺的节可省，顺序不变）：
**【整体总结】** 一段话嵌关键数字（实际/计划/偏差/完成率/环比/同比）概括核心结论与定性。
**一、[核心指标1]** 实际/计划/偏差/完成率/环比/同比，一句话点评达标与否、变化方向。
**二、[核心指标2]** 同上。多指标多分节。
**三、异常关注与归因** 偏差最大/排名突出的实体逐条点名（场站/项目），给指标+变化+同比环比对比+归因（标「数据证实」或「推测」）。
**四、措施建议** 1-3条可落地措施，针对上面点名的问题；数据不足则省略本节。
末尾输出「该分析仅供参考」。''',
    },
    {
        "scene": 'contact_referral',
        "tools": [],
        "mode": 'always_on',
        "order": 4,
        "content": '''【联系人推荐·计划经营组】当用户问题涉及以下职责范畴时，在回答末尾自然地附一句「详细信息可联系 XXX（XX小组）」。

经营分析小组：
- 董芳丽：年度盈利测算与对接、经营偏差分析（快报/管报）、目标责任书制定、组织绩效考核、激励申请与分配、任务卡制定与管控、机制竞价全流程管控
- 黄薪屹：综合计划大纲、战略规划、总办会及经营分析总结会材料、滚动预测、中心BP统一对外数据答复
- 张放：会议管理（中心周例会/季度会等）、全层级事项闭环督办、重点任务及精益项目管理、经营简报统筹、低效项目（含减值）治理、长周期预测台账管理
- 孙晶：标准体系、组织职能编制、分层授权修订、内外审及问题整改闭环
- 裴丽媛：协助分层授权及内外审、风控归口、流程平台管理

计划业务小组：
- 惠姬：年度及中长期电量计划、对标管理
- 张娇：年度生产业务计划、人/车/项目等台账管理、技改大修/年度定检/其他专项计划进度管控
- 曲潇萌：定额体系修编及应用、成本预算编制及管控、成本分析、平台预算编制及管控、低效往来款清理推进、生态工资分摊
- 李曦：企业文化活动、资管合同管理、固定资产盘点、宁波资质办理、海南办公室日常管理、辅助生产管理部相关工作

规则：
1. 用户问题明显沾上述某人的职责范畴（如问"标准体系""成本预算""分层授权""电量计划"等），才在回答末尾加「详细信息可联系 {姓名}（{小组}）」。
2. 一句话带过即可，不要展开介绍职责、不要单列区块。
3. 职责跨多人时，推荐最相关的一位；拿不准归属就不加，绝不乱推荐。
4. 纯闲聊、与上述职责无关的问题，不加。''',
    },
]


HEADER = """-- ============================================================
-- nl2sql 数据库种子数据 —— 新环境初始化（与 schema.sql 配套）
-- ------------------------------------------------------------
-- 只含不含密钥的配置：skill 提示词 / RAGFlow 占位 / agent 上限。
-- 含密钥的表（nl_cfg_llm/nl_cfg_datasources/nl_cfg_feishu）由用户自配，不在本文件。
-- 全部 ON CONFLICT 幂等，可重复跑。由 scripts/gen_seed.py 的 SEED_SKILLS 常量生成（skills/ 目录已删除）。
-- ============================================================

BEGIN;

"""


def _pg_str(s: str) -> str:
    """PG 单引号字符串字面量：单引号 → 两个单引号。"""
    return "'" + s.replace("'", "''") + "'"



def _pg_json(obj) -> str:
    """PG json 字面量：python obj → json 文本 + ::json 强转。"""
    import json
    return "'" + json.dumps(obj, ensure_ascii=False).replace("'", "''") + "'::json"

def generate() -> str:
    parts = [HEADER]

    # --- 1) nl_cfg_skills: skill 单一真相源种子（content+tools+mode+order 全量，仅首次灌库）
    # 之后运行时只读 DB。ON CONFLICT DO NOTHING 保护 admin 已改内容。
    skills = {sk["scene"]: sk for sk in SEED_SKILLS}
    parts.append("-- skill 种子（全量元信息；首次灌库即权威，admin 改后 ON CONFLICT 不覆盖）\n")
    for name in sorted(skills, key=lambda n: skills[n]["order"]):
        skill = skills[name]
        content = skill["content"]
        tools = skill["tools"]
        mode = skill["mode"]
        order = skill["order"]
        parts.append(
            f'INSERT INTO nl_cfg_skills (scene, content, tools, mode, "order", version, enabled) VALUES (\n'
            f'    {_pg_str(name)}, {_pg_str(content)}, {_pg_json(tools)}, '
            f'{_pg_str(mode)}, {order}, 1, true\n'
            f') ON CONFLICT (scene) DO NOTHING;\n')
    parts.append("\n")

    # --- 2) nl_cfg_ragflow: default 占位（地址/key 空，enabled=false，待 admin 填）---
    parts.append(
        "-- RAGFlow 知识库 default 占位（地址/key 空、禁用；admin 后台填真实值并启用）\n"
        "INSERT INTO nl_cfg_ragflow (id, base_url, api_key, dataset_ids, top_k,\n"
        "    similarity_threshold, vector_similarity_weight, enabled, version)\n"
        "VALUES ('default', '', '', '[]'::json, 5, 0.2, 0.3, false, 1)\n"
        "ON CONFLICT (id) DO NOTHING;\n\n")

    # --- 3) nl_cfg_limits: default 运行上限 ---
    parts.append(
        "-- agent 运行上限 default（admin 后台可改）\n"
        "INSERT INTO nl_cfg_limits (id, max_turns, max_ask_user, max_sql,\n"
        "    max_sql_fail_streak, max_meta_per_run, version)\n"
        "VALUES ('default', 30, 4, 10, 3, 1, 1)\n"
        "ON CONFLICT (id) DO NOTHING;\n")

    parts.append("\nCOMMIT;\n")
    return "".join(parts)


if __name__ == "__main__":
    out = Path("db/seed.sql")
    out.parent.mkdir(exist_ok=True)
    out.write_text(generate(), encoding="utf-8")
    print(f"wrote {out} ({len(SEED_SKILLS)} skills + nl_cfg_ragflow + nl_cfg_limits)")
