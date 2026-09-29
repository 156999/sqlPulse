-- SQL Pulse 示例压测脚本（基于内置示例库 sqlpulse_demo）
-- 表：orders（1 万行，id 主键有索引）/ stock（500 行）/ access_log（5 万行，path 无索引）
-- 格式："-- weight: N" 指定执行权重；同一权重段内的多条语句顺序执行（可放事务）
-- 占位符仅用于值位置，外层不加引号；非法参数在启动前拒绝。
-- 可在页面配置 order_id = rand(1,10000)，并在同一任务多处引用 {{var('order_id')}}。
-- 占位符（{{rand(...)}} / {{randf(...)}} / {{pick(...)}} / {{pickw(...)}} / {{randstr(...)}} / {{randdate(...)}} / {{randdt(...)}} / {{uuid()}}）每次执行前随机替换

-- weight: 50 —— 主键点查，走索引，预期 P99 < 10ms
SELECT * FROM orders WHERE id = FLOOR(1 + RAND()*10000);

-- weight: 15 —— 占位符：随机小数区间 + 随机日期
SELECT COUNT(*) FROM orders WHERE amount >= {{randf(10.0,500.0,2)}} AND created_at >= {{randdate('2026-01-01','2026-12-31')}};

-- weight: 15 —— 占位符：加权路径 + 随机时间写入访问日志
INSERT INTO access_log(uid, path, ts) VALUES ({{pick(101,202,303)}}, {{pickw(('/api/x',80),('/api/y',20))}}, {{randdt('2026-01-01 00:00:00','2026-01-31 23:59:59')}});

-- weight: 20 —— 事务块：扣库存（三条语句作为一个 task 顺序执行）
BEGIN;
UPDATE stock SET qty = qty - 1 WHERE sku = CONCAT('SKU', FLOOR(1 + RAND()*500));
COMMIT;

-- weight: 20 —— 写入访问日志
INSERT INTO access_log(uid, path, ts) VALUES (FLOOR(RAND()*1000), '/api/x', NOW());

-- weight: 10 —— 无索引 + 前置通配 LIKE，全表扫描，演示被 L1 规则引擎抓到
SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%';
