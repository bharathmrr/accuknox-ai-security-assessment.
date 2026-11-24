"""Task 2 - Fetch student test scores, analyse them, plot a bar chart and save CSV.

Data source
-----------
No reliable *public* student-score API exists, so this program reads from a
**local mock API fixture** (``data/student_scores.json``) by default. The same
code path also accepts an HTTP(S) URL (``--source https://...``) that returns the
same JSON shape, e.g. a mock server you run yourself. No real endpoint is
invented or hard-coded.

Expected JSON shape::

    {"subjects": ["Mathematics", ...],
     "students": [{"id": "S001", "name": "...", "scores": {"Mathematics": 88, ...}}]}

Cleaning rules
--------------
* Scores must be numeric (int/float or numeric string) and within 0-100.
* Missing (absent key / ``null``) and invalid values are excluded from that
  student's average and reported in the ``issues`` column.
* Students with no name, or with no valid score at all, are excluded from
  rankings but listed in the run report.

Usage
-----
    python student_scores.py
    python student_scores.py --source data/student_scores.json --out-dir output
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Optional

import requests

LOGGER = logging.getLogger("student_scores")

BASE_DIR = Path(__file__).parent
DEFAULT_SOURCE = BASE_DIR / "data" / "student_scores.json"
DEFAULT_OUT_DIR = BASE_DIR / "output"
MIN_SCORE, MAX_SCORE = 0.0, 100.0
HTTP_TIMEOUT = (5.0, 10.0)


class DataSourceError(Exception):
    """Raised when the score data cannot be loaded or has the wrong shape."""


@dataclass
class StudentResult:
    """Processed result for one student."""

    student_id: str
    name: str
    scores: dict[str, float]
    issues: list[str] = field(default_factory=list)

    @property
    def average(self) -> Optional[float]:
        """Mean of valid scores, or ``None`` if the student has none."""
        return round(mean(self.scores.values()), 2) if self.scores else None

    @property
    def is_rankable(self) -> bool:
        """True when the student can take part in highest/lowest ranking."""
        return bool(self.name) and self.average is not None


@dataclass
class Summary:
    """Class-level statistics."""

    overall_average: Optional[float]
    subject_averages: dict[str, Optional[float]]
    highest: list[StudentResult]
    lowest: list[StudentResult]
    excluded: list[StudentResult]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_source(source: str | Path, timeout: tuple[float, float] = HTTP_TIMEOUT) -> dict[str, Any]:
    """Load JSON from a URL (``http``/``https``) or a local file path."""
    src = str(source)
    try:
        if src.startswith(("http://", "https://")):
            response = requests.get(src, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        else:
            payload = json.loads(Path(src).read_text(encoding="utf-8"))
    except requests.Timeout as exc:
        raise DataSourceError(f"Timed out fetching {src}") from exc
    except requests.RequestException as exc:
        raise DataSourceError(f"HTTP error fetching {src}: {exc}") from exc
    except FileNotFoundError as exc:
        raise DataSourceError(f"File not found: {src}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise DataSourceError(f"Source is not valid JSON: {src}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("students"), list):
        raise DataSourceError("JSON must be an object with a 'students' list.")
    return payload


# --------------------------------------------------------------------------- #
# Cleaning and processing
# --------------------------------------------------------------------------- #
def clean_score(value: Any) -> tuple[Optional[float], Optional[str]]:
    """Return ``(score, None)`` when valid, else ``(None, reason)``."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        return None, f"invalid ({value!r})"
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None, f"invalid ({value!r})"
    if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
        return None, f"invalid ({value!r})"
    if not MIN_SCORE <= value <= MAX_SCORE:
        return None, f"out of range ({value:g})"
    return float(value), None


def process_students(payload: dict[str, Any]) -> tuple[list[StudentResult], list[str]]:
    """Validate every student record. Returns ``(results, subjects)``."""
    subjects: list[str] = list(payload.get("subjects") or [])
    if not subjects:  # derive subjects from the data if the API omits them
        seen: dict[str, None] = {}
        for s in payload["students"]:
            for subj in (s.get("scores") or {}):
                seen.setdefault(subj, None)
        subjects = list(seen)

    results: list[StudentResult] = []
    for index, raw in enumerate(payload["students"], start=1):
        if not isinstance(raw, dict):
            LOGGER.warning("Record %d is not an object; skipped.", index)
            continue
        sid = str(raw.get("id") or f"ROW{index}")
        name = " ".join(str(raw.get("name") or "").split())
        raw_scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
        result = StudentResult(student_id=sid, name=name, scores={})
        if not name:
            result.issues.append("missing name")
        for subject in subjects:
            score, problem = clean_score(raw_scores.get(subject))
            if problem:
                result.issues.append(f"{subject}: {problem}")
            else:
                result.scores[subject] = score  # type: ignore[assignment]
        results.append(result)
    return results, subjects


