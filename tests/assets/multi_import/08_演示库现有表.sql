-- ============================================================================
-- 用例 8：直接打到演示库已有的三张表（orders / access_log / stock）
--
-- 不需要执行 00_setup_tables.sql，开箱即用 —— 想先快速看一眼效果就用这个。
-- 为了让文件可以反复运行，stock 分组先用 DELETE 清掉本用例写过的 sku。
--
-- 预期结果：状态 finished，3 个分组全部成功，共 15 行
--   orders     +5 行（自增 id，累加）
--   access_log +5 行（自增 id，累加）
--   stock      5 行（先删后插，始终是 5 行）
-- ============================================================================

-- ---- orders ----
INSERT INTO orders (uid, amount, status, created_at) VALUES (90001, 11.10, 'new', NOW());
INSERT INTO orders (uid, amount, status, created_at) VALUES (90002, 22.20, 'paid', NOW());
INSERT INTO orders (uid, amount, status, created_at) VALUES (90003, 33.30, 'new', NOW());
INSERT INTO orders (uid, amount, status, created_at) VALUES (90004, 44.40, 'ship', NOW());
INSERT INTO orders (uid, amount, status, created_at) VALUES (90005, 55.50, 'paid', NOW());

-- ---- access_log ----
INSERT INTO access_log (uid, path, ts) VALUES (90001, '/api/multi/test-1', NOW());
INSERT INTO access_log (uid, path, ts) VALUES (90002, '/api/multi/test-2', NOW());
INSERT INTO access_log (uid, path, ts) VALUES (90003, '/api/multi/test-3', NOW());
INSERT INTO access_log (uid, path, ts) VALUES (90004, '/api/multi/test-4', NOW());
INSERT INTO access_log (uid, path, ts) VALUES (90005, '/api/multi/test-5', NOW());

-- ---- stock（先清掉本用例写过的行，保证可重复运行）----
DELETE FROM stock WHERE sku LIKE 'TEST-%';
INSERT INTO stock (sku, qty) VALUES ('TEST-01', 10);
INSERT INTO stock (sku, qty) VALUES ('TEST-02', 20);
INSERT INTO stock (sku, qty) VALUES ('TEST-03', 30);
INSERT INTO stock (sku, qty) VALUES ('TEST-04', 40);
INSERT INTO stock (sku, qty) VALUES ('TEST-05', 50);
