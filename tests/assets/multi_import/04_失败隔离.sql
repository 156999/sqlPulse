-- ============================================================================
-- 用例 4：单张表失败不影响其他表（失败隔离）
--
-- 一共 7 个分组，故意让其中 3 个失败：
--   失败 A：mi_no_such_table —— 表不存在            -> 1146 Table doesn't exist
--   失败 B：mi_logs          —— 列名写错            -> 1054 Unknown column
--   失败 C：mi_users         —— 唯一键 email 重复    -> 1062 Duplicate entry
-- 其余 4 个分组（mi_orders / mi_items / mi_repo / mi_bulk_5）应当照常成功。
--
-- 预期结果
--   任务状态：partial（不是 failed！）
--   成功 4 个分组 / 失败 3 个分组
--   error_msg 里逐表列出失败原因
-- 看点
--   失败的表不影响其他表落库；任务不会因为一张表出错就整体中止。
-- ============================================================================

-- ---- 应当成功的 4 组 ----
TRUNCATE TABLE mi_orders;
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (2001, 1.00, 'new', NOW());
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (2002, 2.00, 'new', NOW());
INSERT INTO mi_orders (uid, amount, status, created_at) VALUES (2003, 3.00, 'new', NOW());

TRUNCATE TABLE mi_items;
INSERT INTO mi_items (sku, qty, note) VALUES ('OK-1', 1, 'should succeed');
INSERT INTO mi_items (sku, qty, note) VALUES ('OK-2', 2, 'should succeed');
INSERT INTO mi_items (sku, qty, note) VALUES ('OK-3', 3, 'should succeed');

TRUNCATE TABLE mi_repo;
INSERT INTO mi_repo (id, v, tag) VALUES (101, 1, 'ok');
INSERT INTO mi_repo (id, v, tag) VALUES (102, 2, 'ok');
INSERT INTO mi_repo (id, v, tag) VALUES (103, 3, 'ok');

TRUNCATE TABLE mi_bulk_5;
INSERT INTO mi_bulk_5 (v) VALUES (1);
INSERT INTO mi_bulk_5 (v) VALUES (2);
INSERT INTO mi_bulk_5 (v) VALUES (3);

-- ---- 应当失败的 3 组 ----

-- 失败 A：目标表根本不存在
INSERT INTO mi_no_such_table (v) VALUES (1);

-- 失败 B：列名写成 bad_column
TRUNCATE TABLE mi_logs;
INSERT INTO mi_logs (uid, bad_column) VALUES (1, 'x');

-- 失败 C：同一个 email 在同一个文件里插入两次，第二次触发唯一键冲突
TRUNCATE TABLE mi_users;
INSERT INTO mi_users (id, name, email) VALUES (1, 'alice', 'dup@example.com');
INSERT INTO mi_users (id, name, email) VALUES (2, 'bob', 'dup@example.com');
