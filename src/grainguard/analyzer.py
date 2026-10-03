from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp

from .facts import FactStore
from .model import ColumnOrigin, Finding, Key, ModelResult, Relation, key_of, norm


def _exp_types(*names: str) -> tuple:
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


_IDEMPOTENT_AGGS = _exp_types(
    "Min", "Max", "AnyValue", "ApproxDistinct", "LogicalOr", "LogicalAnd", "ArgMax", "ArgMin",
    "First", "FirstValue", "LastValue", "Last", "BitwiseOrAgg", "BitwiseAndAgg", "ApproxTopK",
)
_ANON_AGG_NAMES = {
    "listagg", "string_agg", "group_concat", "product", "bit_xor", "bitxor_agg", "bit_xor_agg",
    "json_agg", "jsonb_agg", "json_arrayagg", "json_objectagg", "array_agg", "collect_list",
    "approx_percentile", "percentile_cont", "percentile_disc", "median", "histogram", "sum",
    "avg", "mean", "count", "variance", "var_pop", "var_samp", "stddev", "stddev_pop",
    "stddev_samp", "corr", "covar_pop", "covar_samp", "regr_slope", "regr_intercept",
    "kurtosis", "skewness", "approx_quantile", "quantile", "quantile_cont", "quantile_disc",
    "mode", "entropy", "sum_distinct", "count_if", "countif", "sumif", "avgif", "minhash",
    "percentile_approx", "approx_count_distinct", "hll", "hll_estimate", "bool_or", "bool_and",
    "every", "any_value", "max_by", "min_by", "arg_max", "arg_min", "argmax", "argmin",
    "first", "last", "first_value", "last_value", "bit_or", "bit_and", "bitor_agg", "bitand_agg",
    "collect_set", "array_unique_agg", "array_agg_distinct", "approx_set", "object_agg",
}
_ANON_IMMUNE_NAMES = {
    "approx_count_distinct", "hll", "hll_estimate", "bool_or", "bool_and", "every", "any_value",
    "max_by", "min_by", "arg_max", "arg_min", "argmax", "argmin", "first", "last", "first_value",
    "last_value", "bit_or", "bit_and", "bitor_agg", "bitand_agg", "collect_set",
    "array_unique_agg", "array_agg_distinct", "approx_set", "minhash",
}

_LOSSLESS_CAST_TYPES = {"VARCHAR", "TEXT", "STRING", "NVARCHAR", "NCHAR", "CHAR"}

_PREDICATES = _exp_types(
    "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Is", "In", "Like", "ILike", "SimilarTo", "Between",
    "And", "Or", "Not", "NullSafeEQ", "NullSafeNEQ", "Exists", "RegexpLike", "RegexpILike",
    "Glob",
)


@dataclass
class JoinInfo:
    description: str
    proven: bool
    chasm: bool = False


@dataclass
class _Source:
    alias: str
    relation: Relation
    inflated_by: List[JoinInfo] = field(default_factory=list)

    def column_causes(self, col: str) -> Tuple[List[JoinInfo], bool]:
        causes = list(self.inflated_by)
        rel = self.relation
        col = norm(col)
        if col in rel.column_inflation:
            causes += list(rel.column_inflation[col])
            return causes, True
        if col in rel.explicit_columns:
            return causes, True
        if rel.columns is None and rel.star_inflation:
            causes += list(rel.star_inflation)
            return causes, rel.all_inflated
        return causes, True


