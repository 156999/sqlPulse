-- SQL Pulse 电商压测与造数验证库
-- 适用版本：MySQL 8.0+
-- 使用方式：先选择目标数据库，再执行本文件。本脚本不会创建、删除或清空数据库。

SET NAMES utf8mb4;
SET time_zone = '+08:00';

CREATE TABLE IF NOT EXISTS ec_users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  username VARCHAR(32) NOT NULL COMMENT '登录名',
  email VARCHAR(128) NOT NULL COMMENT '邮箱',
  mobile VARCHAR(20) DEFAULT NULL COMMENT '手机号',
  password_hash VARCHAR(255) NOT NULL DEFAULT '' COMMENT '密码摘要，造数时应跳过',
  gender ENUM('unknown', 'male', 'female') NOT NULL DEFAULT 'unknown',
  age TINYINT UNSIGNED DEFAULT NULL,
  level TINYINT UNSIGNED NOT NULL DEFAULT 1,
  status ENUM('active', 'disabled', 'pending') NOT NULL DEFAULT 'active',
  registered_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_users_username (username),
  UNIQUE KEY uk_ec_users_email (email),
  UNIQUE KEY uk_ec_users_mobile (mobile),
  KEY idx_ec_users_registered_at (registered_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='电商用户';

CREATE TABLE IF NOT EXISTS ec_user_addresses (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  receiver_name VARCHAR(40) NOT NULL,
  receiver_mobile VARCHAR(20) NOT NULL,
  province VARCHAR(32) NOT NULL,
  city VARCHAR(32) NOT NULL,
  district VARCHAR(32) NOT NULL,
  detail_address VARCHAR(200) NOT NULL,
  postal_code VARCHAR(12) DEFAULT NULL,
  is_default TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_ec_addresses_user (user_id),
  CONSTRAINT fk_ec_addresses_user FOREIGN KEY (user_id) REFERENCES ec_users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户收货地址';

CREATE TABLE IF NOT EXISTS ec_categories (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  parent_id BIGINT UNSIGNED DEFAULT NULL,
  category_name VARCHAR(64) NOT NULL,
  category_code VARCHAR(32) NOT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_categories_code (category_code),
  KEY idx_ec_categories_parent (parent_id),
  CONSTRAINT fk_ec_categories_parent FOREIGN KEY (parent_id) REFERENCES ec_categories (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品分类';

CREATE TABLE IF NOT EXISTS ec_products (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  category_id BIGINT UNSIGNED NOT NULL,
  product_code VARCHAR(40) NOT NULL,
  product_name VARCHAR(120) NOT NULL,
  subtitle VARCHAR(200) DEFAULT NULL,
  brand VARCHAR(60) DEFAULT NULL,
  status ENUM('draft', 'online', 'offline') NOT NULL DEFAULT 'draft',
  min_price DECIMAL(12,2) UNSIGNED NOT NULL DEFAULT 0.00,
  max_price DECIMAL(12,2) UNSIGNED NOT NULL DEFAULT 0.00,
  sales_count INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_products_code (product_code),
  KEY idx_ec_products_category_status (category_id, status),
  KEY idx_ec_products_created_at (created_at),
  CONSTRAINT fk_ec_products_category FOREIGN KEY (category_id) REFERENCES ec_categories (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品SPU';

CREATE TABLE IF NOT EXISTS ec_product_skus (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  product_id BIGINT UNSIGNED NOT NULL,
  sku_code VARCHAR(48) NOT NULL,
  sku_name VARCHAR(160) NOT NULL,
  attributes JSON DEFAULT NULL,
  sale_price DECIMAL(12,2) UNSIGNED NOT NULL,
  market_price DECIMAL(12,2) UNSIGNED NOT NULL,
  stock_qty INT UNSIGNED NOT NULL DEFAULT 0,
  locked_qty INT UNSIGNED NOT NULL DEFAULT 0,
  status ENUM('enabled', 'disabled') NOT NULL DEFAULT 'enabled',
  available_qty INT GENERATED ALWAYS AS (stock_qty - locked_qty) STORED,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_skus_code (sku_code),
  UNIQUE KEY uk_ec_skus_product_name (product_id, sku_name),
  KEY idx_ec_skus_product_status (product_id, status),
  CONSTRAINT fk_ec_skus_product FOREIGN KEY (product_id) REFERENCES ec_products (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品SKU';

CREATE TABLE IF NOT EXISTS ec_orders (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no VARCHAR(32) NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  address_id BIGINT UNSIGNED NOT NULL,
  status ENUM('created', 'paid', 'shipped', 'completed', 'cancelled', 'refunded') NOT NULL DEFAULT 'created',
  channel ENUM('web', 'app', 'mini_program', 'api') NOT NULL DEFAULT 'web',
  total_amount DECIMAL(14,2) UNSIGNED NOT NULL,
  discount_amount DECIMAL(14,2) UNSIGNED NOT NULL DEFAULT 0.00,
  freight_amount DECIMAL(12,2) UNSIGNED NOT NULL DEFAULT 0.00,
  payable_amount DECIMAL(14,2) UNSIGNED NOT NULL,
  paid_amount DECIMAL(14,2) UNSIGNED NOT NULL DEFAULT 0.00,
  item_count SMALLINT UNSIGNED NOT NULL,
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='订单主表';

CREATE TABLE IF NOT EXISTS ec_order_items (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id BIGINT UNSIGNED NOT NULL,
  sku_id BIGINT UNSIGNED NOT NULL,
  product_name VARCHAR(120) NOT NULL,
  sku_name VARCHAR(160) NOT NULL,
  unit_price DECIMAL(12,2) UNSIGNED NOT NULL,
  quantity SMALLINT UNSIGNED NOT NULL,
  discount_amount DECIMAL(12,2) UNSIGNED NOT NULL DEFAULT 0.00,
  line_amount DECIMAL(14,2) UNSIGNED NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_order_items_order_sku (order_id, sku_id),
  KEY idx_ec_order_items_sku (sku_id),
  CONSTRAINT fk_ec_order_items_order FOREIGN KEY (order_id) REFERENCES ec_orders (id),
  CONSTRAINT fk_ec_order_items_sku FOREIGN KEY (sku_id) REFERENCES ec_product_skus (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='订单明细';

CREATE TABLE IF NOT EXISTS ec_payments (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  payment_no VARCHAR(40) NOT NULL,
  order_id BIGINT UNSIGNED NOT NULL,
  payment_method ENUM('alipay', 'wechat', 'bank_card', 'balance') NOT NULL,
  status ENUM('pending', 'success', 'failed', 'refunded') NOT NULL DEFAULT 'pending',
  amount DECIMAL(14,2) UNSIGNED NOT NULL,
  transaction_no VARCHAR(80) DEFAULT NULL,
  paid_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_payments_no (payment_no),
  UNIQUE KEY uk_ec_payments_transaction (transaction_no),
  KEY idx_ec_payments_order (order_id),
  KEY idx_ec_payments_status_created (status, created_at),
  CONSTRAINT fk_ec_payments_order FOREIGN KEY (order_id) REFERENCES ec_orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='支付流水';

CREATE TABLE IF NOT EXISTS ec_inventory_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  order_id BIGINT UNSIGNED DEFAULT NULL,
  change_type ENUM('purchase', 'sale', 'cancel', 'refund', 'adjust') NOT NULL,
  quantity_delta INT NOT NULL,
  stock_after INT UNSIGNED NOT NULL,
  reference_no VARCHAR(48) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ec_inventory_reference (sku_id, reference_no),
  KEY idx_ec_inventory_order (order_id),
  KEY idx_ec_inventory_created (created_at),
  CONSTRAINT fk_ec_inventory_sku FOREIGN KEY (sku_id) REFERENCES ec_product_skus (id),
  CONSTRAINT fk_ec_inventory_order FOREIGN KEY (order_id) REFERENCES ec_orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='库存流水';

-- 基础样本数据：用于验证 sample、外键识别和分布抽样。
INSERT IGNORE INTO ec_users
  (id, username, email, mobile, gender, age, level, status, registered_at)
VALUES
  (1, 'alice', 'alice@example.test', '13800000001', 'female', 28, 3, 'active', '2026-01-03 09:20:00'),
  (2, 'bob', 'bob@example.test', '13800000002', 'male', 35, 5, 'active', '2026-02-12 12:10:00'),
  (3, 'carol', 'carol@example.test', '13800000003', 'female', 22, 2, 'active', '2026-03-08 18:30:00'),
  (4, 'david', 'david@example.test', '13800000004', 'male', 41, 6, 'active', '2026-04-21 08:05:00'),
  (5, 'eve', 'eve@example.test', '13800000005', 'unknown', 30, 1, 'pending', '2026-05-16 14:45:00');

INSERT IGNORE INTO ec_user_addresses
  (id, user_id, receiver_name, receiver_mobile, province, city, district, detail_address, postal_code, is_default)
VALUES
  (1, 1, '张晓雨', '13800000001', '浙江省', '杭州市', '西湖区', '文三路 100 号', '310000', 1),
  (2, 2, '李明', '13800000002', '广东省', '深圳市', '南山区', '科技园 8 号', '518000', 1),
  (3, 3, '王佳', '13800000003', '四川省', '成都市', '高新区', '天府大道 66 号', '610000', 1),
  (4, 4, '陈立', '13800000004', '上海市', '上海市', '浦东新区', '张江路 18 号', '200120', 1),
  (5, 5, '周宁', '13800000005', '北京市', '北京市', '海淀区', '中关村大街 9 号', '100080', 1);

INSERT IGNORE INTO ec_categories (id, parent_id, category_name, category_code, sort_order, enabled) VALUES
  (1, NULL, '数码家电', 'digital', 10, 1),
  (2, 1, '手机通讯', 'mobile', 11, 1),
  (3, 1, '电脑办公', 'computer', 12, 1),
  (4, NULL, '家居生活', 'home', 20, 1),
  (5, 4, '厨房用品', 'kitchen', 21, 1);

INSERT IGNORE INTO ec_products
  (id, category_id, product_code, product_name, subtitle, brand, status, min_price, max_price, sales_count)
VALUES
  (1, 2, 'SPU-PHONE-001', '轻旗舰智能手机', '高刷新率屏幕与长续航', 'North', 'online', 2999.00, 3599.00, 1250),
  (2, 3, 'SPU-LAPTOP-001', '轻薄商务笔记本', '14 英寸高性能办公本', 'River', 'online', 5499.00, 6999.00, 620),
  (3, 5, 'SPU-CUP-001', '真空保温杯', '食品级不锈钢内胆', 'Morning', 'online', 89.00, 129.00, 4380);

INSERT IGNORE INTO ec_product_skus
  (id, product_id, sku_code, sku_name, attributes, sale_price, market_price, stock_qty, locked_qty, status)
VALUES
  (1, 1, 'SKU-PHONE-BLK-128', '曜石黑 128GB', JSON_OBJECT('color','black','storage','128GB'), 2999.00, 3299.00, 500, 12, 'enabled'),
  (2, 1, 'SKU-PHONE-BLU-256', '海湾蓝 256GB', JSON_OBJECT('color','blue','storage','256GB'), 3599.00, 3899.00, 320, 8, 'enabled'),
  (3, 2, 'SKU-LAPTOP-I5-16', 'i5 16GB 512GB', JSON_OBJECT('cpu','i5','memory','16GB'), 5499.00, 5999.00, 180, 5, 'enabled'),
  (4, 2, 'SKU-LAPTOP-I7-32', 'i7 32GB 1TB', JSON_OBJECT('cpu','i7','memory','32GB'), 6999.00, 7599.00, 90, 2, 'enabled'),
  (5, 3, 'SKU-CUP-WHT-500', '云白色 500ml', JSON_OBJECT('color','white','capacity','500ml'), 89.00, 109.00, 1200, 20, 'enabled'),
  (6, 3, 'SKU-CUP-BLK-750', '墨黑色 750ml', JSON_OBJECT('color','black','capacity','750ml'), 129.00, 159.00, 800, 15, 'enabled');

INSERT IGNORE INTO ec_orders
  (id, order_no, user_id, address_id, status, channel, total_amount, discount_amount, freight_amount,
   payable_amount, paid_amount, item_count, buyer_remark, created_at, paid_at, completed_at)
VALUES
  (1, 'EC202609010001', 1, 1, 'completed', 'app', 2999.00, 100.00, 0.00, 2899.00, 2899.00, 1, '工作日送达', '2026-09-01 10:00:00', '2026-09-01 10:02:00', '2026-09-04 16:20:00'),
  (2, 'EC202609020001', 2, 2, 'paid', 'web', 178.00, 0.00, 0.00, 178.00, 178.00, 2, NULL, '2026-09-02 11:20:00', '2026-09-02 11:22:00', NULL),
  (3, 'EC202609030001', 3, 3, 'created', 'mini_program', 5499.00, 200.00, 0.00, 5299.00, 0.00, 1, NULL, '2026-09-03 19:05:00', NULL, NULL);

INSERT IGNORE INTO ec_order_items
  (id, order_id, sku_id, product_name, sku_name, unit_price, quantity, discount_amount, line_amount)
VALUES
  (1, 1, 1, '轻旗舰智能手机', '曜石黑 128GB', 2999.00, 1, 100.00, 2899.00),
  (2, 2, 5, '真空保温杯', '云白色 500ml', 89.00, 2, 0.00, 178.00),
  (3, 3, 3, '轻薄商务笔记本', 'i5 16GB 512GB', 5499.00, 1, 200.00, 5299.00);

INSERT IGNORE INTO ec_payments
  (id, payment_no, order_id, payment_method, status, amount, transaction_no, paid_at, created_at)
VALUES
  (1, 'PAY202609010001', 1, 'alipay', 'success', 2899.00, 'ALI20260901000001', '2026-09-01 10:02:00', '2026-09-01 10:01:00'),
  (2, 'PAY202609020001', 2, 'wechat', 'success', 178.00, 'WX20260902000001', '2026-09-02 11:22:00', '2026-09-02 11:21:00'),
  (3, 'PAY202609030001', 3, 'bank_card', 'pending', 5299.00, NULL, NULL, '2026-09-03 19:06:00');

INSERT IGNORE INTO ec_inventory_logs
  (id, sku_id, order_id, change_type, quantity_delta, stock_after, reference_no, created_at)
VALUES
  (1, 1, 1, 'sale', -1, 499, 'EC202609010001', '2026-09-01 10:02:00'),
  (2, 5, 2, 'sale', -2, 1198, 'EC202609020001', '2026-09-02 11:22:00'),
  (3, 3, 3, 'sale', -1, 179, 'EC202609030001', '2026-09-03 19:06:00');

-- 导入后可用于快速确认对象是否就绪。
SELECT 'ec_users' AS table_name, COUNT(*) AS row_count FROM ec_users
UNION ALL SELECT 'ec_products', COUNT(*) FROM ec_products
UNION ALL SELECT 'ec_product_skus', COUNT(*) FROM ec_product_skus
UNION ALL SELECT 'ec_orders', COUNT(*) FROM ec_orders
UNION ALL SELECT 'ec_order_items', COUNT(*) FROM ec_order_items
UNION ALL SELECT 'ec_payments', COUNT(*) FROM ec_payments;
