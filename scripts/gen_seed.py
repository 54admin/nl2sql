"""从代码常量生成 db/seed.sql —— 数据库初始化种子数据。

只 seed 不含敏感信息的配置：
  * prompts: skill 全量元信息（SEED_SKILLS 常量 seed，DB 即唯一真相源；skills/ 目录已删除）
  * ragflow_config: default 占位行（地址/key 空，enabled=false，待 admin 填）
  * agent_limits: default 行（agent 运行上限，纯数字配置）
含密钥的表（llm_config/datasources/feishu_config）由用户自配，不进 seed。

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
        "content": '【问数方法论】用户问业务数据（多少/对比/排名/趋势/明细/统计）时按此走：\n1. 先调 query_metadata（无参数）看数据源有哪些表、字段、表注释、表间关联（relations）。整轮对话只调一次、记住结果。绝不凭空猜表名/字段名——编了必报错。\n2. 从返回的表里挑需要的，用 execute_sql 跑只读查询。字段取值不确定（是 0/1 还是 是/否、时间字段什么格式）时，先 SELECT DISTINCT 查一次真实取值再写主查询，别瞎猜导致查空。\n3. 拿到结果用自然语言回答。execute_sql 别反复试错：一次写对最好，连续查不到就如实说明，不换着条件硬试。\n- 复杂查询（同比/环比/行转列/同行多指标对比排序）先调 get_sql_template 取现成样板，按 usage 改表名/参数套用；没合适样板再自己写，写完核对列名/聚合口径。\n\n澄清（候选从数据查、动态给，别写死别默认）：\n用户问题缺关键信息时先 ask_user 澄清：缺时间就 SELECT DISTINCT 查时间字段真实取值，把最近几个作 options（第一个=最新值标推荐）；缺主体/筛选对象就查主体字段实际值作 options；实体名对不上就把 query_metadata 里相近的表/字段列给用户选。用户已明确给的不重复问。统计口径/对比基准不追问——按最常见口径直接查、回答里说明。ask_user 尽量带 options（2-4 个），候选一律从数据查真实取值，别凭空编、别写死「本月/上月」；只有数据里真查不到候选（纯开放信息）才只传 question。\n\n回答原则：\n- 直接答用户问的，只给问的那些数据，不自作主张做要点总结/分点罗列/额外建议。\n- 表格必须列全所有结果行，禁止只列前 5/前 N 截断——查回几行列几行（除非用户明确说 top N）。\n- 问一个数给一个数，问一张表给那批数据，别扩展成报告。\n- 结果为空如实说「没有查到符合条件的数据」，不编造。\n- 先简述查了什么（表/口径/行数），再给数据。\n\nSQL 原则：\n- 全程只读：只写 SELECT，禁止 DDL（建/改/删表）、DML（增删改数据）、危险函数。\n- 字段名用原始英文：SELECT/WHERE/ORDER BY/GROUP BY 引用字段必须用 query_metadata 返回的原始英文名（如 code/swdl）。AS 中文别名仅用于列头友好，绝不能在 WHERE/ORDER BY 当字段引用——中文别名不是真字段，拿来查必报错。\n- 全限定名：表名用 query_metadata 返回的原始写法（可能是 schema.table），别自己改写。\n- JOIN 优先：跨表用 JOIN，优先按 relations 关联口径；relations 为空按字段注释推导主外键，用 INNER/LEFT JOIN，别堆子查询。\n- UNION/UNION ALL 合并结构相同的结果集（优先 UNION ALL 更快）；步骤多/有中间复用用 WITH/CTE；聚合 SUM/COUNT/AVG 起有意义的别名。\n- 比率/增长率/百分率不乘 100，数据是什么写什么；聚合空值用 coalesce(sum(coalesce(字段,0)),0)；数字保留原始精度不四舍五入。',
    },
    {
        "scene": 'attribution',
        "tools": ['knowledge_search'],
        "mode": 'always_on',
        "order": 2,
        "content": '【归因方法论】用户问「为什么下降/上升/异常/波动/原因/怎么回事/主要原因」时按此走。\n归因是问数的延伸，不另起工具、不另起子任务——全程同一轮，基于问数阶段已查回的数据直接推理。\n\n【铁律·绝对禁止查库】归因阶段【绝对禁止】调用 execute_sql。你需要的全部数据，在问数阶段已经 execute_sql 查回、就在上方对话的工具结果里（result_id 对应的全量行）。归因 = 读上面的已有数据 + knowledge_search 取文档依据，然后推理。再调 execute_sql 就是错误——哪怕你觉得数据"可能变了"也不许查，数据就是上面那批，复用它。\n1. 定位事实（直接读已有）：先看本对话已有的 execute_sql 结果，确认涉及的指标、数值、对比基准。事实就在上面，不用查。\n2. 取依据：用 knowledge_search 检索相关文档（调度政策/历史复盘/指标口径/运维手册/规章制度）补充归因线索。query 必须传具体检索词（实体+指标+归因语义），禁止空 query。文档无相关依据就如实说「知识库无相关依据」，绝不编造。\n3. 分层归因：综合已有事实（数据）+ 检索依据（文档，如有），分主因（最可能/影响最大）与次因，每条标注来源（来自数据/来自文档）。明确区分「数据证实的」与「推测的」。\n4. 先给结论（主因），再列依据，最后给数据支撑。\n\n注意：归因是问数的延伸——拿到问数的事实后直接推理，无需切换、无需 load、无需把已查过的数据再查一遍。'
    },
]


HEADER = """-- ============================================================
-- nl2sql 数据库种子数据 —— 新环境初始化（与 schema.sql 配套）
-- ------------------------------------------------------------
-- 只含不含密钥的配置：skill 提示词 / RAGFlow 占位 / agent 上限。
-- 含密钥的表（llm_config/datasources/feishu_config）由用户自配，不在本文件。
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

    # --- 1) prompts: skill 单一真相源种子（content+tools+mode+order 全量，仅首次灌库）
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
            f'INSERT INTO prompts (scene, content, tools, mode, "order", version, enabled) VALUES (\n'
            f'    {_pg_str(name)}, {_pg_str(content)}, {_pg_json(tools)}, '
            f'{_pg_str(mode)}, {order}, 1, true\n'
            f') ON CONFLICT (scene) DO NOTHING;\n')
    parts.append("\n")

    # --- 2) ragflow_config: default 占位（地址/key 空，enabled=false，待 admin 填）---
    parts.append(
        "-- RAGFlow 知识库 default 占位（地址/key 空、禁用；admin 后台填真实值并启用）\n"
        "INSERT INTO ragflow_config (id, base_url, api_key, dataset_ids, top_k,\n"
        "    similarity_threshold, vector_similarity_weight, enabled, version)\n"
        "VALUES ('default', '', '', '[]'::json, 5, 0.2, 0.3, false, 1)\n"
        "ON CONFLICT (id) DO NOTHING;\n\n")

    # --- 3) agent_limits: default 运行上限 ---
    parts.append(
        "-- agent 运行上限 default（admin 后台可改）\n"
        "INSERT INTO agent_limits (id, max_turns, max_ask_user, max_sql,\n"
        "    max_sql_fail_streak, max_meta_per_run, version)\n"
        "VALUES ('default', 30, 2, 10, 3, 1, 1)\n"
        "ON CONFLICT (id) DO NOTHING;\n")

    parts.append("\nCOMMIT;\n")
    return "".join(parts)


if __name__ == "__main__":
    out = Path("db/seed.sql")
    out.parent.mkdir(exist_ok=True)
    out.write_text(generate(), encoding="utf-8")
    print(f"wrote {out} ({len(SEED_SKILLS)} skills + ragflow_config + agent_limits)")
