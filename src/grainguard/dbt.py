from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

from .analyzer import Analyzer
from .facts import FactStore, facts_from_config, facts_from_dbt_yaml_files, load_config_file, parse_pragmas
from .model import Finding, ModelResult, Relation, norm

_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_STATEMENT = re.compile(r"\{%.*?%\}", re.DOTALL)
_EXPR = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_REF_CALL = re.compile(r"^ref\s*\(\s*(?:['\"]([^'\"]+)['\"]\s*,\s*)?['\"]([^'\"]+)['\"]")
_SOURCE_CALL = re.compile(r"^source\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]")
_REF_ANY = re.compile(r"\bref\s*\(\s*(?:['\"]([^'\"]+)['\"]\s*,\s*)?['\"]([^'\"]+)['\"]")
_DANGLING_COMMA = [
    (re.compile(r",(\s*)(from|where|group|order|having|qualify|window|limit|\))\b", re.IGNORECASE), r"\1\2"),
    (re.compile(r"\b(select|distinct|by|\()(\s*),", re.IGNORECASE), r"\1\2"),
    (re.compile(r",(\s*),"), r","),
]

PLACEHOLDER = "__jinja__"

_EXPR_KEYWORDS = {
    "select", "by", "and", "or", "on", "where", "then", "else", "when", "as", "from", "join",
    "in", "not", "case", "distinct", "having", "set", "values", "over", "partition", "order",
    "group", "with", "union", "all", "like", "ilike", "between", "exists", "limit", "offset",
    "is", "interval", "filter", "using", "return", "returns", "qualify", "window", "except",
    "intersect", "lateral", "cross", "left", "right", "inner", "outer", "full", "semi", "anti",
    "if", "iff", "coalesce", "cast", "nullif", "end", "top", "into", "insert", "update", "delete",
}
_IF_BLOCK = re.compile(
    r"\{%-?\s*if\b((?:(?!\{%-?\s*(?:if|endif)\b).)*?)\{%-?\s*endif\s*-?%\}", re.DOTALL)
_ELSE_SPLIT = re.compile(r"\{%-?\s*(?:else|elif)\b", re.DOTALL)
_TAG = re.compile(r"\{%.*?%\}", re.DOTALL)
_TYPE_MACRO = re.compile(r"\btype_(string|timestamp|int|bigint|float|numeric|boolean|date)\b")


def _keep_first_branch(sql: str) -> str:
    for _ in range(50):
        m = _IF_BLOCK.search(sql)
        if not m:
            break
        body = m.group(1)
        close = body.find("%}")
        body = body[close + 2:] if close >= 0 else body
        parts = _ELSE_SPLIT.split(body, maxsplit=1)
        first = parts[0]
        sql = sql[:m.start()] + first + sql[m.end():]
    return sql


def _previous_word(text: str) -> str:
    i = len(text) - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    j = i
    while j >= 0 and (text[j].isalnum() or text[j] == "_"):
        j -= 1
    return text[j + 1:i + 1].lower(), (text[i] if i >= 0 else "")


def strip_jinja(sql: str, model_name: str = "this_model", placeholder: Optional[str] = PLACEHOLDER,
                smart: bool = True) -> str:
    sql = _COMMENT.sub(" ", sql)
    sql = _keep_first_branch(sql)
    sql = _TAG.sub(" ", sql)

    out: List[str] = []
    pos = 0
    for m in _EXPR.finditer(sql):
        before = sql[pos:m.start()]
        out.append(before)
        pos = m.end()
        body = m.group(1).strip().strip("-").strip()
        if body.startswith("config"):
            out.append(" ")
            continue
        r = _REF_CALL.match(body)
        if r:
            out.append(r.group(2))
            continue
        s_ = _SOURCE_CALL.match(body)
        if s_:
            out.append(f"{s_.group(1)}.{s_.group(2)}")
            continue
        if body == "this":
            out.append(model_name)
            continue
        if _TYPE_MACRO.search(body):
            out.append("VARCHAR")
            continue
        if placeholder is None:
            out.append(" ")
            continue
        if smart:
            prev_word, prev_char = _previous_word("".join(out))
            following = sql[m.end():].lstrip()[:3].lower()
            expression_position = (
                prev_word in _EXPR_KEYWORDS
                or prev_char in ",(=<>+-*/|.&!"
                or prev_char == ""
                or following.startswith("as ")
                or following.startswith("as\n")
            )
            if not expression_position and (prev_char.isalnum() or prev_char in "_)\"'`*]"):
                out.append(" ")
                continue
        out.append(placeholder)
    out.append(sql[pos:])
    sql = "".join(out)
    if placeholder is None:
        for pat, rep in _DANGLING_COMMA:
            sql = pat.sub(rep, sql)
    return sql


def parseable_sql(raw: str, model_name: str, dialect: Optional[str]) -> str:
    import sqlglot

    candidates = [
        strip_jinja(raw, model_name, PLACEHOLDER, smart=True),
        strip_jinja(raw, model_name, PLACEHOLDER, smart=False),
        strip_jinja(raw, model_name, None),
        strip_jinja(raw, model_name, "NULL"),
    ]
    for cand in candidates:
        try:
            sqlglot.parse(cand, read=dialect)
            return cand
        except Exception:
            continue
    return candidates[0]


def find_refs(sql: str) -> List[str]:
    seen: List[str] = []
    for m in _REF_ANY.finditer(sql):
        name = norm(m.group(2))
        if name not in seen:
            seen.append(name)
    return seen


