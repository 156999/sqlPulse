-- ============================================================================
-- 多表导入测试用表（14 张，统一 mi_ 前缀，便于一次性清理）
--
-- 运行方式：整份按顺序执行（mysql 客户端，或造数页的「SQL」标签顺序执行）。
-- ⚠️ 不要用「多表导入」跑这个文件：多表导入按目标表并发、不保证 DDL 先后顺序，
--    建/删有外键关系的父子表会失败。这本身就是该功能的一个已知取舍。
--
-- 清理：见同目录 99_cleanup_tables.sql
-- ============================================================================

-- 有外键的表先删子表
DROP TABLE IF EXISTS mi_child;
DROP TABLE IF EXISTS mi_parent;
DROP TABLE IF EXISTS mi_orders;
DROP TABLE IF EXISTS mi_items;
DROP TABLE IF EXISTS mi_logs;
DROP TABLE IF EXISTS mi_users;
DROP TABLE IF EXISTS mi_seq;
DROP TABLE IF EXISTS mi_repo;
DROP TABLE IF EXISTS mi_bulk_1;
DROP TABLE IF EXISTS mi_bulk_2;
DROP TABLE IF EXISTS mi_bulk_3;
DROP TABLE IF EXISTS mi_bulk_4;
DROP TABLE IF EXISTS mi_bulk_5;
DROP TABLE IF EXISTS mi_bulk_6;

-- 基础三表：模拟订单 / 商品 / 访问日志
CREATE TABLE mi_orders (
  id INT NOT NULL AUTO_INCREMENT,
  uid INT NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  status VARCHAR(10) NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY idx_uid (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE mi_items (
  id INT NOT NULL AUTO_INCREMENT,
  sku VARCHAR(32) NOT NULL,
  qty INT NOT NULL DEFAULT 0,
  note VARCHAR(64) DEFAULT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE mi_logs (
  id BIGINT NOT NULL AUTO_INCREMENT,
  uid INT NOT NULL,
  path VARCHAR(128) NOT NULL,
  ts DATETIME NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 带唯一约束：用于测试「唯一键冲突」的失败隔离
CREATE TABLE mi_users (
  id INT NOT NULL,
  name VARCHAR(32) NOT NULL,
  email VARCHAR(64) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 只有自增主键：用于验证「同一张表内语句保序」
CREATE TABLE mi_seq (
  id INT NOT NULL AUTO_INCREMENT,
  seq INT NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 外键父子表：用于演示「不做依赖排序」的代价
CREATE TABLE mi_parent (
  id INT NOT NULL,
  name VARCHAR(32) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE mi_child (
  id INT NOT NULL AUTO_INCREMENT,
  parent_id INT NOT NULL,
  PRIMARY KEY (id),
  KEY idx_parent (parent_id),
  CONSTRAINT fk_mi_child_parent FOREIGN KEY (parent_id) REFERENCES mi_parent (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 用于 REPLACE / UPDATE / DELETE 等写法变体
CREATE TABLE mi_repo (
  id INT NOT NULL,
  v INT NOT NULL,
  tag VARCHAR(16) DEFAULT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 并发计数用（无主键，纯追加）
CREATE TABLE mi_bulk_1 (v INT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE mi_bulk_2 (v INT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE mi_bulk_3 (v INT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE mi_bulk_4 (v INT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE mi_bulk_5 (v INT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE mi_bulk_6 (v INT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 预置行（供 06 / 07 用例使用）
INSERT INTO mi_repo (id, v, tag) VALUES (1, 10, 'seed1'), (2, 20, 'seed2'), (3, 30, 'seed3');
INSERT INTO mi_parent (id, name) VALUES (1, 'seed-parent');

-- 注意：mi_no_such_table 故意不创建，04 用例会用它在制造「表不存在」的失败。