class Analyzer:
    def __init__(
        self,
        facts: Optional[FactStore] = None,
        dialect: Optional[str] = None,
        strict: bool = False,
        model_outputs: Optional[Dict[str, Relation]] = None,
    ) -> None:
        self.facts = facts or FactStore()
        self.dialect = dialect
        self.strict = strict
        self.model_outputs: Dict[str, Relation] = model_outputs or {}


    def analyze_sql(self, sql: str, name: str = "query", file: Optional[str] = None,
                    self_keys: Optional[Set[Key]] = None) -> ModelResult:
        result = ModelResult(name=name, file=file, output=None)
        try:
            statements = sqlglot.parse(sql, read=self.dialect)
        except Exception as e:
            result.findings.append(Finding(
                code="PARSE_ERROR", severity="warning", file=file, model=name,
                message=f"Could not parse SQL ({type(e).__name__}): {str(e).splitlines()[0][:200]}",
            ))
            return result

        last_output: Optional[Relation] = None
        for stmt in statements:
            if stmt is None:
                continue
            ctx = _Context(result=result, model=name, file=file, cte_scope={})
            target_name = None
            body = stmt
            if isinstance(stmt, exp.Create):
                body = stmt.expression
                tbl = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
                if isinstance(tbl, exp.Table):
                    target_name = _table_full_name(tbl)
            elif isinstance(stmt, exp.Insert):
                body = stmt.expression
                if isinstance(stmt.this, exp.Table):
                    target_name = _table_full_name(stmt.this)
            if body is None or not _is_query(body):
                continue
            rel = self._analyze_query(body, ctx, part="main query")
            if rel is not None:
                last_output = rel
                if target_name:
                    rel.name = target_name
                    self.model_outputs[norm(target_name)] = rel

        if last_output is not None:
            last_output.name = name
            if self_keys:
                last_output.unique_keys |= set(self_keys)
                last_output.declared = True
        result.output = last_output
        return result


    def _analyze_query(self, node: exp.Expression, ctx: "_Context", part: str) -> Optional[Relation]:
        if isinstance(node, exp.Subquery):
            return self._analyze_query(node.this, ctx, part)
        with_ = _arg(node, "with_", "with")
        scope = dict(ctx.cte_scope)
        if with_ is not None:
            for cte in with_.expressions:
                cte_name = norm(cte.alias)
                inner_ctx = ctx.child(scope)
                rel = self._analyze_query(cte.this, inner_ctx, part=f"CTE {cte.alias}")
                if rel is None:
                    rel = Relation(name=cte_name)
                rel.name = cte_name
                scope[cte_name] = rel
        ctx2 = ctx.child(scope)
        if isinstance(node, exp.Select):
            return self._analyze_select(node, ctx2, part)
        if _is_set_operation(node):
            return self._analyze_set_operation(node, ctx2, part)
        return None

    def _analyze_set_operation(self, node: exp.Expression, ctx: "_Context", part: str) -> Relation:
        left = self._analyze_query(node.left, ctx, part=f"{part} (left of set operation)")
        right = self._analyze_query(node.right, ctx, part=f"{part} (right of set operation)")
        rel = Relation(name="set_operation")
        rel.columns = left.columns if left is not None else None
        distinct = node.args.get("distinct", True)
        if isinstance(node, (exp.Intersect, exp.Except)) and left is not None:
            rel.unique_keys = set(left.unique_keys)
        elif distinct and rel.columns:
            rel.unique_keys = {key_of(rel.columns)}
        for branch in (left, right):
            if branch is None:
                continue
            for col, causes in branch.column_inflation.items():
                rel.column_inflation.setdefault(col, []).extend(causes)
            rel.fanout_causes.extend(branch.fanout_causes)
            rel.star_inflation.extend(branch.star_inflation)
        rel.all_inflated = all(b.all_inflated for b in (left, right) if b is not None) and bool(rel.star_inflation)
        return rel


    def _analyze_select(self, select: exp.Select, ctx: "_Context", part: str) -> Relation:
        sources: List[_Source] = []
        acc_keys: Set[frozenset] = set()
        acc_known = False
        fanout_joins: List[JoinInfo] = []
        all_infos: List[JoinInfo] = []

        from_ = _arg(select, "from_", "from")
        if from_ is not None:
            alias, rel = self._resolve_source(from_.this, ctx, part)
            sources.append(_Source(alias=alias, relation=rel))
            acc_keys = _qualify_keys(alias, rel.unique_keys)
            acc_known = rel.grain_known
        where = select.args.get("where")
        where_terms = list(_and_terms(where.this)) if where is not None else []

        for join in select.args.get("joins") or []:
            kind = (join.args.get("kind") or "").upper()
            method = (join.args.get("method") or "").upper()
            alias, rel = self._resolve_source(join.this, ctx, part)
            ctx.result.joins_total += 1
            if kind in ("SEMI", "ANTI"):
                ctx.result.joins_proven_safe += 1
                continue

            is_lateral = isinstance(join.this, (exp.Lateral, exp.Unnest))
            no_condition = join.args.get("on") is None and join.args.get("using") is None
            cond = _join_condition(join, sources, alias, rel, where_terms)
            right_cols = cond.right_cols
            left_cols = cond.left_cols
            is_cross = (not cond.pairs and no_condition and not cond.right_consts
                        and not cond.pinned_right_null)

            right_unique = rel.single_row or (bool(right_cols) and rel.is_unique_on(right_cols))
            left_unique = (frozenset() in acc_keys) or (
                bool(left_cols) and any(k <= left_cols for k in acc_keys)
            )
            if cond.pinned_right_null:
                right_unique = True
            if is_cross:
                right_unique = rel.single_row
                left_unique = frozenset() in acc_keys
            if method == "ASOF":
                right_unique = True
            opaque = cond.opaque or method == "NATURAL"
            if is_lateral:
                left_unique = True
                right_unique = False
                rel.unique_keys = set()

            join_desc = _describe_join(rel, alias, join)
            if is_lateral:
                join_desc += " (one row per element of each left row)"

            if not right_unique:
                proven = (rel.grain_known and not opaque) or is_lateral
                info = JoinInfo(description=join_desc, proven=proven)
                for s in sources:
                    s.inflated_by.append(info)
                fanout_joins.append(info)
                all_infos.append(info)
                if proven:
                    ctx.result.joins_fanout += 1
                else:
                    ctx.result.joins_unknown += 1
            else:
                ctx.result.joins_proven_safe += 1

            new_source = _Source(alias=alias, relation=rel)
            if not left_unique and not right_unique:
                left_desc = _describe_left(sources, left_cols, acc_keys, acc_known)
                info = JoinInfo(description=f"{join_desc}; {left_desc}",
                                proven=acc_known and not opaque)
                new_source.inflated_by.append(info)
                all_infos.append(info)
            sources.append(new_source)

            right_keys = _qualify_keys(alias, rel.unique_keys)
            if right_unique and left_unique:
                acc_keys = acc_keys | right_keys
            elif right_unique:
                pass
            elif left_unique:
                acc_keys = right_keys
            else:
                acc_keys = {kl | kr for kl in acc_keys for kr in right_keys}
            acc_known = bool(acc_keys)

        if len(fanout_joins) >= 2:
            for j in all_infos:
                j.chasm = True

        output = self._build_output(select, sources, acc_keys, fanout_joins)
        self._check_aggregates(select, sources, acc_keys, fanout_joins, ctx, part)
        self._analyze_nested_selects(select, ctx, part)
        return output


    def _resolve_source(self, node: exp.Expression, ctx: "_Context", part: str) -> Tuple[str, Relation]:
        if isinstance(node, exp.Table):
            full = _table_full_name(node)
            alias = norm(node.alias_or_name)
            short = norm(node.name)
            if short in ctx.cte_scope and not node.db:
                rel = _copy_relation(ctx.cte_scope[short])
            elif norm(full) in self.model_outputs or short in self.model_outputs:
                base = self.model_outputs.get(norm(full)) or self.model_outputs[short]
                rel = _copy_relation(base)
                declared = self.facts.lookup(full)
                if declared is not None:
                    rel.unique_keys |= declared.unique_keys
                    rel.declared = rel.declared or declared.declared
                    if rel.columns is None and declared.columns:
                        rel.columns = list(declared.columns)
            else:
                found = self.facts.lookup(full)
                rel = _copy_relation(found) if found is not None else Relation(name=full)
                rel.name = full
            return alias, rel
        if isinstance(node, exp.Subquery):
            alias = norm(node.alias_or_name) or "subquery"
            rel = self._analyze_query(node.this, ctx, part=f"{part} > subquery {alias}")
            if rel is None:
                rel = Relation(name=alias)
            rel.name = f"subquery {alias}"
            return alias, rel
        if isinstance(node, exp.Values):
            alias = norm(node.alias_or_name) or "values"
            rel = Relation(name="VALUES")
            if len(node.expressions) == 1:
                rel.unique_keys = {frozenset()}
            if isinstance(node.args.get("alias"), exp.TableAlias):
                cols = [norm(c.name) for c in node.args["alias"].columns]
                rel.columns = cols or None
            return alias, rel
        if isinstance(node, (exp.Lateral, exp.Unnest)):
            alias = norm(node.alias_or_name) or "lateral"
            return alias, Relation(name=f"lateral {alias}")
        alias = norm(getattr(node, "alias_or_name", "") or "source")
        return alias, Relation(name=alias)


    def _build_output(self, select: exp.Select, sources: List[_Source], acc_keys: Set[frozenset],
                      fanout_joins: List[JoinInfo]) -> Relation:
        rel = Relation(name="select")
        col_index = _column_index(sources)
        by_alias = {s.alias: s for s in sources}

        columns: List[str] = []
        origins: Dict[str, ColumnOrigin] = {}
        inflation: Dict[str, List[JoinInfo]] = {}
        unknown_columns = False
        star_aliases: Set[str] = set()
        plain_outputs: Set[str] = set()

        star_inflation: List[JoinInfo] = []
        star_all_inflated = True
        uncertain_outputs: Set[str] = set()
        explicit_through_star: Set[str] = set()

        def add_origin(out_name: str, src_alias: str, src_col: str) -> None:
            origins[out_name] = ColumnOrigin(src_alias, src_col)
            plain_outputs.add(out_name)
            src = by_alias.get(src_alias)
            if src is not None:
                causes, certain = src.column_causes(src_col)
                if causes:
                    inflation[out_name] = causes
                    if not certain:
                        uncertain_outputs.add(out_name)

        def expand(table_alias: Optional[str]) -> None:
            nonlocal unknown_columns, star_all_inflated
            for s in sources:
                if table_alias is not None and s.alias != table_alias:
                    continue
                if s.relation.columns is None:
                    unknown_columns = True
                    causes = list(s.inflated_by) + list(s.relation.star_inflation)
                    if causes:
                        star_inflation.extend(causes)
                    if not (s.inflated_by or (s.relation.star_inflation and s.relation.all_inflated)):
                        star_all_inflated = False
                    for c in s.relation.explicit_columns:
                        add_origin(c, s.alias, c)
                        explicit_through_star.add(c)
                else:
                    for c in s.relation.columns:
                        c = norm(c)
                        columns.append(c)
                        add_origin(c, s.alias, c)

        for proj in select.expressions:
            if isinstance(proj, exp.Star):
                star_aliases |= {s.alias for s in sources}
                expand(None)
                continue
            if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                table_alias = norm(proj.table)
                star_aliases.add(table_alias)
                expand(table_alias)
                continue
            out_name = norm(proj.alias_or_name) if proj.alias_or_name else None
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            inner_u = _unwrap_lossless_cast(inner)
            if isinstance(inner_u, exp.Column) and not isinstance(inner_u.this, exp.Star):
                if out_name is None:
                    out_name = norm(inner_u.name)
                src_alias = _resolve_column_alias(inner_u, sources, col_index)
                if src_alias is not None:
                    add_origin(out_name, src_alias, norm(inner_u.name))
                else:
                    causes = _unresolved_inflation(sources)
                    if causes:
                        inflation[out_name] = causes
            else:
                if out_name is None:
                    out_name = f"_col{len(columns)}"
                causes = _expression_inflation(inner, sources, col_index)
                if causes:
                    inflation[out_name] = causes
            columns.append(out_name)
        rel.columns = None if unknown_columns else columns
        rel.column_sources = origins

        has_agg = _has_group_aggregate(select)
        group = select.args.get("group")
        qualify = select.args.get("qualify")
        limit = select.args.get("limit")
        collapses = group is not None or has_agg or bool(select.args.get("distinct"))

        if group is not None or has_agg:
            inflation = {}
            if group is None:
                rel.unique_keys = {frozenset()}
            elif group.args.get("all") and not group.expressions:
                names = []
                for proj in select.expressions:
                    inner = proj.this if isinstance(proj, exp.Alias) else proj
                    if _contains_aggregate(inner) or isinstance(inner, exp.Star) or (
                            isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
                        continue
                    names.append(norm(proj.alias_or_name))
                if names:
                    rel.unique_keys = {frozenset(names)}
            else:
                key = _group_key(group, select, columns, origins, plain_outputs, sources)
                if key is not None:
                    rel.unique_keys = {key}
        elif select.args.get("distinct") and rel.columns is not None:
            rel.unique_keys = {key_of(rel.columns)}
            inflation = {}
        else:
            reverse: Dict[Tuple[str, str], str] = {}
            for out_name, o in origins.items():
                reverse.setdefault((o.source_alias, o.source_column), out_name)
            for k in acc_keys:
                mapped = []
                ok = True
                for q in k:
                    a, c = q.split(".", 1)
                    if (a, c) in reverse:
                        mapped.append(reverse[(a, c)])
                    elif a in star_aliases:
                        mapped.append(c)
                    else:
                        ok = False
                        break
                if ok:
                    rel.unique_keys.add(frozenset(mapped))
            if qualify is not None:
                part_cols = _rownumber_partition(qualify.this, select, sources, col_index)
                if part_cols is not None:
                    mapped = _map_qualified_to_output(part_cols, origins, star_aliases)
                    if mapped is not None:
                        rel.unique_keys.add(mapped)
            where = select.args.get("where")
            if where is not None and len(sources) == 1:
                s = sources[0]
                for colname, part_cols in s.relation.window_dedup.items():
                    is_flag = colname in s.relation.window_dedup_flags
                    if _where_filters_to_one(where.this, colname, s.alias, is_flag):
                        mapped = _map_qualified_to_output(
                            {f"{s.alias}.{c}" for c in part_cols}, origins, star_aliases)
                        if mapped is not None:
                            rel.unique_keys.add(mapped)

        if limit is not None and isinstance(limit.expression, exp.Literal):
            try:
                if int(limit.expression.this) == 1:
                    rel.unique_keys.add(frozenset())
            except ValueError:
                pass

        dedup: Dict[str, List[str]] = {}
        flags: Set[str] = set()
        for proj in select.expressions:
            if not isinstance(proj, exp.Alias):
                continue
            win, is_flag = _rownumber_window(proj.this)
            if win is None:
                continue
            parts: Optional[List[str]] = []
            for p in win.args.get("partition_by") or []:
                p = _unwrap_lossless_cast(p)
                if not isinstance(p, exp.Column):
                    parts = None
                    break
                src_alias = _resolve_column_alias(p, sources, col_index)
                name = _output_name_for(src_alias, norm(p.name), origins) if src_alias else None
                if name is None and src_alias in star_aliases:
                    name = norm(p.name)
                if name is None:
                    parts = None
                    break
                parts.append(name)
            if parts is None:
                continue
            out = norm(proj.alias)
            dedup[out] = parts
            if is_flag:
                flags.add(out)
            elif not collapses:
                rel.unique_keys.add(frozenset(parts + [out]))
        rel.window_dedup = dedup
        rel.window_dedup_flags = flags
        rel.column_inflation = inflation
        rel.explicit_columns = set(columns) | explicit_through_star
        if not collapses:
            rel.star_inflation = star_inflation
            explicit_all_inflated = all(c in inflation for c in columns) if columns else True
            rel.all_inflated = bool(star_inflation) and star_all_inflated and explicit_all_inflated \
                and not uncertain_outputs
            rel.fanout_causes = list(fanout_joins)
            for s in sources:
                rel.fanout_causes.extend(s.relation.fanout_causes)
        return rel


    def _check_aggregates(self, select: exp.Select, sources: List[_Source], acc_keys: Set[frozenset],
                          fanout_joins: List[JoinInfo], ctx: "_Context", part: str) -> None:
        aggs = list(_group_aggregates(select))
        if not aggs:
            return
        ctx.result.aggregates_total += len(aggs)
        if len(sources) > 1:
            ctx.result.aggregates_after_join += len(aggs)

        row_causes: List[JoinInfo] = list(fanout_joins)
        for s in sources:
            row_causes.extend(s.relation.fanout_causes)
        any_column_inflation = any(s.inflated_by or s.relation.column_inflation for s in sources)
        if not row_causes and not any_column_inflation:
            return

        col_index = _column_index(sources)
        if _groups_cover_a_key(select, sources, acc_keys, col_index):
            return

        alias_to_source = {s.alias: s for s in sources}
        reported: Set[str] = set()

        for agg in aggs:
            agg_sql = agg.sql(dialect=self.dialect)
            if agg_sql in reported or _is_immune(agg):
                continue
            value_cols, cond_cols = _aggregate_columns(agg)
            anchor_cols = value_cols or cond_cols
            if not anchor_cols:
                if row_causes:
                    reported.add(agg_sql)
                    self._report(ctx, part, agg_sql, row_causes, sources=None, count_rows=True)
                continue

            traced: List[Tuple[_Source, List[JoinInfo]]] = []
            unresolved: List[str] = []
            uncertain_causes: List[JoinInfo] = []
            for c in anchor_cols:
                a = _resolve_column_alias(c, sources, col_index)
                if a is None or a not in alias_to_source:
                    unresolved.append(c.sql(dialect=self.dialect))
                    continue
                src = alias_to_source[a]
                causes, certain = src.column_causes(c.name)
                if causes and not certain:
                    unresolved.append(c.sql(dialect=self.dialect))
                    uncertain_causes.extend(causes)
                    continue
                traced.append((src, causes))

            if not traced:
                inflated_causes = uncertain_causes or row_causes or [j for s in sources for j in s.inflated_by]
                if unresolved and inflated_causes:
                    reported.add(agg_sql)
                    self._report_unresolved(ctx, part, agg_sql, unresolved, inflated_causes, sources)
                continue
            if any(not causes for _, causes in traced):
                continue
            if unresolved:
                reported.add(agg_sql)
                self._report_unresolved(ctx, part, agg_sql, unresolved, traced[0][1], sources)
                continue
            reported.add(agg_sql)
            all_causes = [cause for _, causes in traced for cause in causes]
            self._report(ctx, part, agg_sql, all_causes, sources=[s for s, _ in traced])

    def _report(self, ctx: "_Context", part: str, agg_sql: str, causes: List[JoinInfo],
                sources: Optional[List[_Source]], count_rows: bool = False) -> None:
        proven = any(j.proven for j in causes)
        chasm = any(j.chasm for j in causes)
        cause = next((j for j in causes if j.proven), causes[0])
        labels = sorted({_src_label(s) for s in sources}) if sources else []
        subject = " and ".join(labels) if labels else "the join result"
        if proven:
            code = "CHASM_TRAP" if chasm else "FANOUT_AGG"
            severity = "error"
            if count_rows:
                what = (f"{agg_sql} counts rows after {cause.description}. "
                        f"Rows are multiplied by that join, so the count is inflated.")
            else:
                what = (f"{agg_sql} is computed after {cause.description}. "
                        f"Rows of {subject} can be matched more than once, so this value is inflated.")
            if chasm:
                what += (" Two or more one to many joins in the same query multiply "
                         "each other (a chasm trap).")
            fix = (f"Aggregate {_short_name(cause.description)} to one row per join key "
                   f"in a CTE before joining, or compute {agg_sql} in a CTE at the grain "
                   f"of {subject} and join the result.")
        else:
            code = "UNKNOWN_GRAIN"
            severity = "error" if self.strict else "warning"
            what = (f"{agg_sql} is computed after {cause.description}, whose grain is "
                    f"unknown, so grainguard cannot prove that rows of {subject} "
                    f"are not duplicated.")
            fix = ("Declare the grain: add a dbt `unique` test or "
                   "`dbt_utils.unique_combination_of_columns`, list the table in "
                   "grainguard.yml, or add `-- grainguard: table <name> grain(<cols>)`.")
        ctx.result.findings.append(Finding(
            code=code, severity=severity, message=what, fix=fix,
            file=ctx.file, model=ctx.model, query_part=part,
            aggregate=agg_sql, join=cause.description,
        ))

    def _report_unresolved(self, ctx: "_Context", part: str, agg_sql: str, unresolved: List[str],
                           causes: List[JoinInfo], sources: List[_Source]) -> None:
        cause = next((j for j in causes if j.proven), causes[0])
        inflated = sorted({_src_label(s) for s in sources if s.inflated_by})
        tail = (f"If the column comes from {', '.join(inflated)} the value is inflated."
                if inflated else "If the column comes from the multiplied side the value is inflated.")
        ctx.result.findings.append(Finding(
            code="UNKNOWN_GRAIN", severity="error" if self.strict else "warning",
            message=(f"{agg_sql} uses column(s) {', '.join(unresolved)} that grainguard "
                     f"could not trace to a specific table, and this query (or a relation it reads) "
                     f"contains {cause.description}. {tail}"),
            fix=("Qualify the column with its table alias (for example o.amount), or list the "
                 "columns of the tables involved (dbt schema.yml or grainguard.yml) so SELECT * "
                 "can be expanded."),
            file=ctx.file, model=ctx.model, query_part=part,
            aggregate=agg_sql, join=cause.description,
        ))

    def _analyze_nested_selects(self, select: exp.Select, ctx: "_Context", part: str) -> None:
        for sub in _direct_nested_selects(select):
            self._analyze_query(sub, ctx, part=f"{part} > nested subquery")


@dataclass
class _Context:
    result: ModelResult
    model: str
    file: Optional[str]
    cte_scope: Dict[str, Relation]

    def child(self, scope: Dict[str, Relation]) -> "_Context":
        return _Context(result=self.result, model=self.model, file=self.file, cte_scope=scope)


@dataclass
class _JoinCondition:
    pairs: List[Tuple[str, str]] = field(default_factory=list)
    right_consts: Set[str] = field(default_factory=set)
    left_consts: Set[str] = field(default_factory=set)
    opaque: bool = False
    pinned_right_null: bool = False

    @property
    def right_cols(self) -> Set[str]:
        return {r for _, r in self.pairs} | self.right_consts

    @property
    def left_cols(self) -> Set[str]:
        return {l for l, _ in self.pairs} | self.left_consts


def _join_condition(join: exp.Join, sources: List[_Source], right_alias: str, right_rel: Relation,
                    where_terms: List[exp.Expression]) -> _JoinCondition:
    cond = _JoinCondition()
    left_aliases = {s.alias for s in sources}
    using = join.args.get("using")
    if using:
        for ident in using:
            c = norm(ident.name)
            owners = [s.alias for s in sources
                      if s.relation.columns and c in {norm(x) for x in s.relation.columns}]
            if len(owners) == 1:
                cond.pairs.append((f"{owners[0]}.{c}", c))
            else:
                for s in sources:
                    cond.pairs.append((f"{s.alias}.{c}", c))
    on = join.args.get("on")
    terms: List[exp.Expression] = list(_and_terms(on)) if on is not None else []
    for t in where_terms:
        if _mentions(t, right_alias):
            terms.append(t)

    for term in terms:
        if isinstance(term, exp.Is) and isinstance(term.expression, exp.Null) and \
                isinstance(term.this, exp.Column) and norm(term.this.table) == right_alias and \
                right_rel.is_unique_on({norm(term.this.name)}):
            cond.pinned_right_null = True
            continue
        if not isinstance(term, exp.EQ):
            if _mentions(term, right_alias) and isinstance(term, (exp.In, exp.Or, exp.NullSafeEQ)):
                cond.opaque = True
            continue
        a = _unwrap_lossless_cast(term.this)
        b = _unwrap_lossless_cast(term.expression)
        handled = False
        for col, other in ((a, b), (b, a)):
            if isinstance(col, exp.Column) and _is_constant(other):
                if norm(col.table) == right_alias:
                    cond.right_consts.add(norm(col.name))
                    handled = True
                elif norm(col.table) in left_aliases:
                    cond.left_consts.add(f"{norm(col.table)}.{norm(col.name)}")
                    handled = True
        if handled:
            continue
        if isinstance(a, exp.Column) and isinstance(b, exp.Column):
            matched = False
            for l, r in ((a, b), (b, a)):
                if norm(r.table) == right_alias and norm(l.table) in left_aliases:
                    cond.pairs.append((f"{norm(l.table)}.{norm(l.name)}", norm(r.name)))
                    matched = True
                    break
            if not matched:
                la, lb = norm(a.table), norm(b.table)
                if not la and lb == right_alias:
                    owner = _owner_of(norm(a.name), sources)
                    if owner:
                        cond.pairs.append((f"{owner}.{norm(a.name)}", norm(b.name)))
                        matched = True
                elif not lb and la == right_alias:
                    owner = _owner_of(norm(b.name), sources)
                    if owner:
                        cond.pairs.append((f"{owner}.{norm(b.name)}", norm(a.name)))
                        matched = True
            if not matched and _mentions(term, right_alias):
                cond.opaque = True
            continue
        if _mentions(term, right_alias):
            cond.opaque = True
    return cond


def _mentions(node: exp.Expression, alias: str) -> bool:
    return any(norm(c.table) == alias for c in node.find_all(exp.Column))


def _arg(node: exp.Expression, *names: str):
    for n in names:
        v = node.args.get(n)
        if v is not None:
            return v
    return None


def _is_set_operation(node: exp.Expression) -> bool:
    base = getattr(exp, "SetOperation", None)
    if base is not None and isinstance(node, base):
        return True
    return isinstance(node, (exp.Union, exp.Intersect, exp.Except))


def _is_query(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Select, exp.Subquery)) or _is_set_operation(node)


