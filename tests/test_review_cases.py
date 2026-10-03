import pytest

from grainguard import Analyzer, FactStore


@pytest.fixture
def store():
    st = FactStore()
    for t, k in [("orders", ["order_id"]), ("payments", ["payment_id"]), ("customers", ["customer_id"]),
                 ("tickets", ["ticket_id"]), ("codes", ["customer_code"])]:
        st.add_key(t, k)
    st.add_key("rates", ["currency", "rate_date"])
    return st


def verdict(store, sql, dialect=None):
    r = Analyzer(facts=store, dialect=dialect).analyze_sql(sql)
    return sorted((f.code, f.aggregate) for f in r.findings)


def test_fp_count_star_after_lookup_join_is_fine(store):
    assert verdict(store, """
        select c.region, count(*) as n_orders, count(1) as n2, sum(o.amount) as revenue
        from orders o join customers c on c.customer_id = o.customer_id group by 1""") == []


def test_fp_window_dedup_with_select_star(store):
    assert verdict(store, """
        with latest as (select *, row_number() over (partition by order_id order by paid_at desc) as rn from payments),
             l as (select * from latest where rn = 1)
        select o.customer_id, sum(o.amount) from orders o left join l on l.order_id = o.order_id group by 1""") == []
    assert verdict(store, """
        with l as (select * from payments qualify row_number() over (partition by order_id order by paid_at desc) = 1)
        select o.customer_id, sum(o.amount) from orders o left join l on l.order_id = o.order_id group by 1""",
        dialect="snowflake") == []


def test_fp_rn_filter_in_the_on_clause(store):
    assert verdict(store, """
        with latest as (select payment_id, order_id, amount,
                               row_number() over (partition by order_id order by paid_at desc) as rn
                        from payments)
        select o.customer_id, sum(o.amount) from orders o
        left join latest l on l.order_id = o.order_id and l.rn = 1 group by 1""") == []


def test_fp_join_on_expression_is_unproven_not_an_error(store):
    assert verdict(store, """
        select o.status, sum(o.amount) from orders o
        left join customers c on c.customer_id = coalesce(o.customer_id, -1) group by 1
    """) == [("UNKNOWN_GRAIN", "SUM(o.amount)")]


def test_fp_asof_join_matches_at_most_one_row(store):
    assert verdict(store, """
        select o.customer_id, sum(o.amount), count(*) from orders o
        asof join payments p on p.order_id = o.order_id and p.paid_at >= o.order_date group by 1""",
        dialect="duckdb") == []


def test_fp_anti_join_idiom(store):
    assert verdict(store, """
        select count(*) as unpaid_orders, sum(o.amount) from orders o
        left join payments p on p.order_id = o.order_id where p.payment_id is null""") == []


def test_fp_where_clause_pins_the_key(store):
    assert verdict(store, """
        select o.customer_id, sum(o.amount) from orders o join payments p on p.order_id = o.order_id
        where p.payment_id = o.first_payment_id group by 1""") == []


def test_fp_using_in_a_join_chain(store):
    assert verdict(store, """
        select c.region, sum(p.amount) as paid from orders o
        join customers c on c.customer_id = o.customer_id
        join payments p using (order_id) group by 1""") == []


def test_fp_distinct_aggregates_are_immune(store):
    assert verdict(store, """
        select o.customer_id, sum(distinct o.amount), avg(distinct o.amount),
               array_agg(distinct o.order_id), string_agg(distinct o.status, ',')
        from orders o join payments p on p.order_id = o.order_id group by 1""", dialect="duckdb") == []


def test_fp_more_idempotent_aggregates(store):
    assert verdict(store, """
        select o.customer_id, bool_or(o.flag), bool_and(o.flag), arg_max(o.order_id, o.amount),
               arg_min(o.order_id, o.amount), max_by(o.order_id, o.amount)
        from orders o join payments p on p.order_id = o.order_id group by 1""", dialect="duckdb") == []


def test_fp_group_by_the_finest_key_makes_every_group_one_row(store):
    assert verdict(store, """
        select p.payment_id, sum(o.amount), count(*), avg(o.amount)
        from orders o join payments p on p.order_id = o.order_id group by 1""") == []


def test_fn_fan_out_inside_a_cte_aggregated_later(store):
    assert verdict(store, """
        with joined as (
            select o.customer_id, o.order_id, o.amount, p.payment_id, p.amount as paid
            from orders o left join payments p on p.order_id = o.order_id)
        select customer_id, sum(amount), count(*), count(order_id), sum(paid) from joined group by 1
    """) == [("FANOUT_AGG", "COUNT(*)"), ("FANOUT_AGG", "COUNT(order_id)"), ("FANOUT_AGG", "SUM(amount)")]


def test_fn_fan_out_inside_a_from_subquery(store):
    assert verdict(store, """
        select j.customer_id, sum(j.amount)
        from (select o.customer_id, o.amount from orders o left join payments p on p.order_id = o.order_id) j
        group by 1""") == [("FANOUT_AGG", "SUM(j.amount)")]


