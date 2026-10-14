-- ============================================================================
-- 用例 6：识别目标表的各种写法变体
--
-- 覆盖：反引号、库名前缀、行内注释、块注释、INSERT ... SELECT、
--       REPLACE INTO、UPDATE、DELETE、LIKE 里的百分号。
--
-- 预期结果：状态 finished
--   分组：mi_repo（7 条语句）、mi_items（2 条语句），共影响 7 行
--   注意留意的两点：
--     ① TRUNCATE / INSERT 用了反引号和「库名.表名」，两组都能被归到正确的表上；
--     ② mi_repo 最终只剩 3 行：id = 11(blue) / 12(green) / 13(yel)
--        id=10 的 red 先被 UPDATE 改了 v，又被最后一条 LIKE 'r%' 的 DELETE 删掉
--        —— 能删掉，前提是同一张表的语句确实按文件顺序执行。
-- ============================================================================

TRUNCATE TABLE `mi_repo`;
TRUNCATE TABLE sqlpulse_demo.mi_items;

-- 反引号 + 行尾注释
INSERT INTO `mi_repo` (id, v, tag) VALUES (10, 1, 'red');   -- 这一行最后会被 DELETE 干掉

-- 块注释，跨两行
/* 下面这条用「库名.表名」的写法，
   分组时应识别为 mi_repo 而不是 sqlpulse_demo */
INSERT INTO sqlpulse_demo.mi_repo (id, v, tag) VALUES (11, 2, 'blue');

-- REPLACE INTO（也算写入目标表）
REPLACE INTO mi_repo (id, v, tag) VALUES (12, 3, 'green');

-- INSERT ... SELECT，没有 FROM
INSERT INTO mi_repo (id, v, tag) SELECT 13, 4, 'yel';

-- UPDATE：把 red 的 v 加 100
UPDATE mi_repo SET v = v + 100 WHERE tag = 'red';

-- DELETE：删掉 tag 以 r 开头的行（LIKE 里的百分号要能正确透传）
DELETE FROM mi_repo WHERE tag LIKE 'r%';

-- 换一张表，验证多组并存
INSERT INTO mi_items (sku, qty, note) VALUES ('VAR-1', 1, 'from syntax variants');
