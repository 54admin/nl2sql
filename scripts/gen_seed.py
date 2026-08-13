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
        "content": '【问数方法论】用户问业务数据（多少/对比/排名/趋势/明细/统计）时按此走：\n1. 先调 query_metadata（无参数）看数据源有哪些表、字段、表注释、表间关联（relations）。整轮对话只调一次、记住结果。绝不凭空猜表名/字段名——编了必报错。\n2. 从返回的表里挑需要的，用 execute_sql 跑只读查询。字段取值不确定（是 0/1 还是 是/否、时间字段什么格式）时，先 SELECT DISTINCT 查一次真实取值再写主查询，别瞎猜导致查空。\n3. 拿到结果用自然语言回答。execute_sql 别反复试错：一次写对最好，连续查不到就如实说明，不换着条件硬试。\n- 复杂查询（同比/环比/行转列/同行多指标对比排序）先调 get_sql_template 取现成样板，按 usage 改表名/参数套用；没合适样板再自己写，写完核对列名/聚合口径。\n\n澄清（候选从数据查、动态给，别写死别默认）：\n用户问题缺关键信息时先 ask_user 澄清：缺时间就 SELECT DISTINCT 查时间字段真实取值，把最近几个作 options（第一个=最新值标推荐）；缺主体/筛选对象就查主体字段实际值作 options；实体名对不上就把 query_metadata 里相近的表/字段列给用户选。用户已明确给的不重复问。统计口径/对比基准不追问——按最常见口径直接查、回答里说明。ask_user 尽量带 options（2-4 个），候选一律从数据查真实取值，别凭空编、别写死「本月/上月」；只有数据里真查不到候选（纯开放信息）才只传 question。\n\n回答原则：\n- 直接答用户问的，只给问的那些数据，不自作主张做要点总结/分点罗列/额外建议。\n- 表格必须列全所有结果行，禁止只列前 5/前 N 截断——查回几行列几行（除非用户明确说 top N）。\n- 问一个数给一个数，问一张表给那批数据，别扩展成报告。\n- 结果为空如实说「没有查到符合条件的数据」，不编造。\n- 先简述查了什么（表/口径/行数），再给数据。\n- Markdown 表格列类型规范：数值（金额/比率/得分/计数等）一律放独立的数字列，纯文本（名称/描述/说明等）放描述列。禁止把数值混进描述性列里（如「损失率 15.2%」整体放一个列），也禁止把文字描述混进数字列。每列只放一种数据类型——数字列只填数字（可带 % 或单位），描述列只填文字。\n- 数据解读要克制：回答里只复述查回的真实数值，绝不编造未查询出的数字/名次/排名。对结果做业务解读时，只说「数据支撑什么」，不臆测数据里没有的结论。\n- 展示形式按问题选：单值/明细用表格；排名/对比类适合时按高低排序呈现；趋势类（近N月变化）给时间序列；用户问「为什么/原因」类走归因方法论、不做表格堆砌。\n\nSQL 原则：\n- 全程只读：只写 SELECT，禁止 DDL（建/改/删表）、DML（增删改数据）、危险函数。\n- 字段名用原始英文：SELECT/WHERE/ORDER BY/GROUP BY 引用字段必须用 query_metadata 返回的原始英文名（如 code/swdl）。AS 中文别名仅用于列头友好，绝不能在 WHERE/ORDER BY 当字段引用——中文别名不是真字段，拿来查必报错。\n- 全限定名：表名用 query_metadata 返回的原始写法（可能是 schema.table），别自己改写。\n- JOIN 优先：跨表用 JOIN，优先按 relations 关联口径；relations 为空按字段注释推导主外键，用 INNER/LEFT JOIN，别堆子查询。\n- UNION/UNION ALL 合并结构相同的结果集（优先 UNION ALL 更快）；步骤多/有中间复用用 WITH/CTE；聚合 SUM/COUNT/AVG 起有意义的别名。\n- 百分比/比率/完成率/损失率/增长率等比率型指标：业务表中小数存储（如 0.152 表示 15.2%），查询时必须乘 100 展示（如 loss_rate*100 AS 损失率）。SQL 里直接 *100，结果表和回答文本都显示乘后的值（如 15.2 而非 0.152）。百分点类差值也同理乘 100。\n- 聚合空值用 coalesce(sum(coalesce(字段,0)),0)；数字保留原始精度不四舍五入（乘 100 后同理不四舍五入）。',
    },
    {
        "scene": 'attribution',
        "tools": ['knowledge_search'],
        "mode": 'always_on',
        "order": 2,
        "content": '【归因方法论】用户问「为什么/异常/波动/情况如何/主要情况/主要原因」时，做归因分析汇报。\n\n【铁律1·绝对禁止查库】归因阶段绝对不调 execute_sql，全部复用问数阶段已查回的数据。knowledge_search 最多1次、仅作文档佐证，无匹配就停，不换词重查。\n【铁律2·绝不编数】回答中每个数值/名次/排名都必须能在「已查回数据」里找到出处。数据里没有的，写「数据中未提及」，绝不凭印象补、绝不用「大概/约/左右」。\n【铁律3·归因要标据】每条原因要么提炼自归因文本（≤30字，不照搬长原文），要么标注「（推测）」。两者必须区分。\n\n【输出文案·会议看板汇报风格·硬约束】\n1. 总结先行：开头一段话直接给核心结论，关键数字嵌进句子里，不铺垫、不寒暄。\n2. 数字嵌入正文：关键数据写在句子里，不甩裸表格、不脱离上下文列数字。\n3. 分条编号：用「一、二、三、」分层次，每条 = 标题 + 数据 + 简评。\n4. 对比表述：环比/同比/与计划偏差，必须写出方向（上升/下降、正/负偏差、超/欠计划）。\n5. 异常突出：偏差大、排名突出的，单独成段，点名到具体场站/项目。\n6. 简洁有力：每句带数据或结论，禁过渡废话（「经过分析」「综上所述」「值得一提的是」全删）。\n7. 比率展示规则：数据库里比率型指标（限电率/完成率/损失率等）按小数存（如0.0043），汇报时必须换算成业务可读形式——百分数带%（写成0.43%），完成率/得分等按业务口径展示（115.27%）。偏差带方向与单位（+15.9个百分点、+540万kWh）。\n\n固定骨架（缺的节可省，但顺序不变）：\n**【整体总结】** 一段话嵌关键数字（实际/计划/偏差/完成率/排名/得分）概括核心结论与定性。\n**一、[核心指标1]** 实际/计划/偏差/完成率/环比，一句话点评达标与否、变化方向。\n**二、[核心指标2]** 同上。多指标多分节。\n**三、异常关注** 偏差最大/排名突出的实体逐条点名（场站/项目），给指标+变化+归因（标「数据证实」或「推测」）。\n**四、措施建议** 1-3条可落地措施，针对上面点名的问题；数据不足则省略本节，绝不编造。\n末尾输出「该分析仅供参考」。',
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
        "VALUES ('default', 30, 2, 10, 3, 1, 1)\n"
        "ON CONFLICT (id) DO NOTHING;\n")

    parts.append("\nCOMMIT;\n")
    return "".join(parts)


if __name__ == "__main__":
    out = Path("db/seed.sql")
    out.parent.mkdir(exist_ok=True)
    out.write_text(generate(), encoding="utf-8")
    print(f"wrote {out} ({len(SEED_SKILLS)} skills + nl_cfg_ragflow + nl_cfg_limits)")
