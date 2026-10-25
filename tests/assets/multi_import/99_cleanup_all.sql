-- ============================================================================
-- 清理：删掉全部测试表（14 张）以及用例 8 写进演示表的数据
--
-- 同样请按顺序整份执行（mysql 客户端或造数页「SQL」标签），
-- 不要用「多表导入」——父子表删除有先后依赖。
-- ============================================================================

-- 有外键：先删子表
DROP TABLE IF EXISTS mi_child;
DROP TABLE IF EXISTS mi_parent;
DROP TABLE IF EXISTS mi_orders;
DROP TABLE IF EXISTS mi_items;
DROP TABLE IF EXISTS mi_logs;
DROP TABLE IF EXISTS mi_users;
DROP TABLE IF EXISTS mi_seq;
DROP TABLE IF EXISTS mi_repo;
DROP TABLE IF EXISTS mi_bulk_1;
DROP TABLE IF EXISTS mi_bulk_2;
DROP TABLE IF EXISTS mi_bulk_3;
DROP TABLE IF EXISTS mi_bulk_4;
DROP TABLE IF EXISTS mi_bulk_5;
DROP TABLE IF EXISTS mi_bulk_6;

-- 用例 8 写进演示表的数据（orders / access_log 的行无法按来源精确区分，
-- 这里按 uid 前缀 9000x 删；如果演示库里有别的数据也用了这些 uid，请手动调整）
DELETE FROM orders      WHERE uid BETWEEN 90001 AND 90005;
DELETE FROM access_log  WHERE uid BETWEEN 90001 AND 90005;
DELETE FROM stock       WHERE sku LIKE 'TEST-%';
