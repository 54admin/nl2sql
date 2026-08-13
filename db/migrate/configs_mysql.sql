-- 从在线 PG 导出的环境配置（模型/数据源/模板/知识库）
-- 源: nl2sql@online PG (旧表名) → 目标: MySQL 新表名（dev profile 连的华为云 RDS MySQL）
-- 飞书 feishu_config 不含（用户指定不配）。生成器: scripts/export_configs.py

-- llm_config → nl_cfg_llm  (8 行)
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__qwen3_6_vl', '[]', 'qwen3.6-vl', 'http://10.111.32.151:3001', 'sk-g2G93EQHSq0MZDAwITVU1M8orOGqgbQ9hCVJSPpnsxrcYpd0', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 1, '2026-08-05 18:41:09.193273');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__glm_5_2', '[]', 'glm-5.2', 'http://10.111.32.151:3001', 'sk-zNtdVk4dG6fVf0emN4PTtd1uN6Mjw0kquPqfuSbICii1yMUk', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 3, '2026-08-05 18:12:37.883970');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__kimi_k2_7_code', '["analysis","attribution"]', 'kimi-k2.7-code', 'http://10.111.32.151:3001', 'sk-zNtdVk4dG6fVf0emN4PTtd1uN6Mjw0kquPqfuSbICii1yMUk', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 1, '2026-07-30 14:54:48.286015');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__qwen3_7_max', '[]', 'qwen3.7-max', 'http://10.111.32.151:3001', 'sk-zNtdVk4dG6fVf0emN4PTtd1uN6Mjw0kquPqfuSbICii1yMUk', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 1, '2026-07-30 14:54:58.336318');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__deepseek_v4_pro', '[]', 'deepseek-v4-pro', 'http://10.111.32.151:3001', 'sk-zNtdVk4dG6fVf0emN4PTtd1uN6Mjw0kquPqfuSbICii1yMUk', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 1, '2026-07-30 14:54:47.630547');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__deepseek_v4_flash', '[]', 'deepseek-v4-flash', 'http://10.111.32.151:3001', 'sk-g2G93EQHSq0MZDAwITVU1M8orOGqgbQ9hCVJSPpnsxrcYpd0', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 6, '2026-08-05 18:12:36.194309');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__gemma_4_31B', '[]', 'gemma-4-31B', 'http://10.111.32.151:3001', 'sk-g2G93EQHSq0MZDAwITVU1M8orOGqgbQ9hCVJSPpnsxrcYpd0', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 4, '2026-08-05 18:12:36.971500');
INSERT IGNORE INTO nl_cfg_llm (`id`, `purposes`, `model`, `base_url`, `api_key`, `temperature`, `timeout`, `max_context`, `protocol`, `rpm_limit`, `concurrency`, `enabled`, `version`, `updated_at`) VALUES ('10_111_32_151_3001__Qwen3_6_27B', '[]', 'Qwen3.6-27B', 'http://10.111.32.151:3001', 'sk-g2G93EQHSq0MZDAwITVU1M8orOGqgbQ9hCVJSPpnsxrcYpd0', 0.0, 60, 32000, 'anthropic', NULL, NULL, 1, 2, '2026-08-05 18:12:37.433123');

-- datasources → nl_cfg_datasources  (1 行)
INSERT IGNORE INTO nl_cfg_datasources (`id`, `name`, `type`, `host`, `port`, `db_name`, `username`, `password_enc`, `sync_scope`, `enabled`, `version`, `created_at`, `updated_at`) VALUES (2, '数仓', 'starrocks', '10.12.26.211', 9030, NULL, 'comm_user', 'R3&fhp!LyH#e^cFuGOu', NULL, 1, 2, '2026-07-20 10:04:21.939528', '2026-07-31 09:24:14.895968');

