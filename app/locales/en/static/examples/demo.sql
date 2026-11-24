-- SQL Pulse example load test for the sqlpulse_demo database
-- Tables: orders (~10,000 rows, indexed id), stock (~500 rows), access_log (~50,000 rows, unindexed path)
-- Format: "-- weight: N" sets a group’s relative weight. Statements within a group run in order.
-- Use unquoted placeholders for data values only. Invalid parameters are rejected before a run starts.
-- Define order_id = rand(1,10000) in the form and reuse it as {{var('order_id')}} in the same task group.
-- Inline generators are sampled independently before each execution.

-- weight: 50 — Indexed primary-key lookup (expected P99 below 10 ms)
SELECT * FROM orders WHERE id = FLOOR(1 + RAND()*10000);

-- weight: 15 — Random decimal and date placeholders
SELECT COUNT(*) FROM orders WHERE amount >= {{randf(10.0,500.0,2)}} AND created_at >= {{randdate('2026-01-01','2026-12-31')}};

-- weight: 15 — Insert a weighted path and a random timestamp into the access log
INSERT INTO access_log(uid, path, ts) VALUES ({{pick(101,202,303)}}, {{pickw(('/api/x',80),('/api/y',20))}}, {{randdt('2026-01-01 00:00:00','2026-01-31 23:59:59')}});

-- weight: 20 — Deduct inventory in a transaction
BEGIN;
UPDATE stock SET qty = qty - 1 WHERE sku = CONCAT('SKU', FLOOR(1 + RAND()*500));
COMMIT;

-- weight: 20 — Write to the access log
INSERT INTO access_log(uid, path, ts) VALUES (FLOOR(RAND()*1000), '/api/x', NOW());

-- weight: 10 — Leading-wildcard LIKE on an unindexed column; demonstrates a full scan
SELECT COUNT(*) FROM access_log WHERE path LIKE '%api%';
