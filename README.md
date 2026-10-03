# grainguard

**Stop SQL joins from silently inflating your aggregates.**

grainguard is a static checker for SQL and dbt projects. It works out the *grain* of every
table and query (what one row means) and refuses to pass any `SUM`, `COUNT` or `AVG` that is
computed on top of a join that can duplicate rows. It runs before the query does, needs no
database connection, and explains every finding in plain English.

```
$ grainguard check models/

ERROR FANOUT_AGG    models/marts/customer_revenue.sql
      SUM(o.amount) is computed after LEFT join to stg_payments (alias p) on p.order_id =
      o.order_id [one row per (payment_id)]. Rows of stg_orders (alias o) can be matched more
      than once, so this value is inflated.
      Fix: Aggregate stg_payments (alias p) to one row per join key in a CTE before joining, or
      compute SUM(o.amount) in a CTE at the grain of stg_orders (alias o) and join the result.

10 model(s), 7 join(s): 3 proven safe, 3 fan out, 1 unknown grain. 17 aggregate(s), 15 of them after a join.
4 error(s), 1 warning(s).
```

## The bug this catches

```sql
select o.customer_id, sum(o.amount) as revenue
from orders o
left join payments p on p.order_id = o.order_id
group by o.customer_id
```

This query runs without error on every database. It is also wrong. An order paid in three
instalments appears three times after the join, so its amount is summed three times. With the
tiny dataset in `tests/test_duckdb_truth.py`, customer 1 has orders worth 150 and the query
reports 400. Nobody notices until finance asks why the numbers do not match.

This is called a *fan out* (or a fan trap). Its cousin, the *chasm trap*, happens when one
table joins to two one to many tables at once and the two multiply each other. The 2023 paper
on aggregation consistency found this class of error in every major BI tool, and a 2026 paper
proved that it can be detected from schemas alone, at compile time, without running anything.
grainguard is the tool that does it.

## Install

```
pip install grainguard
```

Python 3.9 or newer. The only dependencies are `sqlglot` and `PyYAML`.

## Use

```
grainguard check path/to/dbt_project      # a dbt project (reads dbt_project.yml)
grainguard check path/to/sql_folder       # every .sql file under a folder
grainguard check query.sql                # one file
grainguard grain path/to/dbt_project      # also print the inferred grain of every model
grainguard sql "select ... "              # one query from the command line
```

Options: `--dialect snowflake|bigquery|spark|duckdb|postgres|...`, `--format json`,
`--strict` (treat unknown grain as an error), `--fail-on error|warning|never`, `--config`.

The exit code is 1 when there are errors, so it drops straight into CI:

```yaml
# .github/workflows/grainguard.yml
- run: pip install grainguard
- run: grainguard check . --dialect snowflake
```

Or as a pre-commit hook:

```yaml
- repo: local
  hooks:
    - id: grainguard
      name: grainguard
      entry: grainguard check .
      language: python
      additional_dependencies: [grainguard]
      pass_filenames: false
```

## How grainguard knows the grain

**Declared.** Anything you already tell dbt is used as is: a `unique` test (or `data_tests`)
on a column, `dbt_utils.unique_combination_of_columns`, `dbt_expectations.expect_compound_columns_to_be_unique`,
and `primary_key` or `unique` constraints, on models, seeds, snapshots and sources. For tables
outside dbt, list them in `grainguard.yml`:

```yaml
dialect: snowflake
tables:
  raw.orders: [order_id]
  raw.exchange_rates: [[currency, rate_date]]   # several columns, or several keys
  raw.settings: [[]]                            # a table that has at most one row
```

or declare them in the SQL itself:

```sql
-- grainguard: table raw.payments grain(payment_id)
-- grainguard: grain(order_id, line_no)   -- the grain of this file's own output
-- grainguard: ignore                     -- skip this file
```

**Inferred.** Everything else is worked out from the SQL:

| Construct | Resulting grain |
| --- | --- |
| `GROUP BY a, b` | one row per (a, b) |
| aggregate without `GROUP BY` | a single row |
| `SELECT DISTINCT` | all output columns |
| `QUALIFY ROW_NUMBER() OVER (PARTITION BY k ...) = 1` | one row per k |
| `ROW_NUMBER() ... AS rn` in a CTE, then `WHERE rn = 1` | one row per partition |
| `SELECT key AS new_name` | the key survives under its new name (lossless casts too) |
| `LIMIT 1` | a single row |
| `UNION` | all output columns; `UNION ALL` loses the grain |
| join to a relation that is unique on the join columns | the left grain survives |
| join where the left side is unique on the join columns | the right grain survives |
| join where neither side is unique | the union of both keys |
| `CROSS JOIN` to a single row relation | nothing changes |

