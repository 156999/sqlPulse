from app.services.datagen_rules import apply_database_rules, list_table_names, validate_field_rules


def test_list_table_names_uses_current_schema_and_fuzzy_query():
    executed = []

    class Cursor:
        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchall(self):
            return [("ec_orders",), ("ec_order_items",)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    dsn = {"host": "db", "port": 3306, "user": "u", "password": "p", "database": "shop"}
    assert list_table_names(dsn, "order", connect=lambda **kwargs: Connection()) == ["ec_orders", "ec_order_items"]
    assert "TABLE_TYPE='BASE TABLE'" in executed[0][0]
    assert "TABLE_NAME LIKE %s" in executed[0][0]
    assert executed[0][1] == ("shop", "%order%")


def test_validate_field_rules_rejects_database_limit_breaks():
    metadata = {
        "columns": [
            {"name": "email", "nullable": False, "max_length": 20, "enum_values": []},
            {"name": "status", "nullable": False, "max_length": None, "enum_values": ["new", "paid"]},
        ],
        "unique_indexes": [{"name": "uk_email", "columns": ["email"]}],
    }
    errors = validate_field_rules(
        metadata,
        [
            {"column": "email", "nullable": False, "null_ratio": 0.1, "length": {"max": 64}},
            {"column": "status", "values": ["new", "bad"]},
        ],
        row_count=10,
    )
    messages = {error["message"] for error in errors}
    assert "NOT NULL 字段不能配置空值比例" in messages
    assert "长度上限不能超过数据库限制 20" in messages
    assert any("枚举值不在数据库允许范围内" in message for message in messages)


def test_validate_field_rules_checks_unique_space_and_joint_unique():
    metadata = {
        "columns": [
            {"name": "tenant_id", "nullable": False, "enum_values": []},
            {"name": "code", "nullable": False, "enum_values": []},
        ],
        "unique_indexes": [{"name": "uk_tenant_code", "columns": ["tenant_id", "code"]}],
    }
    errors = validate_field_rules(
        metadata,
        [{"column": "tenant_id", "generator": "fixed"}, {"column": "code", "unique": True, "values": ["a"]}],
        row_count=2,
    )
    assert any(error["column"] == "code" and "唯一值空间" in error["message"] for error in errors)


def test_apply_database_rules_clamps_user_parameters_and_reports_changes():
    metadata = {
        "table": "users",
        "columns": [
            {"name": "age", "data_type": "tinyint", "column_type": "tinyint unsigned", "nullable": False,
             "default": None, "extra": "", "key": "", "enum_values": []},
            {"name": "nickname", "data_type": "varchar", "column_type": "varchar(20)", "nullable": False,
             "default": None, "extra": "", "key": "", "max_length": 20, "enum_values": []},
        ],
        "unique_indexes": [], "foreign_keys": [],
    }
    result = apply_database_rules(metadata, [
        {"column": "age", "generator": "rand", "params": {"min": -9, "max": 999}, "null_ratio": 10},
        {"column": "nickname", "generator": "randstr", "params": {"max_length": 100}},
    ])
    rules = {rule["column"]: rule for rule in result["rules"]}
    assert rules["age"]["params"] == {"min": 0, "max": 255}
    assert rules["age"]["null_ratio"] == 0
    assert rules["nickname"]["params"]["max_length"] == 20
    assert result["ok"] is True
    assert len(result["changes"]) == 4


def test_apply_database_rules_uses_foreign_key_sample_source():
    metadata = {
        "table": "orders",
        "columns": [{"name": "user_id", "data_type": "int", "column_type": "int", "nullable": False,
                     "default": None, "extra": "", "key": "", "enum_values": []}],
        "unique_indexes": [],
        "foreign_keys": [{"COLUMN_NAME": "user_id", "REFERENCED_TABLE_NAME": "users", "REFERENCED_COLUMN_NAME": "id"}],
    }
    result = apply_database_rules(metadata, [
        {"column": "user_id", "generator": "rand", "params": {"min": 1, "max": 999}, "sample_ratio": 100},
    ])
    rule = result["rules"][0]
    assert rule["generator"] == "sample"
    assert rule["sample_source"] == {"table": "users", "column": "id"}


def test_foreign_keys_in_compound_unique_index_keep_sample():
    metadata = {
        "table": "order_items",
        "columns": [
            {"name": "order_id", "data_type": "bigint", "column_type": "bigint", "nullable": False,
             "default": None, "extra": "", "key": "MUL", "enum_values": []},
            {"name": "sku_id", "data_type": "bigint", "column_type": "bigint", "nullable": False,
             "default": None, "extra": "", "key": "MUL", "enum_values": []},
        ],
        "unique_indexes": [{"name": "uk_order_sku", "columns": ["order_id", "sku_id"]}],
        "foreign_keys": [
            {"COLUMN_NAME": "order_id", "REFERENCED_TABLE_NAME": "orders", "REFERENCED_COLUMN_NAME": "id"},
            {"COLUMN_NAME": "sku_id", "REFERENCED_TABLE_NAME": "skus", "REFERENCED_COLUMN_NAME": "id"},
        ],
    }
    result = apply_database_rules(metadata, [
        {"column": "order_id", "generator": "rand", "params": {"min": 1, "max": 100}},
        {"column": "sku_id", "generator": "rand", "params": {"min": 1, "max": 100}},
    ])
    rules = {rule["column"]: rule for rule in result["rules"]}
    assert rules["order_id"]["generator"] == "sample"
    assert rules["sku_id"]["generator"] == "sample"
    assert not any(change["after"] == "rand" and "唯一字段不能" in change["reason"] for change in result["changes"])


def test_apply_database_rules_disables_sample_for_unique_column():
    metadata = {
        "table": "categories",
        "columns": [{"name": "category_code", "data_type": "varchar", "column_type": "varchar(32)",
                     "nullable": False, "default": None, "extra": "", "key": "UNI", "max_length": 32,
                     "enum_values": []}],
        "unique_indexes": [{"name": "uk_code", "columns": ["category_code"]}],
        "foreign_keys": [],
    }
    result = apply_database_rules(metadata, [{
        "column": "category_code", "generator": "sample", "base_generator": "randstr",
        "params": {"max_length": 32}, "sample_ratio": 80,
    }])
    rule = result["rules"][0]
    assert rule["generator"] == "randstr"
    assert rule["sample_ratio"] == 0
    assert any("唯一字段不能" in change["reason"] for change in result["changes"])


def test_apply_database_rules_detects_safe_derived_check_expression():
    metadata = {
        "table": "order_items",
        "columns": [
            {"name": "unit_price", "data_type": "decimal", "column_type": "decimal(12,2)", "nullable": False,
             "default": None, "extra": "", "key": "", "numeric_precision": 12, "numeric_scale": 2, "enum_values": []},
            {"name": "quantity", "data_type": "smallint", "column_type": "smallint", "nullable": False,
             "default": None, "extra": "", "key": "", "enum_values": []},
            {"name": "discount_amount", "data_type": "decimal", "column_type": "decimal(12,2)", "nullable": False,
             "default": "0.00", "extra": "", "key": "", "numeric_precision": 12, "numeric_scale": 2, "enum_values": []},
            {"name": "line_amount", "data_type": "decimal", "column_type": "decimal(14,2)", "nullable": False,
             "default": None, "extra": "", "key": "", "numeric_precision": 14, "numeric_scale": 2, "enum_values": []},
        ],
        "unique_indexes": [], "foreign_keys": [],
        "checks": [{"CONSTRAINT_NAME": "chk_amount", "CHECK_CLAUSE": "(`line_amount` = ((`unit_price` * `quantity`) - `discount_amount`))"}],
    }
    rules = [
        {"column": "unit_price", "generator": "randf", "params": {"min": 0, "max": 1000, "digits": 2}},
        {"column": "quantity", "generator": "rand", "params": {"min": 1, "max": 100}},
        {"column": "discount_amount", "generator": "randf", "params": {"min": 0, "max": 10, "digits": 2}},
        {"column": "line_amount", "generator": "randf", "params": {"min": 0, "max": 1000, "digits": 2}},
    ]
    result = apply_database_rules(metadata, rules)
    line_rule = next(rule for rule in result["rules"] if rule["column"] == "line_amount")
    assert line_rule["generator"] == "derived"
    assert line_rule["dependencies"] == ["discount_amount", "quantity", "unit_price"]
    assert "unit_price" in line_rule["derived_expression"]