-- sql_templates → nl_md_templates  (4 行)
INSERT IGNORE INTO nl_md_templates (`id`, `name`, `sql_template`, `usage`, `enabled`, `version`, `created_at`, `updated_at`) VALUES (2, '宽表列转行(unpivot)', 'SELECT ind.indicator,
       CASE ind.indicator
         WHEN ''指标A'' THEN t.指标A_列
         WHEN ''指标B'' THEN t.指标B_列
         -- 每个指标一列，列名只在 CASE 出现一次（比 UNION ALL 稳）
       END AS score
FROM 目标宽表 t
CROSS JOIN (
    SELECT ''指标A'' AS indicator
    UNION ALL SELECT ''指标B''
    -- 字典子查询，一行一个指标名
) ind
HAVING score IS NOT NULL
ORDER BY score ASC
LIMIT 1', '把「每指标一列」的宽表拉成「一行一指标」，用于跨指标排名/找最值/对比。要点：只查一次表 + CROSS JOIN 一个指标名字典子查询拉长行数 + CASE 按指标名从同行取对应列值。', 1, 1, '2026-07-30 19:55:22.565616', '2026-07-30 19:55:22.565616');
INSERT IGNORE INTO nl_md_templates (`id`, `name`, `sql_template`, `usage`, `enabled`, `version`, `created_at`, `updated_at`) VALUES (3, '环比对比+指标拆解', 'SELECT
  cur.综合得分 AS 本期综合, prev.综合得分 AS 上期综合,
  cur.综合得分 - prev.综合得分 AS 综合变化,
  prev.排名 - cur.排名 AS 排名变化,
  ind.indicator AS 指标,
  CASE ind.col WHEN ''指标A_列'' THEN cur.指标A_列 WHEN ''指标B_列'' THEN cur.指标B_列 END AS 本期指标得分,
  CASE ind.col WHEN ''指标A_列'' THEN prev.指标A_列 WHEN ''指标B_列'' THEN prev.指标B_列 END AS 上期指标得分
FROM 目标表 cur
JOIN 目标表 prev ON prev.主体键 = cur.主体键 AND prev.时间 = :上期时间
CROSS JOIN (
  SELECT ''指标A'' AS indicator, ''指标A_列'' AS col
  UNION ALL SELECT ''指标B'', ''指标B_列''
) ind
WHERE cur.时间 = :本期时间
ORDER BY (本期指标得分 - 上期指标得分) DESC', '一条 SQL 同时给综合分变化/排名升降/各指标正负贡献。两步组合：自连接拿两期同主体 → CROSS JOIN 指标字典拉长 → 各指标环比排序看拉动/拖累。要点：自连接比窗口函数稳（显式配对两期）；排名变化=prev.排名-cur.排名（数字小=靠前，相减正=上升）；综合分带权重，指标层变化不能反推综合分。', 1, 1, '2026-07-30 19:55:22.565616', '2026-07-30 19:55:22.565616');
INSERT IGNORE INTO nl_md_templates (`id`, `name`, `sql_template`, `usage`, `enabled`, `version`, `created_at`, `updated_at`) VALUES (4, '两期差集(新增/退出实体)', 'SELECT cur.实体名, cur.其他字段
FROM 目标表 cur
WHERE cur.时间 = :本期
  AND NOT EXISTS (
    SELECT 1 FROM 目标表 prev
    WHERE prev.实体键 = cur.实体键
      AND prev.时间 = :上期
  )', '找出本期新进入、上期不存在的实体（或反向找退出）。NOT EXISTS 反半连接——「本期有、上期没有」=新增；两期互换=退出。比 NOT IN（NULL 会干掉整个结果）和 LEFT JOIN IS NULL 干净，NULL 安全。反向找退出：把 :本期 和 :上期 互换。', 1, 1, '2026-07-30 19:55:22.565616', '2026-07-30 19:55:22.565616');
INSERT IGNORE INTO nl_md_templates (`id`, `name`, `sql_template`, `usage`, `enabled`, `version`, `created_at`, `updated_at`) VALUES (5, '跨层级钻取(全局最优→下钻TopN)', 'WITH best AS (
  SELECT 实体, 指标 FROM (
    SELECT 实体, 指标, 值,
           RANK() OVER (ORDER BY 值 DESC) AS rnk
    FROM (
      SELECT t.实体, ind.indicator,
             CASE ind.indicator WHEN ''指标A'' THEN t.指标A列 WHEN ''指标B'' THEN t.指标B列 END AS 值
      FROM 汇总表 t
      CROSS JOIN (SELECT ''指标A'' AS indicator UNION ALL SELECT ''指标B'') ind
      WHERE t.层级 = :汇总层 AND t.时间 = :本期
    ) raw WHERE 值 IS NOT NULL
  ) ranked WHERE rnk = 1
),
detail AS (
  SELECT * FROM (
    SELECT t.明细实体, t.实体, ind.indicator,
           CASE ind.indicator WHEN ''指标A'' THEN t.指标A列 WHEN ''指标B'' THEN t.指标B列 END AS 值,
           RANK() OVER (PARTITION BY t.实体, ind.indicator ORDER BY 值 DESC) AS rnk
    FROM 明细表 t
      CROSS JOIN (SELECT ''指标A'' AS indicator UNION ALL SELECT ''指标B'') ind
    WHERE t.层级 = :明细层 AND t.时间 = :本期
  ) p WHERE 值 IS NOT NULL
)
SELECT b.实体, b.指标, d.rnk AS 排名, d.明细实体, d.值
FROM best b
JOIN detail d ON d.实体 = b.实体 AND d.指标 = b.指标
WHERE d.rnk <= :N
ORDER BY d.rnk', '先在汇总层找全局最优指标，再下钻到明细层按「实体+指标」分组排名取 TopN。要点：CTE 拆两段可读可测；RANK 比 ROW_NUMBER 好（同分并列不丢）；按结构化字段关联不用 LIKE；窗口 ORDER BY 不能引用同层别名，CASE 要在 ORDER BY 重写。变体：找最高→DESC/最低→ASC；按偏差排用 ABS(偏差) DESC；只看未完成去掉 ABS 直接 ORDER BY 偏差 ASC。', 1, 1, '2026-07-30 19:55:22.565616', '2026-07-30 19:55:22.565616');

-- ragflow_config → nl_cfg_ragflow  (1 行)
INSERT IGNORE INTO nl_cfg_ragflow (`id`, `base_url`, `api_key`, `dataset_ids`, `top_k`, `similarity_threshold`, `vector_similarity_weight`, `enabled`, `version`, `updated_at`) VALUES ('default', 'http://10.111.88.35', 'ragflow-c2zRmwwz9MWCIx44txQgkKo-3vCEbUBDoocYAQkuTYc', '["e0372ac6921211f1bf42978c49ce16e3", "d619b5e0921211f1bf42978c49ce16e3"]', 5, 0.2, 0.3, 1, 7, '2026-08-07 13:39:37.991501');
