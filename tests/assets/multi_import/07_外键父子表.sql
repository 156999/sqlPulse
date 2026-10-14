-- ============================================================================
-- 用例 7：外键父子表 —— 演示「不做依赖排序」的代价
--
-- 本功能按你的选择**不做外键依赖排序**，父表和子表会并发执行。
-- 这个文件里同时出现三件事：
--   ① mi_child 引用 setup 预置的父行 id=1   -> 一定成功（对照组）
--   ② mi_child 引用本文件才创建的 id=999    -> 与父表分组抢跑（实验组）
--   ③ mi_parent 创建 id=999
--
-- 预期结果：**不稳定，这是刻意的**
--   若 ③ 先于 ② 落库 -> 状态 finished，2 行
--   若 ② 先于 ③ 落库 -> mi_child 分组失败 1452
--                        （Cannot add or update a child row），状态 partial
-- 实测两种情况都会出现。另外如果你的文件里对**父表**用了 DELETE / REPLACE
-- （会先删后插），还可能报 1451（Cannot delete or update a parent row）——
-- 因为父行被子表引用着。这里刻意用 INSERT IGNORE 避免掺进 1451，好让现象更干净。
-- 需要稳定成功时，正确做法是：父表一个任务先跑完，再跑子表任务；
-- 或者把父子表都放进造数页的「SQL」标签（顺序执行，不分组并发）。
-- ============================================================================

DELETE FROM mi_child;

INSERT INTO mi_child (parent_id) VALUES (1);
INSERT INTO mi_child (parent_id) VALUES (999);

INSERT IGNORE INTO mi_parent (id, name) VALUES (999, 'p999');
