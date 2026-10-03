import pytest

from grainguard import Analyzer, FactStore


def facts(**tables):
    store = FactStore()
    for name, keys in tables.items():
        if keys and all(isinstance(k, str) for k in keys):
            store.add_key(name, keys)
        else:
            for k in keys:
                store.add_key(name, k)
    return store


def run(sql, store=None, dialect=None, strict=False, model_outputs=None):
    return Analyzer(facts=store or FactStore(), dialect=dialect, strict=strict,
                    model_outputs=model_outputs).analyze_sql(sql, name="q")


def codes(result):
    return sorted(f.code for f in result.findings)


def grain(result):
    return {tuple(sorted(k)) for k in result.output.unique_keys}


SHOP = facts(orders=["order_id"], payments=["payment_id"], customers=["customer_id"],
             order_lines=[["order_id", "line_no"]])


def test_sum_over_one_to_many_join_is_flagged():
    r = run("""
        select o.customer_id, sum(o.amount) as revenue
        from orders o left join payments p on p.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert codes(r) == ["FANOUT_AGG"]
    f = r.findings[0]
    assert f.severity == "error"
    assert "SUM(o.amount)" in f.aggregate
    assert "payments" in f.join


def test_measure_from_the_many_side_is_safe():
    r = run("""
        select o.customer_id, sum(p.amount) as paid
        from orders o left join payments p on p.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert codes(r) == []


def test_count_star_after_fan_out_is_flagged_but_count_distinct_is_not():
    r = run("""
        select o.customer_id, count(*) as n, count(distinct o.order_id) as orders
        from orders o join payments p on p.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert [f.aggregate for f in r.findings] == ["COUNT(*)"]


def test_min_and_max_are_immune():
    r = run("""
        select o.customer_id, max(o.amount), min(o.order_date)
        from orders o join payments p on p.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert codes(r) == []


def test_many_to_one_lookup_join_is_safe():
    r = run("""
        select c.region, sum(o.amount)
        from orders o join customers c on c.customer_id = o.customer_id
        group by c.region
    """, SHOP)
    assert codes(r) == []


def test_measure_mixing_fact_and_lookup_columns_is_safe():
    store = facts(orders=["order_id"], rates=[["currency", "rate_date"]])
    r = run("""
        select o.customer_id, sum(o.amount * r.rate) as usd
        from orders o left join rates r on r.currency = 'INR' and r.rate_date = o.order_date
        group by o.customer_id
    """, store)
    assert codes(r) == []


def test_constant_in_join_condition_counts_towards_the_key():
    store = facts(orders=["order_id"], rates=[["currency", "rate_date"]])
    bad = run("""
        select o.customer_id, sum(o.amount)
        from orders o left join rates r on r.rate_date = o.order_date
        group by o.customer_id
    """, store)
    good = run("""
        select o.customer_id, sum(o.amount)
        from orders o left join rates r on r.rate_date = o.order_date and r.currency = 'INR'
        group by o.customer_id
    """, store)
    assert codes(bad) == ["FANOUT_AGG"]
    assert codes(good) == []


def test_right_side_duplicated_when_left_is_not_unique_on_the_key():
    r = run("""
        select l.order_id, sum(p.amount) as paid
        from order_lines l join payments p on p.order_id = l.order_id
        group by l.order_id
    """, SHOP)
    assert codes(r) == ["FANOUT_AGG"]
    assert "payments" in r.findings[0].message


def test_chasm_trap_two_fan_outs_from_the_same_parent():
    store = facts(customers=["customer_id"], orders=["order_id"], tickets=["ticket_id"])
    r = run("""
        select c.region, sum(o.amount) as revenue, sum(t.minutes) as minutes
        from customers c
        left join orders o on o.customer_id = c.customer_id
        left join tickets t on t.customer_id = c.customer_id
        group by c.region
    """, store)
    assert len(r.findings) == 2
    assert all(f.code == "CHASM_TRAP" for f in r.findings)


def test_unknown_grain_is_a_warning_by_default_and_an_error_in_strict_mode():
    store = facts(customers=["customer_id"])
    sql = """
        select c.region, count(*) from customers c join web_events e on e.customer_id = c.customer_id
        group by c.region
    """
    r = run(sql, store)
    assert codes(r) == ["UNKNOWN_GRAIN"] and r.findings[0].severity == "warning"
    r2 = run(sql, store, strict=True)
    assert r2.findings[0].severity == "error"


def test_no_aggregate_means_no_finding_even_with_fan_out():
    r = run("select o.*, p.amount from orders o join payments p on p.order_id = o.order_id", SHOP)
    assert codes(r) == []
    assert r.joins_fanout == 1


def test_group_by_sets_the_grain():
    r = run("select customer_id, order_date as d, sum(amount) from orders group by customer_id, order_date", SHOP)
    assert grain(r) == {("customer_id", "d")}


def test_group_by_position_sets_the_grain():
    r = run("select customer_id, sum(amount) from orders group by 1", SHOP)
    assert grain(r) == {("customer_id",)}


def test_aggregate_without_group_by_is_a_single_row():
    r = run("select sum(amount) as total from orders", SHOP)
    assert r.output.single_row


def test_distinct_makes_all_columns_the_key():
    r = run("select distinct customer_id, region from customers", SHOP)
    assert grain(r) == {("customer_id", "region")}


def test_rename_carries_the_key_through_a_projection():
    r = run("select order_id as id, amount from orders", SHOP)
    assert grain(r) == {("id",)}


def test_lossless_cast_keeps_the_key():
    r = run("select cast(order_id as varchar) as id, amount from orders", SHOP)
    assert grain(r) == {("id",)}


def test_dropping_the_key_column_loses_the_key():
    r = run("select customer_id, amount from orders", SHOP)
    assert grain(r) == set()


def test_cte_grain_flows_into_the_main_query():
    r = run("""
        with paid as (select order_id, sum(amount) as paid from payments group by order_id)
        select o.customer_id, sum(o.amount) as revenue, sum(paid.paid) as paid
        from orders o left join paid on paid.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert codes(r) == []


def test_subquery_in_from_is_analysed():
    r = run("""
        select o.customer_id, sum(o.amount)
        from orders o
        join (select order_id, count(*) as n from payments group by order_id) p on p.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert codes(r) == []


def test_qualify_row_number_dedup_makes_the_partition_a_key():
    r = run("""
        with last_payment as (
            select order_id, amount
            from payments
            qualify row_number() over (partition by order_id order by payment_id desc) = 1
        )
        select o.customer_id, sum(o.amount)
        from orders o left join last_payment lp on lp.order_id = o.order_id
        group by o.customer_id
    """, SHOP, dialect="snowflake")
    assert codes(r) == []


def test_where_rn_equals_one_dedup_makes_the_partition_a_key():
    r = run("""
        with ranked as (
            select order_id, amount, row_number() over (partition by order_id order by payment_id desc) as rn
            from payments
        ),
        last_payment as (select order_id, amount from ranked where rn = 1)
        select o.customer_id, sum(o.amount)
        from orders o left join last_payment lp on lp.order_id = o.order_id
        group by o.customer_id
    """, SHOP)
    assert codes(r) == []


def test_union_all_loses_the_grain_but_union_keeps_all_columns():
    r1 = run("select order_id from orders union all select order_id from orders", SHOP)
    r2 = run("select order_id from orders union select order_id from orders", SHOP)
    assert grain(r1) == set()
    assert grain(r2) == {("order_id",)}


def test_cross_join_to_a_single_row_is_safe():
    r = run("""
        select o.customer_id, sum(o.amount) / max(t.total) as share
        from orders o cross join (select sum(amount) as total from orders) t
        group by o.customer_id
    """, SHOP)
    assert codes(r) == []


def test_cross_join_to_a_multi_row_relation_is_flagged():
    r = run("""
        select o.customer_id, sum(o.amount)
        from orders o cross join customers c
        group by o.customer_id
    """, SHOP)
    assert codes(r) == ["FANOUT_AGG"]


def test_semi_join_never_inflates():
    r = run("""
        select o.customer_id, sum(o.amount)
        from orders o left semi join payments p on p.order_id = o.order_id
        group by o.customer_id
    """, SHOP, dialect="spark")
    assert codes(r) == []


def test_using_clause_is_understood():
    r = run("""
        select o.customer_id, sum(o.amount)
        from orders o join payments p using (order_id)
        group by o.customer_id
    """, SHOP)
    assert codes(r) == ["FANOUT_AGG"]


def test_pragma_declares_table_grain():
    from grainguard.facts import parse_pragmas
    pr = parse_pragmas("-- grainguard: table web_events grain(event_id)")
    store = facts(customers=["customer_id"])
    store.merge(pr.store)
    r = run("""
        select c.region, sum(c.score)
        from customers c join web_events e on e.customer_id = c.customer_id
        group by c.region
    """, store)
    assert codes(r) == ["FANOUT_AGG"]


def test_window_aggregates_are_not_group_aggregates():
    r = run("""
        select o.order_id, sum(o.amount) over (partition by o.customer_id) as running
        from orders o join payments p on p.order_id = o.order_id
    """, SHOP)
    assert codes(r) == []


def test_parse_error_is_reported_not_raised():
    r = run("select from where group", SHOP)
    assert codes(r) == ["PARSE_ERROR"]


def test_create_table_as_select_registers_the_output_grain():
    a = Analyzer(facts=SHOP)
    a.analyze_sql("create table paid as select order_id, sum(amount) as paid from payments group by order_id")
    r = a.analyze_sql("""
        select o.customer_id, sum(o.amount)
        from orders o left join paid on paid.order_id = o.order_id
        group by o.customer_id
    """)
    assert codes(r) == []


def test_limit_one_is_a_single_row():
    r = run("select * from orders order by order_date desc limit 1", SHOP)
    assert r.output.single_row


def test_unqualified_measure_column_is_resolved_when_only_one_source_lacks_columns():
    store = FactStore()
    store.add_key("orders", ["order_id"])
    store.add_key("payments", ["payment_id"])
    store.add_columns("payments", ["payment_id", "order_id", "amount"])
    r = run("""
        select o.customer_id, sum(total)
        from orders o join payments p on p.order_id = o.order_id
        group by o.customer_id
    """, store)
    assert codes(r) == ["FANOUT_AGG"]


def test_comma_join_uses_where_predicates():
    store = facts(t=["id"], u=["id"])
    ok = run("select t.id, sum(u.v) from t, u where t.id = u.tid group by t.id", store)
    bad = run("select u.tid, sum(t.v) from t, u where t.id = u.tid group by u.tid", store)
    assert codes(ok) == []
    assert codes(bad) == ["FANOUT_AGG"]


def test_lateral_flatten_inflates_the_left_but_not_the_elements():
    store = facts(t=["id"])
    ok = run("select t.id, sum(f.value) from t, lateral flatten(input => t.arr) f group by t.id",
             store, dialect="snowflake")
    bad = run("select t.id, sum(t.amount) from t, lateral flatten(input => t.arr) f group by t.id",
              store, dialect="snowflake")
    assert codes(ok) == []
    assert codes(bad) == ["FANOUT_AGG"]
    assert "one row per element" in bad.findings[0].join


def test_bigquery_unnest_inflates_the_left():
    store = facts(t=["id"])
    bad = run("select t.id, sum(t.amount) from t cross join unnest(t.arr) as x group by t.id",
              store, dialect="bigquery")
    assert codes(bad) == ["FANOUT_AGG"]


def test_group_by_all_grain_is_the_non_aggregate_projections():
    r = run("select tid, x, sum(v) from u group by all", facts(u=["id"]), dialect="snowflake")
    assert grain(r) == {("tid", "x")}


def test_qualified_star_keeps_the_key_even_without_a_column_list():
    r = run("select t.*, u.v from t join u on u.id = t.id", facts(t=["id"], u=["id"]))
    assert grain(r) == {("id",)}


def test_unresolved_measure_is_reported_once_even_if_repeated_in_having():
    r = run("select tid, sum(v) from u join t on t.tid = u.tid group by tid having sum(v) > 10",
            facts(t=["id"]))
    assert codes(r) == ["UNKNOWN_GRAIN"]


def test_summing_a_lookup_attribute_per_fact_row_is_by_design():
    store = facts(order_items=["order_item_id"], products=["product_id"])
    r = run("""
        with items as (
            select i.order_id, i.order_item_id, p.product_price, p.is_food_item
            from order_items i left join products p on p.product_id = i.product_id)
        select order_id, sum(product_price) as subtotal, sum(case when is_food_item then 1 else 0 end) as food
        from items group by 1
    """, store)
    assert codes(r) == []
