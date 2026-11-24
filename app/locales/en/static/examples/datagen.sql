-- SQL Pulse example: delete previously generated rows by uid, then append 1,000 orders
USE sqlpulse_demo;

DELETE FROM orders WHERE uid = 999999;

INSERT INTO orders (uid, amount, status, created_at)
WITH RECURSIVE seq(n) AS (
  SELECT 1
  UNION ALL
  SELECT n + 1 FROM seq WHERE n < 1000
)
SELECT
  999999,
  ROUND(RAND() * 500, 2),
  ELT(1 + FLOOR(RAND() * 3), 'new', 'paid', 'done'),
  NOW() - INTERVAL FLOOR(RAND() * 86400) SECOND
FROM seq;
