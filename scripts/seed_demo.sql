-- SQL Pulse 示例库：三张表。orders.id 走索引；access_log.path 无索引（演示慢 SQL 被规则引擎抓到）
CREATE DATABASE IF NOT EXISTS sqlpulse_demo;
USE sqlpulse_demo;

DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  uid INT NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  status VARCHAR(10) NOT NULL,
  created_at DATETIME NOT NULL,
  KEY idx_uid (uid)
) ENGINE=InnoDB;

DROP TABLE IF EXISTS stock;
CREATE TABLE stock (
  sku VARCHAR(32) PRIMARY KEY,
  qty INT NOT NULL DEFAULT 0
) ENGINE=InnoDB;

DROP TABLE IF EXISTS access_log;
CREATE TABLE access_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid INT NOT NULL,
  path VARCHAR(128) NOT NULL,
  ts DATETIME NOT NULL
) ENGINE=InnoDB;

DELIMITER $$
DROP PROCEDURE IF EXISTS seed_data $$
CREATE PROCEDURE seed_data()
BEGIN
  DECLARE i INT DEFAULT 1;
  START TRANSACTION;
  WHILE i <= 10000 DO
    INSERT INTO orders (uid, amount, status, created_at)
    VALUES (FLOOR(RAND()*1000), ROUND(RAND()*500,2),
            ELT(1+FLOOR(RAND()*3),'new','paid','done'),
            NOW() - INTERVAL FLOOR(RAND()*86400) SECOND);
    SET i = i + 1;
  END WHILE;
  COMMIT;

  START TRANSACTION;
  SET i = 1;
  WHILE i <= 500 DO
    INSERT INTO stock (sku, qty) VALUES (CONCAT('SKU', i), 1000 + FLOOR(RAND()*100));
    SET i = i + 1;
  END WHILE;
  COMMIT;

  START TRANSACTION;
  SET i = 1;
  WHILE i <= 50000 DO
    INSERT INTO access_log (uid, path, ts)
    VALUES (FLOOR(RAND()*1000),
            ELT(1+FLOOR(RAND()*4),'/api/x','/api/y','/page/z','/static/w'),
            NOW() - INTERVAL FLOOR(RAND()*3600) SECOND);
    SET i = i + 1;
  END WHILE;
  COMMIT;
END $$
DELIMITER ;

CALL seed_data();
DROP PROCEDURE seed_data;

SELECT 'orders' t, COUNT(*) n FROM orders
UNION ALL SELECT 'stock', COUNT(*) FROM stock
UNION ALL SELECT 'access_log', COUNT(*) FROM access_log;
