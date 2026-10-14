-- ============================================================================
-- 用例 5：表数 > 并发分组数 时的波次行为
--
-- 6 张表，每张表里都放了一条 SLEEP(0.5)，让单个分组的耗时稳定可测。
--   并发分组数 = 2  ->  6 张表分 3 波，总耗时约 1.6 ~ 2.2 秒
--   并发分组数 = 6  ->  一波跑完，总耗时约 0.6 ~ 1.0 秒
-- 注意：实际并发度还会被「目标表数量」和服务端上限（默认 16）夹住。
--
-- 预期结果：状态 finished，6 个分组全部成功，共 18 行
-- ============================================================================

TRUNCATE TABLE mi_bulk_1;
INSERT INTO mi_bulk_1 (v) VALUES (SLEEP(0.5));
INSERT INTO mi_bulk_1 (v) VALUES (1);
INSERT INTO mi_bulk_1 (v) VALUES (1);

TRUNCATE TABLE mi_bulk_2;
INSERT INTO mi_bulk_2 (v) VALUES (SLEEP(0.5));
INSERT INTO mi_bulk_2 (v) VALUES (2);
INSERT INTO mi_bulk_2 (v) VALUES (2);

TRUNCATE TABLE mi_bulk_3;
INSERT INTO mi_bulk_3 (v) VALUES (SLEEP(0.5));
INSERT INTO mi_bulk_3 (v) VALUES (3);
INSERT INTO mi_bulk_3 (v) VALUES (3);

TRUNCATE TABLE mi_bulk_4;
INSERT INTO mi_bulk_4 (v) VALUES (SLEEP(0.5));
INSERT INTO mi_bulk_4 (v) VALUES (4);
INSERT INTO mi_bulk_4 (v) VALUES (4);

TRUNCATE TABLE mi_bulk_5;
INSERT INTO mi_bulk_5 (v) VALUES (SLEEP(0.5));
INSERT INTO mi_bulk_5 (v) VALUES (5);
INSERT INTO mi_bulk_5 (v) VALUES (5);

TRUNCATE TABLE mi_bulk_6;
INSERT INTO mi_bulk_6 (v) VALUES (SLEEP(0.5));
INSERT INTO mi_bulk_6 (v) VALUES (6);
INSERT INTO mi_bulk_6 (v) VALUES (6);
