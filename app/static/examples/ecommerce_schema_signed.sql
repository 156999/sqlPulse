-- SQL Pulse 电商压测与造数验证库（有符号数版本）
-- MySQL 8.0.16+；本脚本会创建并使用 sqlpulse_ecommerce Schema。

CREATE DATABASE IF NOT EXISTS sqlpulse_ecommerce
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE sqlpulse_ecommerce;
SET NAMES utf8mb4;
SET time_zone = '+08:00';

CREATE TABLE IF NOT EXISTS ec_users (
  id BIGINT NOT NULL AUTO_INCREMENT,
  username VARCHAR(32) NOT NULL,
  email VARCHAR(128) NOT NULL,
  mobile VARCHAR(20) DEFAULT NULL,
  password_hash VARCHAR(255) NOT NULL DEFAULT '',
  gender ENUM('unknown', 'male', 'female') NOT NULL DEFAULT 'unknown',
  age TINYINT DEFAULT NULL,
  level TINYINT NOT NULL DEFAULT 1,
  status ENUM('active', 'disabled', 'pending') NOT NULL DEFAULT 'active',
  registered_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_users_username (username),
  UNIQUE KEY uk_ec_users_email (email),
  UNIQUE KEY uk_ec_users_mobile (mobile),
  KEY idx_ec_users_registered_at (registered_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_user_addresses (
  id BIGINT NOT NULL AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  receiver_name VARCHAR(40) NOT NULL,
  receiver_mobile VARCHAR(20) NOT NULL,
  province VARCHAR(32) NOT NULL,
  city VARCHAR(32) NOT NULL,
  district VARCHAR(32) NOT NULL,
  detail_address VARCHAR(200) NOT NULL,
  postal_code VARCHAR(12) DEFAULT NULL,
  is_default TINYINT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_ec_addresses_user (user_id),
  CONSTRAINT fk_ec_addresses_user FOREIGN KEY (user_id) REFERENCES ec_users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_categories (
  id BIGINT NOT NULL AUTO_INCREMENT,
  parent_id BIGINT DEFAULT NULL,
  category_name VARCHAR(64) NOT NULL,
  category_code VARCHAR(32) NOT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  enabled TINYINT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_categories_code (category_code),
  KEY idx_ec_categories_parent (parent_id),
  CONSTRAINT fk_ec_categories_parent FOREIGN KEY (parent_id) REFERENCES ec_categories (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_products (
  id BIGINT NOT NULL AUTO_INCREMENT,
  category_id BIGINT NOT NULL,
  product_code VARCHAR(40) NOT NULL,
  product_name VARCHAR(120) NOT NULL,
  subtitle VARCHAR(200) DEFAULT NULL,
  brand VARCHAR(60) DEFAULT NULL,
  status ENUM('draft', 'online', 'offline') NOT NULL DEFAULT 'draft',
  min_price DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  max_price DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  sales_count INT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_products_code (product_code),
  KEY idx_ec_products_category_status (category_id, status),
  CONSTRAINT fk_ec_products_category FOREIGN KEY (category_id) REFERENCES ec_categories (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_product_skus (
  id BIGINT NOT NULL AUTO_INCREMENT,
  product_id BIGINT NOT NULL,
  sku_code VARCHAR(48) NOT NULL,
  sku_name VARCHAR(160) NOT NULL,
  attributes JSON DEFAULT NULL,
  sale_price DECIMAL(12,2) NOT NULL,
  market_price DECIMAL(12,2) NOT NULL,
  stock_qty INT NOT NULL DEFAULT 0,
  locked_qty INT NOT NULL DEFAULT 0,
  status ENUM('enabled', 'disabled') NOT NULL DEFAULT 'enabled',
  available_qty INT GENERATED ALWAYS AS (stock_qty - locked_qty) STORED,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_skus_code (sku_code),
  UNIQUE KEY uk_ec_skus_product_name (product_id, sku_name),
  KEY idx_ec_skus_product_status (product_id, status),
  CONSTRAINT fk_ec_skus_product FOREIGN KEY (product_id) REFERENCES ec_products (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_orders (
  id BIGINT NOT NULL AUTO_INCREMENT,
  order_no VARCHAR(32) NOT NULL,
  user_id BIGINT NOT NULL,
  address_id BIGINT NOT NULL,
  status ENUM('created', 'paid', 'shipped', 'completed', 'cancelled', 'refunded') NOT NULL DEFAULT 'created',
  channel ENUM('web', 'app', 'mini_program', 'api') NOT NULL DEFAULT 'web',
  total_amount DECIMAL(14,2) NOT NULL,
  discount_amount DECIMAL(14,2) NOT NULL DEFAULT 0.00,
  freight_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  payable_amount DECIMAL(14,2) NOT NULL,
  paid_amount DECIMAL(14,2) NOT NULL DEFAULT 0.00,
  item_count SMALLINT NOT NULL,
  buyer_remark VARCHAR(200) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  paid_at DATETIME(3) DEFAULT NULL,
  completed_at DATETIME(3) DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_orders_no (order_no),
  KEY idx_ec_orders_user_created (user_id, created_at),
  KEY idx_ec_orders_status_created (status, created_at),
  CONSTRAINT fk_ec_orders_user FOREIGN KEY (user_id) REFERENCES ec_users (id),
  CONSTRAINT fk_ec_orders_address FOREIGN KEY (address_id) REFERENCES ec_user_addresses (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_order_items (
  id BIGINT NOT NULL AUTO_INCREMENT,
  order_id BIGINT NOT NULL,
  sku_id BIGINT NOT NULL,
  product_name VARCHAR(120) NOT NULL,
  sku_name VARCHAR(160) NOT NULL,
  unit_price DECIMAL(12,2) NOT NULL,
  quantity SMALLINT NOT NULL,
  discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  line_amount DECIMAL(14,2) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_order_items_order_sku (order_id, sku_id),
  KEY idx_ec_order_items_sku (sku_id),
  CONSTRAINT fk_ec_order_items_order FOREIGN KEY (order_id) REFERENCES ec_orders (id),
  CONSTRAINT fk_ec_order_items_sku FOREIGN KEY (sku_id) REFERENCES ec_product_skus (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_payments (
  id BIGINT NOT NULL AUTO_INCREMENT,
  payment_no VARCHAR(40) NOT NULL,
  order_id BIGINT NOT NULL,
  payment_method ENUM('alipay', 'wechat', 'bank_card', 'balance') NOT NULL,
  status ENUM('pending', 'success', 'failed', 'refunded') NOT NULL DEFAULT 'pending',
  amount DECIMAL(14,2) NOT NULL,
  transaction_no VARCHAR(80) DEFAULT NULL,
  paid_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_payments_no (payment_no),
  UNIQUE KEY uk_ec_payments_transaction (transaction_no),
  KEY idx_ec_payments_order (order_id),
  CONSTRAINT fk_ec_payments_order FOREIGN KEY (order_id) REFERENCES ec_orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ec_inventory_logs (
  id BIGINT NOT NULL AUTO_INCREMENT,
  sku_id BIGINT NOT NULL,
  order_id BIGINT DEFAULT NULL,
  change_type ENUM('purchase', 'sale', 'cancel', 'refund', 'adjust') NOT NULL,
  quantity_delta INT NOT NULL,
  stock_after INT NOT NULL,
  reference_no VARCHAR(48) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_inventory_reference (sku_id, reference_no),
  KEY idx_ec_inventory_order (order_id),
  CONSTRAINT fk_ec_inventory_sku FOREIGN KEY (sku_id) REFERENCES ec_product_skus (id),
  CONSTRAINT fk_ec_inventory_order FOREIGN KEY (order_id) REFERENCES ec_orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO ec_users
  (id, username, email, mobile, gender, age, level, status, registered_at)
VALUES
  (1, 'alice', 'alice@example.test', '13800000001', 'female', 28, 3, 'active', '2026-01-03 09:20:00'),
  (2, 'bob', 'bob@example.test', '13800000002', 'male', 35, 5, 'active', '2026-02-12 12:10:00'),
  (3, 'carol', 'carol@example.test', '13800000003', 'female', 22, 2, 'active', '2026-03-08 18:30:00');

INSERT IGNORE INTO ec_user_addresses
  (id, user_id, receiver_name, receiver_mobile, province, city, district, detail_address, postal_code, is_default)
VALUES
  (1, 1, '张晓雨', '13800000001', '浙江省', '杭州市', '西湖区', '文三路 100 号', '310000', 1),
  (2, 2, '李明', '13800000002', '广东省', '深圳市', '南山区', '科技园 8 号', '518000', 1),
  (3, 3, '王佳', '13800000003', '四川省', '成都市', '高新区', '天府大道 66 号', '610000', 1);

INSERT IGNORE INTO ec_categories (id, parent_id, category_name, category_code, sort_order, enabled) VALUES
  (1, NULL, '数码家电', 'digital', 10, 1),
  (2, 1, '手机通讯', 'mobile', 11, 1),
  (3, 1, '电脑办公', 'computer', 12, 1);

INSERT IGNORE INTO ec_products
  (id, category_id, product_code, product_name, subtitle, brand, status, min_price, max_price, sales_count)
VALUES
  (1, 2, 'SPU-PHONE-001', '轻旗舰智能手机', '高刷新率屏幕与长续航', 'North', 'online', 2999.00, 3599.00, 1250),
  (2, 3, 'SPU-LAPTOP-001', '轻薄商务笔记本', '14 英寸高性能办公本', 'River', 'online', 5499.00, 6999.00, 620);

INSERT IGNORE INTO ec_product_skus
  (id, product_id, sku_code, sku_name, attributes, sale_price, market_price, stock_qty, locked_qty, status)
VALUES
  (1, 1, 'SKU-PHONE-BLK-128', '曜石黑 128GB', JSON_OBJECT('color','black','storage','128GB'), 2999.00, 3299.00, 500, 12, 'enabled'),
  (2, 1, 'SKU-PHONE-BLU-256', '海湾蓝 256GB', JSON_OBJECT('color','blue','storage','256GB'), 3599.00, 3899.00, 320, 8, 'enabled'),
  (3, 2, 'SKU-LAPTOP-I5-16', 'i5 16GB 512GB', JSON_OBJECT('cpu','i5','memory','16GB'), 5499.00, 5999.00, 180, 5, 'enabled');

INSERT IGNORE INTO ec_orders
  (id, order_no, user_id, address_id, status, channel, total_amount, discount_amount, freight_amount,
   payable_amount, paid_amount, item_count, buyer_remark, created_at, paid_at, completed_at)
VALUES
  (1, 'EC202609010001', 1, 1, 'completed', 'app', 2999.00, 100.00, 0.00, 2899.00, 2899.00, 1, '工作日送达', '2026-09-01 10:00:00', '2026-09-01 10:02:00', '2026-09-04 16:20:00'),
  (2, 'EC202609020001', 2, 2, 'paid', 'web', 3599.00, 0.00, 0.00, 3599.00, 3599.00, 1, NULL, '2026-09-02 11:20:00', '2026-09-02 11:22:00', NULL);

INSERT IGNORE INTO ec_order_items
  (id, order_id, sku_id, product_name, sku_name, unit_price, quantity, discount_amount, line_amount)
VALUES
  (1, 1, 1, '轻旗舰智能手机', '曜石黑 128GB', 2999.00, 1, 100.00, 2899.00),
  (2, 2, 2, '轻旗舰智能手机', '海湾蓝 256GB', 3599.00, 1, 0.00, 3599.00);

INSERT IGNORE INTO ec_payments
  (id, payment_no, order_id, payment_method, status, amount, transaction_no, paid_at, created_at)
VALUES
  (1, 'PAY202609010001', 1, 'alipay', 'success', 2899.00, 'ALI20260901000001', '2026-09-01 10:02:00', '2026-09-01 10:01:00'),
  (2, 'PAY202609020001', 2, 'wechat', 'success', 3599.00, 'WX20260902000001', '2026-09-02 11:22:00', '2026-09-02 11:21:00');

INSERT IGNORE INTO ec_inventory_logs
  (id, sku_id, order_id, change_type, quantity_delta, stock_after, reference_no, created_at)
VALUES
  (1, 1, 1, 'sale', -1, 499, 'EC202609010001', '2026-09-01 10:02:00'),
  (2, 2, 2, 'sale', -1, 319, 'EC202609020001', '2026-09-02 11:22:00');

SELECT DATABASE() AS current_schema;
SELECT 'ec_users' AS table_name, COUNT(*) AS row_count FROM ec_users
UNION ALL SELECT 'ec_products', COUNT(*) FROM ec_products
UNION ALL SELECT 'ec_product_skus', COUNT(*) FROM ec_product_skus
UNION ALL SELECT 'ec_orders', COUNT(*) FROM ec_orders
UNION ALL SELECT 'ec_order_items', COUNT(*) FROM ec_order_items
UNION ALL SELECT 'ec_payments', COUNT(*) FROM ec_payments;