def summarise(results: list[StudentResult], subjects: list[str]) -> Summary:
    """Compute overall/subject averages and top/bottom performers (ties included)."""
    rankable = [r for r in results if r.is_rankable]
    excluded = [r for r in results if not r.is_rankable]
    if not rankable:
        return Summary(None, {s: None for s in subjects}, [], [], excluded)

    # Overall average = mean of all valid individual scores (not mean of means),
    # so students with fewer valid scores are not over-weighted.
    all_scores = [v for r in rankable for v in r.scores.values()]
    subject_avgs: dict[str, Optional[float]] = {}
    for subject in subjects:
        vals = [r.scores[subject] for r in rankable if subject in r.scores]
        subject_avgs[subject] = round(mean(vals), 2) if vals else None

    best = max(r.average for r in rankable)  # type: ignore[type-var]
    worst = min(r.average for r in rankable)  # type: ignore[type-var]
    return Summary(
        overall_average=round(mean(all_scores), 2),
        subject_averages=subject_avgs,
        highest=[r for r in rankable if r.average == best],
        lowest=[r for r in rankable if r.average == worst],
        excluded=excluded,
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_csv(results: list[StudentResult], subjects: list[str], path: Path) -> Path:
    """Write per-student results to CSV (blank cell = missing/invalid score)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["student_id", "name", *subjects, "valid_subjects", "average",
                         "rankable", "issues"])
        for r in results:
            writer.writerow([
                r.student_id, r.name,
                *[("" if s not in r.scores else f"{r.scores[s]:g}") for s in subjects],
                len(r.scores),
                "" if r.average is None else f"{r.average:.2f}",
                "yes" if r.is_rankable else "no",
                "; ".join(r.issues),
            ])
    return path


def plot_averages(results: list[StudentResult], summary: Summary, path: Path) -> Path:
    """Save a bar chart of student averages with an overall-average reference line."""
    import matplotlib
    matplotlib.use("Agg")  # headless backend: works on servers/CI
    import matplotlib.pyplot as plt

    rankable = sorted((r for r in results if r.is_rankable),
                      key=lambda r: r.average, reverse=True)  # type: ignore[arg-type, return-value]
    if not rankable:
        raise ValueError("No rankable students to plot.")
    names = [r.name for r in rankable]
    avgs = [r.average for r in rankable]
    top_ids = {r.student_id for r in summary.highest}
    low_ids = {r.student_id for r in summary.lowest}
    colors = ["#2E7D32" if r.student_id in top_ids else
              "#C62828" if r.student_id in low_ids else "#4C72B0" for r in rankable]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    bars = ax.bar(names, avgs, color=colors)
    ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=9)
    if summary.overall_average is not None:
        ax.axhline(summary.overall_average, color="#555", linestyle="--", linewidth=1)
        ax.text(len(names) - 0.5, summary.overall_average + 1.5,
                f"Overall average {summary.overall_average:.1f}", ha="right",
                fontsize=9, color="#333")
    ax.set_ylim(0, 105)
    ax.set_ylabel("Average score (valid subjects only)")
    ax.set_title("Student average scores (green = highest, red = lowest)")
    ax.spines[["top", "right"]].set_visible(False)
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def format_report(results: list[StudentResult], summary: Summary) -> str:
    """Return a human-readable text report."""
    lines = ["Student averages", "-" * 60]
    for r in sorted(results, key=lambda x: (x.average is None, -(x.average or 0))):
        avg = "n/a" if r.average is None else f"{r.average:6.2f}"
        label = r.name or f"<{r.student_id}: no name>"
        issues = f"  issues: {'; '.join(r.issues)}" if r.issues else ""
        lines.append(f"{r.student_id}  {label:<18} avg={avg}  valid={len(r.scores)}{issues}")
    lines.append("-" * 60)
    lines.append(f"Overall average (all valid scores): {summary.overall_average}")
    lines.append("Subject averages: " + ", ".join(
        f"{s}={'n/a' if v is None else v}" for s, v in summary.subject_averages.items()))
    lines.append("Highest performer(s): " + ", ".join(
        f"{r.name} ({r.average})" for r in summary.highest))
    lines.append("Lowest performer(s): " + ", ".join(
        f"{r.name} ({r.average})" for r in summary.lowest))
    if summary.excluded:
        lines.append("Excluded from ranking: " + ", ".join(
            f"{r.student_id} ({'; '.join(r.issues) or 'no valid scores'})"
            for r in summary.excluded))
    return "\n".join(lines)


def run(source: str | Path = DEFAULT_SOURCE, out_dir: Path = DEFAULT_OUT_DIR,
        make_chart: bool = True) -> tuple[list[StudentResult], Summary]:
    """Full pipeline: load -> clean -> summarise -> CSV -> chart -> report."""
    payload = load_source(source)
    results, subjects = process_students(payload)
    summary = summarise(results, subjects)
    csv_path = write_csv(results, subjects, out_dir / "student_results.csv")
    print(format_report(results, summary))
    print(f"\nCSV written to {csv_path}")
    if make_chart and summary.highest:
        chart = plot_averages(results, summary, out_dir / "student_averages.png")
        print(f"Chart written to {chart}")
    return results, summary


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Student score analysis")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="Local JSON file or http(s) URL returning the same shape")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--no-chart", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        run(args.source, Path(args.out_dir), make_chart=not args.no_chart)
        return 0
    except DataSourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
