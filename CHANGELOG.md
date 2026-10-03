# Changelog

## 0.1.0 (2026-10-03)

First release.

* Grain inference for SELECT, CTEs, subqueries, GROUP BY (including GROUP BY ALL), DISTINCT,
  QUALIFY, WHERE rn = 1 and boolean row number flag dedup, UNION / INTERSECT / EXCEPT, LIMIT 1,
  string casts and renames, star projections.
* Join classification (safe, fan out, unknown) with constants and WHERE predicates counting
  towards the key, SEMI / ANTI / ASOF / LATERAL joins, comma joins, CROSS joins to single row
  relations, the anti join idiom (WHERE right.key IS NULL), and expression joins treated as
  unproven rather than proven.
* Inflation is tracked per output column through CTEs, subqueries, UNIONs and upstream models,
  so a fan out inside a CTE is caught when a later query aggregates over it; a GROUP BY that
  contains a key of the join result is recognised as one row per group; measures are anchored
  on their value columns, not on the columns of CASE conditions.
* Duplicate insensitive aggregates (MIN, MAX, COUNT DISTINCT, SUM DISTINCT, BOOL_OR, ARG_MAX,
  ...) are never reported; aggregates sqlglot does not type (LISTAGG, PRODUCT, BIT_XOR, ...)
  are recognised by name.
* Findings: FANOUT_AGG, CHASM_TRAP, UNKNOWN_GRAIN, PARSE_ERROR.
* Grain facts from dbt schema.yml (tests, data_tests, unique_combination_of_columns,
  constraints, sources), grainguard.yml and SQL pragmas.
* dbt loader with Jinja stripping and dependency ordered analysis; plain SQL folder loader.
* CLI: check, grain, sql; text and JSON output; exit codes for CI.
* Prevalence survey script and first results over 14 public dbt projects.