def _copy_relation(rel: Relation) -> Relation:
    new = Relation(
        name=rel.name,
        unique_keys=set(rel.unique_keys),
        columns=list(rel.columns) if rel.columns is not None else None,
        column_sources=dict(rel.column_sources),
        declared=rel.declared,
    )
    new.window_dedup = dict(rel.window_dedup)
    new.window_dedup_flags = set(rel.window_dedup_flags)
    new.column_inflation = {k: list(v) for k, v in rel.column_inflation.items()}
    new.fanout_causes = list(rel.fanout_causes)
    new.star_inflation = list(rel.star_inflation)
    new.all_inflated = rel.all_inflated
    new.explicit_columns = set(rel.explicit_columns)
    return new


def _table_full_name(t: exp.Table) -> str:
    parts = [p for p in (t.catalog, t.db, t.name) if p]
    return ".".join(norm(p) for p in parts)


def _qualify_keys(alias: str, keys: Set[Key]) -> Set[frozenset]:
    return {frozenset(f"{alias}.{c}" for c in k) for k in keys}


def _column_index(sources: List[_Source]) -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = {}
    for s in sources:
        for c in s.relation.columns or []:
            index.setdefault(norm(c), []).append(s.alias)
    return index


def _resolve_column_alias(col: exp.Column, sources: List[_Source],
                          col_index: Dict[str, List[str]]) -> Optional[str]:
    if col.table:
        a = norm(col.table)
        return a if any(s.alias == a for s in sources) else None
    name = norm(col.name)
    owners = col_index.get(name, [])
    if len(owners) == 1:
        return owners[0]
    if len(sources) == 1:
        return sources[0].alias
    if len(owners) > 1:
        return None
    unknown = [s for s in sources if s.relation.columns is None]
    if len(unknown) == 1:
        return unknown[0].alias
    return None


