USE `sqlpulse_ecommerce`;

ALTER TABLE `ec_users` DROP CHECK `chk_ec_users_age`;
ALTER TABLE `ec_users` DROP CHECK `chk_ec_users_level`;
ALTER TABLE `ec_user_addresses` DROP CHECK `chk_ec_addresses_default`;
ALTER TABLE `ec_categories` DROP CHECK `chk_ec_categories_enabled`;
ALTER TABLE `ec_products` DROP CHECK `chk_ec_products_price`;
ALTER TABLE `ec_products` DROP CHECK `chk_ec_products_sales`;
ALTER TABLE `ec_product_skus` DROP CHECK `chk_ec_skus_price`;
ALTER TABLE `ec_product_skus` DROP CHECK `chk_ec_skus_stock`;
ALTER TABLE `ec_orders` DROP CHECK `chk_ec_orders_amount`;
ALTER TABLE `ec_orders` DROP CHECK `chk_ec_orders_item_count`;
ALTER TABLE `ec_order_items` DROP CHECK `chk_ec_order_items_quantity`;
ALTER TABLE `ec_order_items` DROP CHECK `chk_ec_order_items_amount`;
ALTER TABLE `ec_payments` DROP CHECK `chk_ec_payments_amount`;
ALTER TABLE `ec_inventory_logs` DROP CHECK `chk_ec_inventory_delta`;
ALTER TABLE `ec_inventory_logs` DROP CHECK `chk_ec_inventory_stock`;
