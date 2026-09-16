-- SQL Pulse 示例压测脚本（基于内置示例库 sqlpulse_demo）
-- 表：orders（1 万行，id 主键有索引）/ stock（500 行）/ access_log（5 万行，path 无索引）
-- 格式："-- weight: N" 指定执行权重；同一权重段内的多条语句顺序执行（可放事务）

-- weight: 50 —— 主键点查，走索引，预期 P99 < 10ms
SELECT * FROM orders WHERE id = FLOOR(1 + RAND()*10000);

-- weight: 20 —— 事务块：扣库存（三条语句作为一个 task 顺序执行）
BEGIN;
UPDATE stock SET qty = qty - 1 WHERE sku = CONCAT('SKU', FLOOR(1 + RAND()*500));
COMMIT;

-- weight: 20 —— 写入访问日志
INSERT INTO access_log(uid, path, ts) VALUES (FLOOR(RAND()*1000), '/api/x', NOW());

-- weight: 10 —— 无索引 + 前置通配 LIKE，全表扫描，演示被 L1 规则引擎抓到
SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%';