def _unresolved_inflation(sources: List[_Source]) -> List[JoinInfo]:
    if not sources or any(not s.inflated_by for s in sources):
        return []
    return [j for s in sources for j in s.inflated_by]


def _expression_inflation(node: exp.Expression, sources: List[_Source],
                          col_index: Dict[str, List[str]]) -> List[JoinInfo]:
    by_alias = {s.alias: s for s in sources}
    cols = [c for c in node.find_all(exp.Column) if not isinstance(c.this, exp.Star)]
    if not cols:
        return []
    causes: List[JoinInfo] = []
    for c in cols:
        a = _resolve_column_alias(c, sources, col_index)
        if a is None or a not in by_alias:
            return []
        cc, certain = by_alias[a].column_causes(c.name)
        if not cc or not certain:
            return []
        causes.extend(cc)
    return causes


def _expand_star(sources: List[_Source], table_alias: Optional[str]):
    out = []
    for s in sources:
        if table_alias is not None and s.alias != table_alias:
            continue
        if s.relation.columns is None:
            return None
        out.extend((s.alias, norm(c)) for c in s.relation.columns)
    return out


def _unwrap_lossless_cast(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.Cast, exp.TryCast)):
        to = node.args.get("to")
        type_name = (to.this.name if isinstance(to, exp.DataType) and hasattr(to.this, "name")
                     else str(to)).upper()
        if type_name not in _LOSSLESS_CAST_TYPES:
            break
        node = node.this
    return node


