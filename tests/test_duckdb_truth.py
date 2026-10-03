import pytest

duckdb = pytest.importorskip("duckdb")

from grainguard import Analyzer, FactStore

SETUP = """
create table orders(order_id int, customer_id int, order_date date, amount decimal(10,2));
create table payments(payment_id int, order_id int, payment_method varchar, amount decimal(10,2));
create table customers(customer_id int, region varchar);
create table tickets(ticket_id int, customer_id int, minutes int);

insert into customers values (1, 'east'), (2, 'east'), (3, 'west');
insert into orders values
  (10, 1, '2026-01-01', 100), (11, 1, '2026-01-02', 50), (12, 2, '2026-01-03', 80), (13, 3, '2026-01-04', 20);
insert into payments values
  (1, 10, 'card', 40), (2, 10, 'card', 40), (3, 10, 'cash', 20),
  (4, 11, 'card', 25), (5, 11, 'cash', 25),
  (6, 12, 'card', 80);
insert into tickets values (1, 1, 30), (2, 1, 15), (3, 2, 10);
"""

BAD = """
select o.customer_id, sum(o.amount) as revenue, count(*) as order_count, sum(p.amount) as paid
from orders o left join payments p on p.order_id = o.order_id
group by o.customer_id order by 1
"""

GOOD = """
with ppo as (select order_id, sum(amount) as paid from payments group by order_id)
select o.customer_id, sum(o.amount) as revenue, count(*) as order_count, sum(ppo.paid) as paid
from orders o left join ppo on ppo.order_id = o.order_id
group by o.customer_id order by 1
"""

CHASM = """
select c.region, sum(o.amount) as revenue, sum(t.minutes) as minutes
from customers c
left join orders o on o.customer_id = c.customer_id
left join tickets t on t.customer_id = c.customer_id
group by c.region order by 1
"""

TRUE_REVENUE = "select customer_id, sum(amount) from orders group by 1 order by 1"
TRUE_PAID = ("select o.customer_id, sum(p.amount) from payments p join orders o on o.order_id = p.order_id "
             "group by 1 order by 1")


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute(SETUP)
    return c


@pytest.fixture(scope="module")
def store():
    s = FactStore()
    s.add_key("orders", ["order_id"])
    s.add_key("payments", ["payment_id"])
    s.add_key("customers", ["customer_id"])
    s.add_key("tickets", ["ticket_id"])
    return s


def test_flagged_query_really_inflates_revenue_and_counts(con, store):
    truth = dict(con.execute(TRUE_REVENUE).fetchall())
    rows = con.execute(BAD).fetchall()
    revenue = {r[0]: r[1] for r in rows}
    counts = {r[0]: r[2] for r in rows}
    assert truth[1] == 150 and revenue[1] == 400
    assert counts[1] == 5
    assert revenue[3] == truth[3] == 20

    result = Analyzer(facts=store, dialect="duckdb").analyze_sql(BAD)
    flagged = {f.aggregate for f in result.findings if f.severity == "error"}
    assert flagged == {"SUM(o.amount)", "COUNT(*)"}
    paid_truth = dict(con.execute(TRUE_PAID).fetchall())
    paid = {r[0]: r[3] for r in rows}
    assert paid[1] == paid_truth[1] == 150


def test_passed_query_matches_the_truth(con, store):
    truth = dict(con.execute(TRUE_REVENUE).fetchall())
    rows = con.execute(GOOD).fetchall()
    assert {r[0]: r[1] for r in rows} == truth
    assert {r[0]: r[2] for r in rows} == {1: 2, 2: 1, 3: 1}
    result = Analyzer(facts=store, dialect="duckdb").analyze_sql(GOOD)
    assert result.findings == []


def test_chasm_trap_inflates_both_measures(con, store):
    rows = {r[0]: (r[1], r[2]) for r in con.execute(CHASM).fetchall()}
    true_rev = dict(con.execute(
        "select c.region, sum(o.amount) from orders o join customers c using (customer_id) group by 1").fetchall())
    true_min = dict(con.execute(
        "select c.region, sum(t.minutes) from tickets t join customers c using (customer_id) group by 1").fetchall())
    assert true_rev["east"] == 230 and rows["east"][0] == 380
    assert true_min["east"] == 55 and rows["east"][1] == 100
    result = Analyzer(facts=store, dialect="duckdb").analyze_sql(CHASM)
    assert sorted(f.code for f in result.findings) == ["CHASM_TRAP", "CHASM_TRAP"]