def test_fn_fan_out_two_hops_upstream_with_star(store):
    assert verdict(store, """
        with j1 as (select o.*, p.payment_id from orders o left join payments p on p.order_id = o.order_id),
             j2 as (select j1.*, c.region from j1 join customers c on c.customer_id = j1.customer_id)
        select region, sum(amount), sum(payment_id), count(*) from j2 group by 1
    """) == [("FANOUT_AGG", "COUNT(*)"), ("UNKNOWN_GRAIN", "SUM(amount)")]


def test_fn_create_table_as_then_aggregate(store):
    a = Analyzer(facts=store)
    a.analyze_sql("create table joined as select o.customer_id, o.amount, p.payment_id from orders o left join payments p on p.order_id = o.order_id")
    r = a.analyze_sql("select customer_id, sum(amount) from joined group by 1")
    assert [(f.code, f.aggregate) for f in r.findings] == [("FANOUT_AGG", "SUM(amount)")]


def test_fn_aggregate_inside_a_window_order_by_is_a_group_aggregate(store):
    assert verdict(store, """
        select o.customer_id, rank() over (order by sum(o.amount) desc) as rnk
        from orders o join payments p on p.order_id = o.order_id group by 1""") == [("FANOUT_AGG", "SUM(o.amount)")]


def test_fn_sum_of_a_literal_counts_rows(store):
    assert verdict(store, """
        select o.customer_id, sum(1) as n from orders o join payments p on p.order_id = o.order_id group by 1
    """) == [("FANOUT_AGG", "SUM(1)")]


def test_fn_anonymous_aggregate_functions(store):
    assert verdict(store, """
        select o.customer_id, listagg(o.status, ','), product(o.amount), bit_xor(o.order_id)
        from orders o join payments p on p.order_id = o.order_id group by 1
    """) == [("FANOUT_AGG", "BIT_XOR(o.order_id)"), ("FANOUT_AGG", "LISTAGG(o.status, ',')"),
             ("FANOUT_AGG", "PRODUCT(o.amount)")]


def test_fn_group_by_name_that_shadows_a_source_column_is_not_a_key(store):
    assert verdict(store, """
        with monthly as (select date_trunc('month', order_date) as order_date, sum(amount) as amt
                         from orders group by order_date)
        select om.customer_id, sum(om.amount) from orders om join monthly m on m.order_date = om.order_date
        group by 1""") == [("UNKNOWN_GRAIN", "SUM(om.amount)")]


def test_fn_lossy_cast_on_the_key_is_not_trusted(store):
    assert verdict(store, """
        select o.status, sum(o.amount) from orders o
        join codes c on cast(c.customer_code as int) = o.customer_id group by 1
    """) == [("UNKNOWN_GRAIN", "SUM(o.amount)")]


def test_condition_columns_do_not_anchor_a_measure(store):
    assert verdict(store, """
        select o.customer_id, sum(case when p.payment_id is not null then o.amount else 0 end)
        from orders o left join payments p on p.order_id = o.order_id group by 1
    """) == [("FANOUT_AGG", "SUM(CASE WHEN NOT p.payment_id IS NULL THEN o.amount ELSE 0 END)")]
    assert verdict(store, """
        select o.customer_id, sum(case when p.payment_id is not null then 1 else 0 end)
        from orders o left join payments p on p.order_id = o.order_id group by 1""") == []


def test_intersect_keeps_the_left_grain(store):
    r = Analyzer(facts=store).analyze_sql("select order_id from orders intersect select order_id from orders")
    assert r.output is not None and frozenset({"order_id"}) in r.output.unique_keys


def test_boolean_row_number_flag_is_a_dedup(store):
    st = FactStore()
    st.add_key("issues", ["issue_id"])
    st.add_key("history", ["issue_day_id"])
    assert verdict(st, """
        with daily as (select *, row_number() over (partition by issue_id order by date_day desc) = 1 as latest_record from history),
             latest as (select * from daily where latest_record),
             final as (select i.*, l.status from issues i left join latest l on l.issue_id = i.issue_id)
        select project_id, sum(open_seconds) from final group by 1""") == []


def test_declared_unique_key_overrides_inherited_inflation():
    from grainguard.dbt import apply_declared
    from grainguard.model import Relation
    st = FactStore()
    st.add_key("orders", ["order_id"])
    st.add_key("payments", ["payment_id"])
    a = Analyzer(facts=st)
    r = a.analyze_sql("select o.order_id, o.amount, p.payment_id from orders o left join payments p on p.order_id = o.order_id")
    out = r.output
    assert "amount" in out.column_inflation
    declared = Relation(name="joined", unique_keys={frozenset({"order_id"})}, declared=True)
    apply_declared(out, declared)
    assert out.column_inflation == {} and out.fanout_causes == []
