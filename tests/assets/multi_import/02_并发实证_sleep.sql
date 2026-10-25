-- ============================================================================
-- 用例 2：用 SLEEP 直观验证「组间并发、组内串行」
--
-- 4 张表各 1 条含 SLEEP(0.6) 的插入：
--   并发分组数 >= 4  -> 总耗时约 0.7 ~ 1.2 秒
--   并发分组数 = 1   -> 总耗时 ≥ 2.4 秒（4 × 0.6）
-- 这是最容易复现的对照实验：同一个文件、只改「并发分组数」，耗时差 3 倍以上。
--
-- 预期结果：状态 finished，4 个分组各 1 行，共 4 行
-- ============================================================================

TRUNCATE TABLE mi_bulk_1;
TRUNCATE TABLE mi_bulk_2;
TRUNCATE TABLE mi_bulk_3;
TRUNCATE TABLE mi_bulk_4;

INSERT INTO mi_bulk_1 (v) VALUES (SLEEP(0.6));
INSERT INTO mi_bulk_2 (v) VALUES (SLEEP(0.6));
INSERT INTO mi_bulk_3 (v) VALUES (SLEEP(0.6));
INSERT INTO mi_bulk_4 (v) VALUES (SLEEP(0.6));