def _is_constant(node: exp.Expression) -> bool:
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return True
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return True
    if isinstance(node, (exp.Cast, exp.TryCast)) and _is_constant(node.this):
        return True
    return False


def _owner_of(col: str, sources: List[_Source]) -> Optional[str]:
    owners = [s.alias for s in sources if s.relation.columns and col in {norm(x) for x in s.relation.columns}]
    if len(owners) == 1:
        return owners[0]
    if len(sources) == 1:
        return sources[0].alias
    return None


def _and_terms(node: Optional[exp.Expression]):
    if node is None:
        return
    if isinstance(node, exp.And):
        yield from _and_terms(node.this)
        yield from _and_terms(node.expression)
    elif isinstance(node, exp.Paren):
        yield from _and_terms(node.this)
    else:
        yield node


def _describe_join(rel: Relation, alias: str, join: exp.Join) -> str:
    label = rel.name if rel.name == alias or not alias else f"{rel.name} (alias {alias})"
    on = join.args.get("on")
    using = join.args.get("using")
    cond = ""
    if on is not None:
        cond = " on " + on.sql()
    elif using:
        cond = " using (" + ", ".join(u.name for u in using) + ")"
    words = [join.args.get("method") or "", join.args.get("side") or "", join.args.get("kind") or ""]
    kind = " ".join(w for w in words if w).strip()
    kind = (kind + " join").strip() if kind else "join"
    grain = f" [{rel.describe_grain()}]" if rel.grain_known else ""
    return f"{kind} to {label}{cond}{grain}"


