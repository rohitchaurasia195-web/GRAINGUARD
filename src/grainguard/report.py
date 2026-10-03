from __future__ import annotations

import json
import textwrap
from typing import List

from .model import ModelResult


def _wrap(text: str, indent: str = "      ") -> str:
    return "\n".join(textwrap.wrap(text, width=100, initial_indent=indent, subsequent_indent=indent))


def render_text(results: List[ModelResult], show_grain: bool = False, color: bool = True) -> str:
    red = "\033[31m" if color else ""
    yellow = "\033[33m" if color else ""
    green = "\033[32m" if color else ""
    dim = "\033[2m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""

    lines: List[str] = []
    n_err = n_warn = 0
    for r in sorted(results, key=lambda x: (x.file or "", x.name)):
        if show_grain:
            grain = r.output.describe_grain() if r.output is not None else "no SELECT found"
            lines.append(f"{bold}{r.name}{reset} {dim}({r.file}){reset}: {grain}")
        for f in r.findings:
            if f.severity == "error":
                n_err += 1
                tag = f"{red}ERROR{reset}"
            elif f.severity == "warning":
                n_warn += 1
                tag = f"{yellow}WARN{reset} "
            else:
                tag = f"{dim}INFO{reset} "
            where = f.file or r.name
            if f.query_part and f.query_part != "main query":
                where += f" > {f.query_part}"
            lines.append(f"{tag} {f.code:<13} {where}")
            lines.append(_wrap(f.message))
            if f.fix:
                lines.append(_wrap(f"Fix: {f.fix}"))
            lines.append("")

    models = len(results)
    joins = sum(r.joins_total for r in results)
    safe = sum(r.joins_proven_safe for r in results)
    fan = sum(r.joins_fanout for r in results)
    unk = sum(r.joins_unknown for r in results)
    aggs = sum(r.aggregates_total for r in results)
    aggs_j = sum(r.aggregates_after_join for r in results)
    summary = (f"{models} model(s), {joins} join(s): {safe} proven safe, {fan} fan out, "
               f"{unk} unknown grain. {aggs} aggregate(s), {aggs_j} of them after a join.")
    if n_err:
        verdict = f"{red}{bold}{n_err} error(s){reset}, {n_warn} warning(s)."
    elif n_warn:
        verdict = f"{yellow}{bold}0 errors{reset}, {n_warn} warning(s)."
    else:
        verdict = f"{green}{bold}All clear.{reset} No aggregate sits on a fan out join."
    lines.append(summary)
    lines.append(verdict)
    return "\n".join(lines)


def render_json(results: List[ModelResult]) -> str:
    payload = {
        "models": [
            {
                "name": r.name,
                "file": r.file,
                "grain": (sorted(sorted(k) for k in r.output.unique_keys) if r.output is not None else None),
                "columns": r.output.columns if r.output is not None else None,
                "stats": {
                    "joins_total": r.joins_total,
                    "joins_proven_safe": r.joins_proven_safe,
                    "joins_fanout": r.joins_fanout,
                    "joins_unknown": r.joins_unknown,
                    "aggregates_total": r.aggregates_total,
                    "aggregates_after_join": r.aggregates_after_join,
                },
                "findings": [f.to_dict() for f in r.findings],
            }
            for r in results
        ],
        "summary": {
            "models": len(results),
            "errors": sum(len(r.errors) for r in results),
            "warnings": sum(len(r.warnings) for r in results),
        },
    }
    return json.dumps(payload, indent=2)