Models are analysed in dependency order, so a CTE, a subquery or an upstream `ref()` with an
inferred grain is as good as a declared one. Constants in join conditions count towards the
key: `on r.currency = 'INR' and r.rate_date = o.order_date` proves uniqueness for a table that
is unique on `(currency, rate_date)`.

## What gets reported

| Code | Meaning |
| --- | --- |
| `FANOUT_AGG` (error) | a duplicate sensitive aggregate reads from a relation whose rows a join can repeat, and the grain of the joined relation is known, so the fan out is real |
| `CHASM_TRAP` (error) | the same, with two or more fan out joins in one query multiplying each other |
| `UNKNOWN_GRAIN` (warning, error with `--strict`) | an aggregate follows a join whose grain grainguard does not know, so it cannot prove the query is safe |
| `PARSE_ERROR` (warning) | sqlglot could not parse the file after Jinja was stripped |

`MIN`, `MAX`, `ANY_VALUE` and `COUNT(DISTINCT ...)` are immune to duplicates and are never
reported. A measure that mixes a column of a fanned out table with a column of a table that is
not fanned out (`sum(o.amount * r.rate)` where `r` is a lookup) is anchored to the lookup's rows
and is not reported either. Window functions are not group aggregates and are left alone.

## What we found in public dbt projects

The question behind the tool is: how common is this in real code? `scripts/survey_dbt_projects.py`
clones public dbt projects and tabulates what grainguard finds. The first run (3 October 2026,
grainguard 0.1.0, 14 projects, 1,152 models) is in `survey/`:

| | |
| --- | --- |
| Models | 1,152 (37 could not be parsed, 3.2%) |
| Joins | 765, of which 236 provably safe, 66 provably fan out, 463 of unknown grain |
| Aggregates after a join | 346 |
| Aggregates flagged as inflated | 89 (39 fan outs, 50 chasm traps) in 14 models of 4 projects |
| Aggregates after a join of unknown grain | 138 |

Three things stand out. First, 61% of joins are to relations whose grain is not declared
anywhere in the project, mostly because the staging models with the `unique` tests live in a
separate "source" package. The tool can only prove what it is told, which is itself an argument
for declaring grain. Second, every flagged aggregate we read by hand is a *latent* fan out: the
SQL is correct only under a uniqueness assumption that nothing in the project states or tests.
The two recurring shapes are a join on a key column restricted with `IN ('sale', 'capture')`
rather than pinned to one value, and a `GROUP BY` that includes a dependent attribute
(`owner_id, manager_id`) followed by a join on the identifier alone. Whether those assumptions
hold in a given warehouse is exactly the question a reviewer should be asked, and a one line
grain declaration on the model makes the warning go away. Third, the survey improved the tool:
two idioms it did not understand at first (summing a lookup attribute per fact row, and
`row_number() over (...) = 1 as latest_record` followed by `where latest_record`) produced
false positives on `dbt-labs/jaffle-shop` and `fivetran/dbt_jira`, and both are now handled
and covered by tests.

Treat these numbers as a first measurement, not a verdict on any project. Pull requests that
add projects to `scripts/repos.txt` or that classify findings as true or false positives are
very welcome.

## Limitations

grainguard reasons about schemas and SQL text, not data. It cannot know that a column is unique
unless something declares it or the SQL makes it so. Equality joins are understood; range and
inequality joins are treated as unproven. Set returning constructs (`LATERAL`, `UNNEST`,
`FLATTEN`) are treated as unknown grain. Jinja is stripped, not rendered, so models whose SQL
shape depends on macros may not parse (3.2% in the survey). `FULL OUTER` joins are linted like
inner joins. Summing an attribute of a lookup table across fact rows (`sum(products.price)`
per order item) is treated as intended, so a genuine "sum of a dimension attribute" mistake is
not reported. Nothing is executed and no credentials are needed.

## Roadmap

Column lineage across models so a declared key can be traced through renames in upstream
models; a `--fix` mode that rewrites a flagged query into the pre aggregated form; a dbt
`meta` convention for declaring grain; a SQLMesh loader; a web playground.

## Related work

grainguard builds on two papers. *Aggregation Consistency Errors in Semantic Layers and How to
Avoid Them* (2023) documents fan out errors across Tableau, Power BI, Looker, Malloy and Sigma
and proposes weighting as a fix inside semantic layers. *Grain Aware Data Transformations:
Type Level Formal Verification at Zero Computational Cost* (2026) formalises grain as a type
and proves that grain errors can be found at compile time, but ships no tool and measures no
real code. grainguard is the practical complement: a checker for plain SQL and dbt, plus the
first prevalence measurement on public projects.

## Citation

If grainguard is useful in your work, please cite it:

```
@software{grainguard,
  author = {Chaurasia, Rohit},
  title  = {grainguard: static detection of fan out aggregates in SQL and dbt},
  year   = {2026},
  url    = {https://github.com/rohitchaurasia195-web/GRAINGUARD}
}
```

## License

MIT.
