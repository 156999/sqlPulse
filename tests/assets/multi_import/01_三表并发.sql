-- ============================================================================
-- 用例 1：最基础的三表并发导入
--
-- 预期结果
--   任务状态：finished
--   分组：mi_orders / mi_items / mi_logs，各 6 条语句（1 条 TRUNCATE + 5 条 INSERT）
--         三个分组全部 finished，各影响 5 行，共 15 行
-- 看点
--   1) 三张表在任务日志里几乎同一时刻结束（组间并发）
--   2) 同一张表的 5 条 INSERT 严格按文件顺序执行（组内串行保序）
--
-- 先用 mysql 客户端或造数页「SQL」标签执行 00_setup_tables.sql 建表。
-- ============================================================================

TRUNCATE TABLE mi_orders;
TRUNCATE TABLE mi_items;
TRUNCATE TABLE mi_logs;

-- ---- mi_orders ----
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (1001, 19.90, 'new', NOW());
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (1002, 88.00, 'paid', NOW());
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (1003, 5.50, 'new', NOW());
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (1004, 240.75, 'ship', NOW());
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (1005, 12.30, 'paid', NOW());

-- ---- mi_items ----
INSERT INTO mi_items (sku, qty, note) VALUES ('SKU-A', 3, 'first');
INSERT INTO mi_items (sku, qty, note) VALUES ('SKU-B', 1, 'second');
INSERT INTO mi_items (sku, qty, note) VALUES ('SKU-C', 7, 'third');
INSERT INTO mi_items (sku, qty, note) VALUES ('SKU-D', 2, 'fourth');
INSERT INTO mi_items (sku, qty, note) VALUES ('SKU-E', 9, 'fifth');

-- ---- mi_logs ----
INSERT INTO mi_logs (uid, path, ts) VALUES (1001, '/api/orders', NOW());
INSERT INTO mi_logs (uid, path, ts) VALUES (1002, '/api/items', NOW());
INSERT INTO mi_logs (uid, path, ts) VALUES (1003, '/api/orders', NOW());
INSERT INTO mi_logs (uid, path, ts) VALUES (1004, '/api/stock', NOW());
INSERT INTO mi_logs (uid, path, ts) VALUES (1005, '/api/report', NOW());
