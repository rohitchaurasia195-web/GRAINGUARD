# Prevalence survey

This folder holds the results of running grainguard over public dbt projects.

## Method

`scripts/survey_dbt_projects.py` shallow clones every repository listed in `scripts/repos.txt`,
loads the dbt project (reading `dbt_project.yml`, every `schema.yml`, and `grainguard.yml` when
present), analyses every model in dependency order and writes one CSV row per project.

Columns:

| column | meaning |
| --- | --- |
| models | number of `.sql` models under the model paths |
| parse_errors | models sqlglot could not parse after Jinja stripping |
| joins_total | joins seen in all queries, CTEs and subqueries |
| joins_proven_safe | joins where the joined relation is unique on the join columns |
| joins_fanout | joins where the joined relation has a known grain that the join columns do not cover |
| joins_unknown | joins to relations whose grain is not declared or inferable |
| aggregates_total | duplicate sensitive and immune aggregates together |
| aggregates_after_join | aggregates in a query that contains at least one join |
| fanout_errors | FANOUT_AGG findings |
| chasm_errors | CHASM_TRAP findings |
| unknown_warnings | UNKNOWN_GRAIN findings |
| models_with_errors | models with at least one error |

## Caveats

The survey measures what can be proven from the project text. A join counted as "unknown"
may well be safe in the warehouse. A join counted as "fan out" is one whose declared grain
does not cover the join columns; the SQL is correct only if an undeclared uniqueness holds in
the data. Spot checks of flagged models are recorded below. Please add yours.

## Runs

### 2026-10-03, grainguard 0.1.0, 14 projects

File: `results_2026-10-03.csv`.

Totals: 1,152 models, 37 parse errors, 765 joins (236 proven safe, 66 fan out, 463 unknown),
346 aggregates after a join, 89 flagged (39 FANOUT_AGG, 50 CHASM_TRAP) in 14 models of 4
projects, 138 UNKNOWN_GRAIN warnings.

Spot checks (every flagged model we read):

* `fivetran/dbt_shopify` `int_shopify_gql__customers_order_aggregates`: orders are joined to a
  transactions aggregate that is unique on `(order_id, source_relation, kind)` with
  `kind in ('sale', 'capture')`. If an order ever has both kinds, every per order measure
  summed afterwards (`order_total_tax`, `order_total_shipping`, ...) is counted twice. The
  model already uses `count(distinct orders.order_id)`, which suggests the authors were aware
  of possible duplication. Latent fan out; correct only under an undeclared assumption.
* `fivetran/dbt_salesforce` `int_salesforce__opportunity_aggregation_by_owner` (surfaces in
  `salesforce__manager_performance`): bookings, lost and pipeline CTEs are grouped by
  `(manager_id, owner_id)` and joined to users on `owner_id` alone, three times. Correct only
  if every owner has exactly one manager in the opportunity data. Latent chasm trap.
* `fivetran/dbt_zendesk` `int_zendesk__ticket_work_time_calendar` (surfaces in
  `zendesk__ticket_summary`): grouped by `(source_relation, ticket_id, last_status_assignment_date)`
  and joined on `(ticket_id, source_relation)`. Correct only if a ticket has one
  `last_status_assignment_date`, which the name suggests but nothing declares. Latent fan out.
* `Velir/dbt-ga4` `fct_ga4__pages`: `stg_ga4__page_engaged_time` is right joined to a CTE that
  is unique on five columns using only `page_engagement_key`. Engagement time is then summed.
  Correct only if `page_engagement_key` determines the other four columns. Latent fan out.

False positives found by the survey and fixed before this run:

* `dbt-labs/jaffle-shop` `orders`: `sum(product_price)` per order over order items joined to
  products. Products is unique on the join key, so repeating its price per item is the point
  of the join. grainguard no longer treats the right side of a many to one join as inflated.
* `fivetran/dbt_jira` `jira__issue_enhanced`: dedup written as
  `row_number() over (...) = 1 as latest_record` then `where latest_record`. Now recognised.