@dataclass
class Project:
    root: Path
    kind: str
    dialect: Optional[str]
    model_files: Dict[str, Path]
    facts: FactStore
    strict: bool = False
    ignored: List[str] = field(default_factory=list)


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _dialect_from_dbt(root: Path) -> Optional[str]:
    prof = root / "profiles.yml"
    if not prof.exists():
        return None
    try:
        data = yaml.safe_load(_read_text(prof)) or {}
    except Exception:
        return None
    for prof_name, prof_body in data.items():
        if not isinstance(prof_body, dict):
            continue
        outputs = prof_body.get("outputs") or {}
        target = prof_body.get("target")
        out = outputs.get(target) if target in outputs else next(iter(outputs.values()), None)
        if isinstance(out, dict) and out.get("type"):
            return _adapter_to_dialect(out["type"])
    return None


_ADAPTER_DIALECT = {
    "snowflake": "snowflake", "bigquery": "bigquery", "databricks": "databricks", "spark": "spark",
    "postgres": "postgres", "redshift": "redshift", "duckdb": "duckdb", "trino": "trino",
    "athena": "athena", "sqlserver": "tsql", "fabric": "tsql", "synapse": "tsql", "mysql": "mysql",
    "clickhouse": "clickhouse", "oracle": "oracle", "teradata": "teradata", "starrocks": "starrocks",
}


def _adapter_to_dialect(adapter: str) -> Optional[str]:
    return _ADAPTER_DIALECT.get(norm(adapter))


def load_project(path: Path, dialect: Optional[str] = None, config_path: Optional[Path] = None,
                 strict: bool = False) -> Project:
    path = Path(path)
    root = path if path.is_dir() else path.parent
    config: dict = {}
    cfg_file = config_path or (root / "grainguard.yml")
    if cfg_file and Path(cfg_file).exists():
        config = load_config_file(Path(cfg_file))
    facts = facts_from_config(config)
    dialect = dialect or config.get("dialect")

    dbt_project = root / "dbt_project.yml"
    if path.is_dir() and dbt_project.exists():
        proj = yaml.safe_load(_read_text(dbt_project)) or {}
        model_paths = proj.get("model-paths") or proj.get("source-paths") or ["models"]
        model_files: Dict[str, Path] = {}
        yaml_files: List[Path] = []
        for mp in model_paths:
            base = root / mp
            if not base.exists():
                continue
            for p in sorted(base.rglob("*.sql")):
                model_files[norm(p.stem)] = p
            yaml_files.extend(sorted(base.rglob("*.yml")) + sorted(base.rglob("*.yaml")))
        for extra in ("seeds", "snapshots"):
            for sp in proj.get(f"{extra[:-1]}-paths") or proj.get(f"{extra}-paths") or [extra]:
                base = root / sp
                if base.exists():
                    yaml_files.extend(sorted(base.rglob("*.yml")) + sorted(base.rglob("*.yaml")))
        facts.merge(facts_from_dbt_yaml_files(yaml_files))
        dialect = dialect or _dialect_from_dbt(root)
        return Project(root=root, kind="dbt", dialect=dialect, model_files=model_files,
                       facts=facts, strict=strict)

    files = [path] if path.is_file() else sorted(path.rglob("*.sql"))
    model_files = {}
    for p in files:
        key = norm(str(p.relative_to(root)) if p.is_relative_to(root) else p.name)
        model_files[key] = p
    return Project(root=root, kind="sql", dialect=dialect, model_files=model_files,
                   facts=facts, strict=strict)


def _topo_order(model_files: Dict[str, Path], sql_texts: Dict[str, str]) -> List[str]:
    deps: Dict[str, Set[str]] = {}
    for name, sql in sql_texts.items():
        deps[name] = {r for r in find_refs(sql) if r in model_files and r != name}
    order: List[str] = []
    state: Dict[str, int] = {}

    def visit(n: str) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            return
        state[n] = 1
        for d in sorted(deps.get(n, ())):
            visit(d)
        state[n] = 2
        order.append(n)

    for n in sorted(model_files):
        visit(n)
    return order


def apply_declared(output: Relation, declared: Relation) -> None:
    for key in declared.unique_keys:
        if key and all(c in output.column_inflation for c in key):
            output.column_inflation = {}
            output.fanout_causes = []
            output.star_inflation = []
            output.all_inflated = False
            break
    output.unique_keys |= declared.unique_keys
    output.declared = output.declared or declared.declared
    if output.columns is None and declared.columns:
        output.columns = list(declared.columns)


def analyze_project(project: Project) -> List[ModelResult]:
    texts: Dict[str, str] = {}
    pragmas = {}
    for name, p in project.model_files.items():
        raw = _read_text(p)
        pr = parse_pragmas(raw)
        pragmas[name] = pr
        project.facts.merge(pr.store)
        texts[name] = raw

    order = _topo_order(project.model_files, texts) if project.kind == "dbt" else sorted(texts)
    analyzer = Analyzer(facts=project.facts, dialect=project.dialect, strict=project.strict)
    results: List[ModelResult] = []
    for name in order:
        raw = texts[name]
        pr = pragmas[name]
        rel_path = str(project.model_files[name].relative_to(project.root))
        if pr.ignore:
            project.ignored.append(name)
            continue
        sql = parseable_sql(raw, name, project.dialect) if project.kind == "dbt" else raw
        res = analyzer.analyze_sql(sql, name=name, file=rel_path, self_keys=pr.self_keys)
        if res.output is not None:
            declared = project.facts.lookup(name)
            if declared is not None:
                apply_declared(res.output, declared)
            analyzer.model_outputs[name] = res.output
        results.append(res)
    return results
