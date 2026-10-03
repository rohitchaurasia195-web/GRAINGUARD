import json
from pathlib import Path

import pytest

from grainguard.cli import main
from grainguard.dbt import analyze_project, find_refs, load_project, strip_jinja
from grainguard.facts import facts_from_dbt_yaml, parse_pragmas

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "jaffle_fanout"


def by_name(results):
    return {r.name: r for r in results}


def test_example_project_findings_are_exactly_the_expected_ones():
    project = load_project(EXAMPLE)
    assert project.kind == "dbt"
    assert project.dialect == "snowflake"
    results = by_name(analyze_project(project))

    bad = results["customer_revenue_bad"]
    assert sorted(f.aggregate for f in bad.errors) == ["COUNT(*)", "SUM(o.amount)"]
    assert all(f.code == "FANOUT_AGG" for f in bad.errors)

    chasm = results["chasm_trap_bad"]
    assert len(chasm.errors) == 2 and all(f.code == "CHASM_TRAP" for f in chasm.errors)

    warn = results["unknown_grain_warn"]
    assert [f.code for f in warn.findings] == ["UNKNOWN_GRAIN"]
    assert warn.findings[0].severity == "warning"

    for good in ("customer_revenue_good", "latest_payment_per_order_good", "revenue_in_usd_good"):
        assert results[good].findings == [], good


def test_staging_grain_is_inferred_through_rename_from_source_unique_tests():
    project = load_project(EXAMPLE)
    results = by_name(analyze_project(project))
    assert frozenset({"order_id"}) in results["stg_orders"].output.unique_keys
    assert frozenset({"payment_id"}) in results["stg_payments"].output.unique_keys
    assert results["stg_orders"].output.columns == ["order_id", "customer_id", "order_date", "status", "amount"]


def test_strip_jinja_handles_ref_source_config_and_blocks():
    sql = """
    {{ config(materialized='incremental', unique_key='id') }}
    {# a comment #}
    select {{ dbt_utils.star(ref('x')) }} , a
    from {{ ref('orders') }} o
    join {{ source('shop', 'payments') }} p on p.order_id = o.id
    {% if is_incremental() %} where o.updated_at > (select max(updated_at) from {{ this }}) {% endif %}
    """
    out = strip_jinja(sql, model_name="my_model")
    assert "config(" not in out and "{#" not in out and "{%" not in out
    assert "from orders o" in out
    assert "shop.payments p" in out
    assert "from my_model" in out
    assert "__jinja__" in out
    assert find_refs(sql) == ["x", "orders"]


def test_dbt_yaml_facts_cover_tests_data_tests_combos_and_constraints():
    data = {
        "models": [
            {"name": "a", "columns": [{"name": "id", "tests": ["unique"]}]},
            {"name": "b", "columns": [{"name": "id", "data_tests": [{"unique": {"severity": "warn"}}]}]},
            {"name": "c", "tests": [{"dbt_utils.unique_combination_of_columns": {"combination_of_columns": ["x", "y"]}}]},
            {"name": "d", "constraints": [{"type": "primary_key", "columns": ["k1", "k2"]}]},
            {"name": "e", "columns": [{"name": "id", "constraints": [{"type": "primary_key"}]}]},
        ],
        "sources": [{"name": "s", "schema": "raw_s", "tables": [
            {"name": "t", "identifier": "t_v2", "columns": [{"name": "pk", "tests": ["unique"]}]}
        ]}],
    }
    store = facts_from_dbt_yaml(data)
    assert store.lookup("a").unique_keys == {frozenset({"id"})}
    assert store.lookup("b").unique_keys == {frozenset({"id"})}
    assert store.lookup("c").unique_keys == {frozenset({"x", "y"})}
    assert store.lookup("d").unique_keys == {frozenset({"k1", "k2"})}
    assert store.lookup("e").unique_keys == {frozenset({"id"})}
    assert store.lookup("s.t").unique_keys == {frozenset({"pk"})}
    assert store.lookup("raw_s.t_v2").unique_keys == {frozenset({"pk"})}
    assert store.lookup("warehouse.raw_s.t_v2").unique_keys == {frozenset({"pk"})}


def test_pragmas():
    pr = parse_pragmas("""
        -- grainguard: table raw.payments grain(payment_id)
        -- grainguard: grain(order_id, line_no)
        select 1
    """)
    assert pr.store.lookup("raw.payments").unique_keys == {frozenset({"payment_id"})}
    assert pr.self_keys == {frozenset({"order_id", "line_no"})}
    assert parse_pragmas("-- grainguard: ignore").ignore is True


def test_plain_sql_folder_with_pragmas(tmp_path):
    (tmp_path / "grainguard.yml").write_text("tables:\n  orders: [order_id]\n")
    (tmp_path / "a.sql").write_text(
        "-- grainguard: table payments grain(payment_id)\n"
        "select o.customer_id, sum(o.amount) from orders o join payments p on p.order_id = o.order_id group by 1"
    )
    (tmp_path / "b.sql").write_text("-- grainguard: ignore\nselect o.customer_id, sum(o.amount) from orders o join payments p on p.order_id = o.order_id group by 1")
    project = load_project(tmp_path)
    assert project.kind == "sql"
    results = analyze_project(project)
    assert [r.name for r in results] == ["a.sql"]
    assert [f.code for f in results[0].findings] == ["FANOUT_AGG"]
    assert project.ignored == ["b.sql"]


def test_cli_text_and_json_and_exit_codes(capsys):
    code = main(["check", str(EXAMPLE), "--no-color"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FANOUT_AGG" in out and "CHASM_TRAP" in out and "UNKNOWN_GRAIN" in out
    assert "4 error(s), 1 warning(s)" in out

    code = main(["check", str(EXAMPLE), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["summary"] == {"models": 10, "errors": 4, "warnings": 1}
    names = {m["name"] for m in payload["models"]}
    assert "customer_revenue_bad" in names

    assert main(["check", str(EXAMPLE), "--fail-on", "never"]) == 0
    capsys.readouterr()


def test_cli_single_query(capsys):
    code = main(["sql", "select o.c, sum(o.a) from orders o join payments p on p.oid = o.oid group by o.c", "--no-color"])
    out = capsys.readouterr().out
    assert code == 0
    assert "UNKNOWN_GRAIN" in out
    code = main(["sql", "select o.c, sum(o.a) from orders o join payments p on p.oid = o.oid group by o.c", "--strict", "--no-color"])
    capsys.readouterr()
    assert code == 1