def _describe_left(sources: List[_Source], left_cols: Set[str], acc_keys: Set[frozenset],
                   acc_known: bool) -> str:
    names = ", ".join(_src_label(s) for s in sources)
    cols = ", ".join(sorted(left_cols)) or "the join columns"
    if not acc_known:
        return f"the grain of the left side ({names}) is unknown"
    keys = " or ".join("(" + ", ".join(sorted(k)) + ")" for k in sorted(acc_keys, key=lambda k: sorted(k))) or "nothing"
    return f"the left side ({names}) is unique on {keys}, not on {cols}"


def _src_label(s: _Source) -> str:
    return s.relation.name if s.relation.name == s.alias else f"{s.relation.name} (alias {s.alias})"


def _short_name(join_desc: str) -> str:
    try:
        after = join_desc.split(" to ", 1)[1]
        return after.split(" on ", 1)[0].split(" using ", 1)[0].split(" [", 1)[0].split(" (one row", 1)[0]
    except IndexError:
        return "the joined relation"


def _is_window_function(node: exp.Expression) -> bool:
    child = node
    p = node.parent
    while p is not None:
        if isinstance(p, exp.Window):
            return p.this is child
        if isinstance(p, (exp.Select, exp.Subquery)):
            return False
        child = p
        p = p.parent
    return False


