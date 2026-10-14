-- ============================================================================
-- 用例 3：同一张表内的语句必须保序（组内串行）
--
-- 20 条 INSERT 打向同一张表，seq 的值故意乱序，表上只有自增主键 id。
-- 如果分组后仍按文件顺序执行，那么 id 的递增顺序 = seq 在文件里出现的顺序。
--
-- 预期结果：状态 finished，影响 20 行
-- 验证语句（在 SQL 标签或 mysql 客户端执行）：
--   SELECT GROUP_CONCAT(seq ORDER BY id) AS actual FROM mi_seq;
--   期望值：
--   7,3,15,1,20,9,12,4,18,6,11,2,19,8,14,5,17,10,13,16
-- 若得到的是一串升序数字（1,2,3...），说明语句被重排了 —— 那是 bug。
-- ============================================================================

TRUNCATE TABLE mi_seq;

INSERT INTO mi_seq (seq) VALUES (7);
INSERT INTO mi_seq (seq) VALUES (3);
INSERT INTO mi_seq (seq) VALUES (15);
INSERT INTO mi_seq (seq) VALUES (1);
INSERT INTO mi_seq (seq) VALUES (20);
INSERT INTO mi_seq (seq) VALUES (9);
INSERT INTO mi_seq (seq) VALUES (12);
INSERT INTO mi_seq (seq) VALUES (4);
INSERT INTO mi_seq (seq) VALUES (18);
INSERT INTO mi_seq (seq) VALUES (6);
INSERT INTO mi_seq (seq) VALUES (11);
INSERT INTO mi_seq (seq) VALUES (2);
INSERT INTO mi_seq (seq) VALUES (19);
INSERT INTO mi_seq (seq) VALUES (8);
INSERT INTO mi_seq (seq) VALUES (14);
INSERT INTO mi_seq (seq) VALUES (5);
INSERT INTO mi_seq (seq) VALUES (17);
INSERT INTO mi_seq (seq) VALUES (10);
INSERT INTO mi_seq (seq) VALUES (13);
INSERT INTO mi_seq (seq) VALUES (16);
