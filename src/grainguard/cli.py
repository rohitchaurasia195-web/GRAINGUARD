from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .analyzer import Analyzer
from .dbt import analyze_project, load_project
from .facts import FactStore, facts_from_config, load_config_file
from .report import render_json, render_text


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dialect", help="SQL dialect for parsing (snowflake, bigquery, spark, duckdb, postgres, ...)")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--strict", action="store_true",
                   help="treat aggregates after joins of unknown grain as errors, not warnings")
    p.add_argument("--config", help="path to grainguard.yml (default: <project>/grainguard.yml)")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--fail-on", choices=["error", "warning", "never"], default="error",
                   help="exit code 1 when findings of this severity or worse exist (default: error)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grainguard",
                                     description="Stop joins from silently inflating your aggregates.")
    parser.add_argument("--version", action="version", version=f"grainguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check a dbt project, a folder of .sql files, or one .sql file")
    check.add_argument("path")
    _add_common(check)

    grain = sub.add_parser("grain", help="like check, but also print the inferred grain of every model")
    grain.add_argument("path")
    _add_common(grain)

    one = sub.add_parser("sql", help="check a single query passed as text")
    one.add_argument("query")
    _add_common(one)
    return parser


def _exit_code(results, fail_on: str) -> int:
    errors = sum(len(r.errors) for r in results)
    warnings = sum(len(r.warnings) for r in results)
    if fail_on == "never":
        return 0
    if fail_on == "warning":
        return 1 if (errors or warnings) else 0
    return 1 if errors else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    color = (not args.no_color) and sys.stdout.isatty() and args.format == "text"

    if args.command == "sql":
        facts = FactStore()
        if args.config and Path(args.config).exists():
            facts = facts_from_config(load_config_file(Path(args.config)))
        analyzer = Analyzer(facts=facts, dialect=args.dialect, strict=args.strict)
        results = [analyzer.analyze_sql(args.query, name="query")]
        show_grain = True
    else:
        project = load_project(Path(args.path), dialect=args.dialect,
                               config_path=Path(args.config) if args.config else None,
                               strict=args.strict)
        results = analyze_project(project)
        show_grain = args.command == "grain"

    if args.format == "json":
        print(render_json(results))
    else:
        print(render_text(results, show_grain=show_grain, color=color))
    return _exit_code(results, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
