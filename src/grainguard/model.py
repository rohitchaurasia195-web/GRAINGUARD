from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set

Key = FrozenSet[str]


def norm(name: str) -> str:
    return name.strip().strip('"').strip("`").strip("[]").lower()


def key_of(cols) -> Key:
    return frozenset(norm(c) for c in cols)


@dataclass
class Relation:

    name: str
    unique_keys: Set[Key] = field(default_factory=set)
    columns: Optional[List[str]] = None
    column_sources: Dict[str, "ColumnOrigin"] = field(default_factory=dict)
    declared: bool = False
    window_dedup: Dict[str, List[str]] = field(default_factory=dict)
    window_dedup_flags: Set[str] = field(default_factory=set)
    column_inflation: Dict[str, list] = field(default_factory=dict)
    fanout_causes: list = field(default_factory=list)
    star_inflation: list = field(default_factory=list)
    all_inflated: bool = False
    explicit_columns: Set[str] = field(default_factory=set)

    @property
    def grain_known(self) -> bool:
        return bool(self.unique_keys)

    @property
    def single_row(self) -> bool:
        return frozenset() in self.unique_keys

    def is_unique_on(self, cols: Set[str]) -> bool:
        cols = {norm(c) for c in cols}
        return any(k <= cols for k in self.unique_keys)

    def describe_grain(self) -> str:
        if not self.unique_keys:
            return "unknown grain"
        if self.single_row:
            return "a single row"
        keys = sorted(self.unique_keys, key=lambda k: (len(k), sorted(k)))
        text = "one row per (" + ", ".join(sorted(keys[0])) + ")"
        if len(keys) > 1:
            others = ["(" + ", ".join(sorted(k)) + ")" for k in keys[1:]]
            text += ", also unique on " + " and ".join(others)
        return text


@dataclass(frozen=True)
class ColumnOrigin:
    source_alias: str
    source_column: str


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    file: Optional[str] = None
    model: Optional[str] = None
    query_part: Optional[str] = None
    aggregate: Optional[str] = None
    join: Optional[str] = None
    fix: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "file": self.file,
            "model": self.model,
            "query_part": self.query_part,
            "aggregate": self.aggregate,
            "join": self.join,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass
class ModelResult:
    name: str
    file: Optional[str]
    output: Optional[Relation]
    findings: List[Finding] = field(default_factory=list)
    aggregates_total: int = 0
    aggregates_after_join: int = 0
    joins_total: int = 0
    joins_proven_safe: int = 0
    joins_fanout: int = 0
    joins_unknown: int = 0

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]