def _inside_nested_select(node: exp.Expression, select: exp.Select) -> bool:
    p = node.parent
    while p is not None and p is not select:
        if isinstance(p, (exp.Select, exp.Subquery)):
            return True
        p = p.parent
    return False


def _is_aggregate(node: exp.Expression) -> bool:
    if isinstance(node, exp.AggFunc):
        return True
    if isinstance(node, exp.Anonymous):
        return norm(str(node.this)) in _ANON_AGG_NAMES
    return False


def _contains_aggregate(node: exp.Expression) -> bool:
    return any(_is_aggregate(n) for n in node.walk())


def _ancestors_until_select(node: exp.Expression, select: exp.Select):
    p = node.parent
    while p is not None and p is not select:
        yield p
        p = p.parent


def _group_aggregates(select: exp.Select):
    roots = list(select.expressions)
    for key in ("having", "order", "qualify"):
        v = select.args.get(key)
        if v is not None:
            roots.append(v)
    seen = set()
    for root in roots:
        for node in root.walk():
            if not _is_aggregate(node) or id(node) in seen:
                continue
            if _is_window_function(node) or _inside_nested_select(node, select):
                continue
            if any(_is_aggregate(a) for a in _ancestors_until_select(node, select)):
                continue
            seen.add(id(node))
            yield node


def _has_group_aggregate(select: exp.Select) -> bool:
    for _ in _group_aggregates(select):
        return True
    return False


def _is_immune(agg: exp.Expression) -> bool:
    if isinstance(agg, _IDEMPOTENT_AGGS):
        return True
    if isinstance(agg.args.get("this"), exp.Distinct) or agg.args.get("distinct"):
        return True
    if isinstance(agg, exp.Anonymous):
        name = norm(str(agg.this))
        if name in _ANON_IMMUNE_NAMES:
            return True
        if any(isinstance(e, exp.Distinct) for e in agg.expressions):
            return True
    return False


def _aggregate_columns(agg: exp.Expression) -> Tuple[List[exp.Column], List[exp.Column]]:
    values: List[exp.Column] = []
    conds: List[exp.Column] = []
    for c in agg.find_all(exp.Column):
        if isinstance(c.this, exp.Star):
            continue
        in_condition = False
        child: exp.Expression = c
        p = c.parent
        while p is not None and p is not agg:
            if isinstance(p, _PREDICATES):
                in_condition = True
                break
            if isinstance(p, (exp.If, exp.Case)) and p.args.get("this") is child:
                in_condition = True
                break
            child = p
            p = p.parent
        (conds if in_condition else values).append(c)
    return values, conds


def _groups_cover_a_key(select: exp.Select, sources: List[_Source], acc_keys: Set[frozenset],
                        col_index: Dict[str, List[str]]) -> bool:
    group = select.args.get("group")
    if group is None or group.args.get("all") or not acc_keys:
        return False
    gcols: Set[str] = set()
    for g in group.expressions:
        g_u = _unwrap_lossless_cast(g)
        if isinstance(g, exp.Literal) and g.is_int:
            idx = int(g.this) - 1
            projs = select.expressions
            if not (0 <= idx < len(projs)):
                return False
            inner = projs[idx].this if isinstance(projs[idx], exp.Alias) else projs[idx]
            g_u = _unwrap_lossless_cast(inner)
        if not isinstance(g_u, exp.Column):
            return False
        a = _resolve_column_alias(g_u, sources, col_index)
        if a is None:
            return False
        gcols.add(f"{a}.{norm(g_u.name)}")
    return bool(gcols) and any(k <= gcols for k in acc_keys)


