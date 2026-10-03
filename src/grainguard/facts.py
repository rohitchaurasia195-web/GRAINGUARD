from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import yaml

from .model import Key, Relation, key_of, norm


@dataclass
class FactStore:
    keys: Dict[str, Set[Key]] = field(default_factory=dict)
    columns: Dict[str, List[str]] = field(default_factory=dict)

    def add_key(self, relation: str, cols: Iterable[str]) -> None:
        name = norm(relation)
        self.keys.setdefault(name, set()).add(key_of(cols))

    def add_columns(self, relation: str, cols: Iterable[str]) -> None:
        name = norm(relation)
        existing = self.columns.setdefault(name, [])
        for c in cols:
            c = norm(c)
            if c not in existing:
                existing.append(c)

    def _candidates(self, name: str) -> List[str]:
        name = norm(name)
        parts = name.split(".")
        return [".".join(parts[i:]) for i in range(len(parts))]

    def lookup(self, name: str) -> Optional[Relation]:
        for cand in self._candidates(name):
            if cand in self.keys or cand in self.columns:
                rel = Relation(name=cand, declared=cand in self.keys)
                rel.unique_keys = set(self.keys.get(cand, set()))
                cols = self.columns.get(cand)
                rel.columns = list(cols) if cols else None
                return rel
        return None

    def merge(self, other: "FactStore") -> None:
        for k, v in other.keys.items():
            self.keys.setdefault(k, set()).update(v)
        for k, v in other.columns.items():
            self.add_columns(k, v)


def load_config_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def facts_from_config(config: dict) -> FactStore:
    store = FactStore()
    tables = config.get("tables") or {}
    for name, spec in tables.items():
        if spec is None:
            continue
        if isinstance(spec, dict):
            keys = spec.get("grain") or spec.get("keys") or spec.get("unique") or []
            cols = spec.get("columns") or []
            if cols:
                store.add_columns(name, cols)
        else:
            keys = spec
        if isinstance(keys, str):
            keys = [keys]
        if keys and all(isinstance(k, str) for k in keys):
            store.add_key(name, keys)
        else:
            for k in keys:
                if isinstance(k, str):
                    k = [k]
                store.add_key(name, k)
    return store


_PRAGMA = re.compile(r"--\s*grainguard\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_TABLE_GRAIN = re.compile(
    r"^table\s+([\w\.\"`]+)\s+(?:grain|unique)\s*\(([^)]*)\)\s*$", re.IGNORECASE
)
_SELF_GRAIN = re.compile(r"^(?:grain|unique)\s*\(([^)]*)\)\s*$", re.IGNORECASE)


@dataclass
class Pragmas:
    ignore: bool = False
    self_keys: Set[Key] = field(default_factory=set)
    store: FactStore = field(default_factory=FactStore)


def parse_pragmas(sql: str) -> Pragmas:
    out = Pragmas()
    for m in _PRAGMA.finditer(sql):
        body = m.group(1).strip()
        if body.lower() == "ignore":
            out.ignore = True
            continue
        tm = _TABLE_GRAIN.match(body)
        if tm:
            cols = [c for c in (x.strip() for x in tm.group(2).split(",")) if c]
            out.store.add_key(tm.group(1), cols)
            continue
        sm = _SELF_GRAIN.match(body)
        if sm:
            cols = [c for c in (x.strip() for x in sm.group(1).split(",")) if c]
            out.self_keys.add(key_of(cols))
    return out


_UNIQUE_TEST_NAMES = {"unique", "dbt_constraints.primary_key", "dbt_constraints.unique_key"}
_COMBO_TEST_NAMES = {
    "dbt_utils.unique_combination_of_columns": "combination_of_columns",
    "unique_combination_of_columns": "combination_of_columns",
    "dbt_expectations.expect_compound_columns_to_be_unique": "column_list",
    "expect_compound_columns_to_be_unique": "column_list",
}


def _tests_of(node: dict) -> list:
    tests = []
    for k in ("tests", "data_tests"):
        v = node.get(k)
        if isinstance(v, list):
            tests.extend(v)
    return tests


def _iter_test_specs(tests: list):
    for t in tests:
        if isinstance(t, str):
            yield t, {}
        elif isinstance(t, dict) and len(t) == 1:
            name, args = next(iter(t.items()))
            yield name, (args if isinstance(args, dict) else {})


def _node_facts(store: FactStore, rel_name: str, node: dict) -> None:
    cols = node.get("columns") or []
    col_names = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")]
    if col_names:
        store.add_columns(rel_name, col_names)
    for c in cols:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        for tname, _ in _iter_test_specs(_tests_of(c)):
            if tname in _UNIQUE_TEST_NAMES:
                store.add_key(rel_name, [c["name"]])
        for cons in c.get("constraints") or []:
            if isinstance(cons, dict) and cons.get("type") in ("primary_key", "unique"):
                store.add_key(rel_name, [c["name"]])
    for tname, args in _iter_test_specs(_tests_of(node)):
        if tname in _COMBO_TEST_NAMES:
            arg = _COMBO_TEST_NAMES[tname]
            combo = args.get(arg) or []
            if combo:
                store.add_key(rel_name, combo)
        elif tname in _UNIQUE_TEST_NAMES and args.get("column_name"):
            store.add_key(rel_name, [args["column_name"]])
    for cons in node.get("constraints") or []:
        if isinstance(cons, dict) and cons.get("type") in ("primary_key", "unique"):
            if cons.get("columns"):
                store.add_key(rel_name, cons["columns"])
    meta = node.get("meta") or {}
    gg = node.get("grainguard") or meta.get("grainguard") or {}
    if isinstance(gg, dict):
        keys = gg.get("grain") or gg.get("unique") or []
        if keys and all(isinstance(k, str) for k in keys):
            store.add_key(rel_name, keys)
        else:
            for k in keys:
                store.add_key(rel_name, [k] if isinstance(k, str) else k)


def facts_from_dbt_yaml(data: dict) -> FactStore:
    store = FactStore()
    if not isinstance(data, dict):
        return store
    for section in ("models", "seeds", "snapshots"):
        for node in data.get(section) or []:
            if isinstance(node, dict) and node.get("name"):
                _node_facts(store, node["name"], node)
    for src in data.get("sources") or []:
        if not isinstance(src, dict) or not src.get("name"):
            continue
        schema = src.get("schema") or src["name"]
        for tbl in src.get("tables") or []:
            if not isinstance(tbl, dict) or not tbl.get("name"):
                continue
            ident = tbl.get("identifier") or tbl["name"]
            _node_facts(store, f"{src['name']}.{tbl['name']}", tbl)
            _node_facts(store, f"{schema}.{ident}", tbl)
    return store


def facts_from_dbt_yaml_files(paths: Iterable[Path]) -> FactStore:
    store = FactStore()
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception:
            continue
        store.merge(facts_from_dbt_yaml(data))
    return store
