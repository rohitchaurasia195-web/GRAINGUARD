from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grainguard.dbt import analyze_project, load_project

FIELDS = [
    "repo", "models", "parse_errors", "joins_total", "joins_proven_safe", "joins_fanout",
    "joins_unknown", "aggregates_total", "aggregates_after_join", "fanout_errors", "chasm_errors",
    "unknown_warnings", "models_with_errors",
]


def clone(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(dest)],
                       check=True, timeout=300, capture_output=True)
        return True
    except Exception as e:
        print(f"  clone failed for {url}: {e}", file=sys.stderr)
        return False


def survey(url: str, subdir: str, workdir: Path, dialect: str | None) -> dict | None:
    name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = workdir / name
    if not clone(url, dest):
        return None
    root = dest / subdir if subdir else dest
    if not (root / "dbt_project.yml").exists():
        found = list(root.rglob("dbt_project.yml"))
        found = [f for f in found if "dbt_packages" not in f.parts and "dbt_modules" not in f.parts]
        if not found:
            print(f"  no dbt_project.yml in {url}", file=sys.stderr)
            return None
        root = found[0].parent
    project = load_project(root, dialect=dialect)
    results = analyze_project(project)
    row = {k: 0 for k in FIELDS}
    row["repo"] = url.split("github.com/")[-1]
    row["models"] = len(results)
    for r in results:
        row["parse_errors"] += sum(1 for f in r.findings if f.code == "PARSE_ERROR")
        row["joins_total"] += r.joins_total
        row["joins_proven_safe"] += r.joins_proven_safe
        row["joins_fanout"] += r.joins_fanout
        row["joins_unknown"] += r.joins_unknown
        row["aggregates_total"] += r.aggregates_total
        row["aggregates_after_join"] += r.aggregates_after_join
        row["fanout_errors"] += sum(1 for f in r.findings if f.code == "FANOUT_AGG")
        row["chasm_errors"] += sum(1 for f in r.findings if f.code == "CHASM_TRAP")
        row["unknown_warnings"] += sum(1 for f in r.findings if f.code == "UNKNOWN_GRAIN")
        row["models_with_errors"] += 1 if r.errors else 0
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", required=True, help="text file with one GitHub URL per line")
    ap.add_argument("--out", default="survey_results.csv")
    ap.add_argument("--workdir", default="survey_work")
    ap.add_argument("--dialect", default=None)
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for line in Path(args.repos).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        url = parts[0]
        subdir = parts[1] if len(parts) > 1 and parts[1] != "." else ""
        dialect = parts[2] if len(parts) > 2 else args.dialect
        print(f"surveying {url} ...", file=sys.stderr)
        row = survey(url, subdir, workdir, dialect)
        if row:
            rows.append(row)
            print("  " + ", ".join(f"{k}={row[k]}" for k in FIELDS[1:]), file=sys.stderr)

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    total = {k: sum(r[k] for r in rows) for k in FIELDS[1:]}
    print(f"\n{len(rows)} projects surveyed. Totals: " + ", ".join(f"{k}={v}" for k, v in total.items()))
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