def _direct_nested_selects(select: exp.Select):
    skip_roots = []
    from_ = _arg(select, "from_", "from")
    if from_ is not None:
        skip_roots.append(from_)
    skip_roots.extend(select.args.get("joins") or [])
    with_ = _arg(select, "with_", "with")
    if with_ is not None:
        skip_roots.append(with_)
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        p = sub.parent
        nested_in_other = False
        skipped = False
        while p is not None and p is not select:
            if isinstance(p, exp.Select):
                nested_in_other = True
                break
            if any(p is r for r in skip_roots):
                skipped = True
                break
            p = p.parent
        if not nested_in_other and not skipped:
            yield sub


def _group_key(group: exp.Group, select: exp.Select, columns: List[str],
               origins: Dict[str, ColumnOrigin], plain_outputs: Set[str],
               sources: List[_Source]) -> Optional[Key]:
    names: List[str] = []
    projections = select.expressions
    known_source_cols: Set[str] = set()
    all_columns_known = bool(sources) and all(s.relation.columns is not None for s in sources)
    for s in sources:
        for c in s.relation.columns or []:
            known_source_cols.add(norm(c))
    for g in group.expressions:
        if isinstance(g, exp.Literal) and g.is_int:
            idx = int(g.this) - 1
            if 0 <= idx < len(columns):
                names.append(columns[idx])
                continue
            return None
        g_u = _unwrap_lossless_cast(g)
        if isinstance(g_u, exp.Column):
            target = (norm(g_u.table), norm(g_u.name))
            found = None
            for out_name, o in origins.items():
                if o.source_column == target[1] and (not target[0] or o.source_alias == target[0]):
                    found = out_name
                    break
            if found is None and not target[0] and target[1] in columns:
                if target[1] in plain_outputs or (all_columns_known and target[1] not in known_source_cols):
                    found = target[1]
                else:
                    return None
            if found is None:
                return None
            names.append(found)
            continue
        g_sql = g.sql()
        found = None
        for proj in projections:
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if inner.sql() == g_sql:
                found = norm(proj.alias_or_name)
                break
        if found is None:
            return None
        names.append(found)
    return frozenset(names)


def _rownumber_partition(cond: exp.Expression, select: exp.Select, sources: List[_Source],
                         col_index: Dict[str, List[str]]) -> Optional[Set[str]]:
    for term in _and_terms(cond):
        if not isinstance(term, exp.EQ):
            continue
        a, b = term.this, term.expression
        if isinstance(b, exp.Literal) and b.is_int and int(b.this) == 1:
            win = a
        elif isinstance(a, exp.Literal) and a.is_int and int(a.this) == 1:
            win = b
        else:
            continue
        if isinstance(win, exp.Column):
            for proj in select.expressions:
                if isinstance(proj, exp.Alias) and norm(proj.alias) == norm(win.name):
                    win = proj.this
                    break
        if isinstance(win, exp.Window) and isinstance(win.this, exp.RowNumber):
            cols: Set[str] = set()
            for p in win.args.get("partition_by") or []:
                p = _unwrap_lossless_cast(p)
                if not isinstance(p, exp.Column):
                    return None
                a2 = _resolve_column_alias(p, sources, col_index)
                if a2 is None:
                    return None
                cols.add(f"{a2}.{norm(p.name)}")
            return cols
    return None


def _rownumber_window(node: exp.Expression) -> Tuple[Optional[exp.Window], bool]:
    if isinstance(node, exp.Window) and isinstance(node.this, exp.RowNumber):
        return node, False
    if isinstance(node, exp.EQ):
        a, b = node.this, node.expression
        for win, lit in ((a, b), (b, a)):
            if isinstance(win, exp.Window) and isinstance(win.this, exp.RowNumber) and \
                    isinstance(lit, exp.Literal) and lit.is_int and int(lit.this) == 1:
                return win, True
    return None, False


def _where_filters_to_one(cond: exp.Expression, colname: str, alias: str, is_flag: bool) -> bool:
    def is_col(x: exp.Expression) -> bool:
        return isinstance(x, exp.Column) and norm(x.name) == colname and (not x.table or norm(x.table) == alias)

    for term in _and_terms(cond):
        if is_flag:
            if is_col(term):
                return True
            if isinstance(term, (exp.EQ, exp.Is)):
                a, b = term.this, term.expression
                for x, y in ((a, b), (b, a)):
                    if is_col(x) and isinstance(y, exp.Boolean) and y.this is True:
                        return True
            continue
        if isinstance(term, exp.EQ):
            a, b = term.this, term.expression
            for x, y in ((a, b), (b, a)):
                if is_col(x) and isinstance(y, exp.Literal) and y.is_int and int(y.this) == 1:
                    return True
    return False


def _map_qualified_to_output(qcols: Set[str], origins: Dict[str, ColumnOrigin],
                             star_aliases: Set[str]) -> Optional[Key]:
    reverse = {(o.source_alias, o.source_column): out for out, o in origins.items()}
    mapped = []
    for q in qcols:
        a, c = q.split(".", 1)
        if (a, c) in reverse:
            mapped.append(reverse[(a, c)])
        elif a in star_aliases:
            mapped.append(c)
        else:
            return None
    return frozenset(mapped)


def _output_name_for(src_alias: str, src_col: str, origins: Dict[str, ColumnOrigin]) -> Optional[str]:
    for out, o in origins.items():
        if o.source_alias == src_alias and o.source_column == src_col:
            return out
    return None
