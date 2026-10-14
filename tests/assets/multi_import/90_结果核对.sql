-- ============================================================================
-- 结果核对用查询（只读）
--
-- ⚠️ 这个文件**不能**丢进「多表导入」跑：里面全是 SELECT，没有可识别的目标表，
--    多表导入会直接拒绝（"没有识别出任何目标表"）。
--    请在**造数页的「SQL」标签**（顺序执行）或 mysql 客户端里执行。
--
-- 用法：跑完某个用例后来这里查对应的那几行。
-- 注意：用 mysql 命令行客户端执行时，容器默认客户端字符集不是 utf8mb4，
--       中文列别名会被截断报错 —— 所以下面第一行显式 SET NAMES。
-- ============================================================================

SET NAMES utf8mb4;

-- 用例 1 / 8：各表行数
SELECT 'mi_orders' AS 表名, COUNT(*) AS 行数 FROM mi_orders
UNION ALL SELECT 'mi_items',   COUNT(*) FROM mi_items
UNION ALL SELECT 'mi_logs',    COUNT(*) FROM mi_logs
UNION ALL SELECT 'mi_seq',     COUNT(*) FROM mi_seq
UNION ALL SELECT 'mi_bulk_1',  COUNT(*) FROM mi_bulk_1
UNION ALL SELECT 'mi_bulk_2',  COUNT(*) FROM mi_bulk_2
UNION ALL SELECT 'mi_bulk_3',  COUNT(*) FROM mi_bulk_3
UNION ALL SELECT 'mi_bulk_4',  COUNT(*) FROM mi_bulk_4
UNION ALL SELECT 'mi_bulk_5',  COUNT(*) FROM mi_bulk_5
UNION ALL SELECT 'mi_bulk_6',  COUNT(*) FROM mi_bulk_6;

-- 用例 3：组内保序 —— 期望 7,3,15,1,20,9,12,4,18,6,11,2,19,8,14,5,17,10,13,16
SELECT GROUP_CONCAT(seq ORDER BY id) AS 实际插入顺序 FROM mi_seq;

-- 用例 6：mi_repo 期望只剩 3 行（11 blue / 12 green / 13 yel），id=10 已被 DELETE 干掉
SELECT id, v, tag FROM mi_repo ORDER BY id;
SELECT GROUP_CONCAT(CONCAT(id, ':', tag) ORDER BY id) AS mi_repo_概览 FROM mi_repo;

-- 用例 7：父子表 —— mi_child 若为空说明子表分组失败（1452）
SELECT (SELECT COUNT(*) FROM mi_parent WHERE id = 999) AS 父行999是否存在,
       (SELECT COUNT(*) FROM mi_child)                 AS 子表行数;
