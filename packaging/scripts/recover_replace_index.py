"""Recover the supplied replacement index into a strict release CSV."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path


HEADER = ("source_tag", "canonical_e621_tag", "action", "replacement_tags")
HEADER_BYTES = tuple(value.encode("ascii") for value in HEADER)
VALID_ACTIONS = (b"keep", b"replace", b"drop")
BASELINE_SHA256 = "b13116faa638694592632083d32a89f4ae6c3a5b92d604f89d056dcf48bb329a"
SOURCE_SHA256 = "5e04fe357a54e31ea7e307718f3e9c6022212d7e17fbcbf6d1e33206b7ec746b"
EXPECTED_COUNTS = {"keep": 56_426, "replace": 11_600, "drop": 18_896}
EXPECTED_PIPE_REPLACEMENTS = 2_151
EXPECTED_KEEP_NON_CANONICAL = 269
OMITTED_BASELINE_ROWS = (
    ("rating:s", "rating:s", "drop", ""),
    ("rating:q", "rating:q", "drop", ""),
    ("rating:e", "rating:e", "drop", ""),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_rows(path: Path) -> list[tuple[str, str, str, str]]:
    if sha256(path) != BASELINE_SHA256:
        raise ValueError("baseline replacement index digest is invalid")
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.reader(source)
        if tuple(next(reader, ())) != HEADER:
            raise ValueError("baseline replacement index header is invalid")
        rows = [tuple(row) for row in reader]
    if len(rows) != 86_925 or tuple(rows[-3:]) != OMITTED_BASELINE_ROWS:
        raise ValueError("baseline replacement index rows are not the audited release")
    if any(len(row) != len(HEADER) for row in rows):
        raise ValueError("baseline replacement index contains a malformed row")
    return rows[:-3]


def _damaged_rows(path: Path) -> list[tuple[bytes, bytes, bytes, bytes]]:
    if sha256(path) != SOURCE_SHA256:
        raise ValueError("supplied replacement index digest is invalid")
    raw = path.read_bytes()
    reader = csv.reader(io.StringIO(raw.decode("latin-1"), newline=""))
    header = tuple(value.encode("latin-1") for value in next(reader, ()))
    if header != HEADER_BYTES:
        raise ValueError("supplied replacement index header is invalid")
    rows: list[tuple[bytes, bytes, bytes, bytes]] = []
    for line_number, row in enumerate(reader, start=2):
        if len(row) != len(HEADER):
            raise ValueError(f"supplied replacement index row {line_number} has the wrong column count")
        rows.append(tuple(value.encode("latin-1") for value in row))
    if len(rows) != 86_922:
        raise ValueError("supplied replacement index row count is invalid")
    return rows


def _recover_action(row: tuple[bytes, bytes, bytes, bytes], line_number: int) -> tuple[bytes, bytes, int]:
    if row[2] in VALID_ACTIONS:
        return row[2], row[3], 2
    candidates: list[tuple[bytes, bytes, int]] = []
    for column in (1, 0):
        for action in VALID_ACTIONS:
            if row[column] == action or row[column].endswith(action):
                output = row[column + 1]
                if (action == b"drop" and not output) or (action != b"drop" and output):
                    candidates.append((action, output, column))
    if len(candidates) != 1:
        raise ValueError(f"supplied replacement index action at row {line_number} is ambiguous")
    return candidates[0]


def _validate_rule(source: str, action: str, replacement: str, seen: set[str], line_number: int) -> None:
    if not source or source in seen:
        raise ValueError(f"recovered replacement index source at row {line_number} is empty or duplicated")
    seen.add(source)
    if action == "drop":
        if replacement:
            raise ValueError(f"recovered drop rule at row {line_number} has output")
        return
    if not replacement or replacement != replacement.strip():
        raise ValueError(f"recovered non-drop rule at row {line_number} has invalid output")
    if action == "keep":
        if any(character in replacement for character in ",\r\n\x00"):
            raise ValueError(f"recovered keep rule at row {line_number} has an invalid tag")
        return
    parts = replacement.split("|")
    if any(not part or part != part.strip() or any(character in part for character in ",\r\n\x00") for part in parts):
        raise ValueError(f"recovered replace rule at row {line_number} has an invalid tag")


def recover(source_path: Path, baseline_path: Path, output_path: Path) -> dict[str, object]:
    baseline = _baseline_rows(baseline_path)
    damaged = _damaged_rows(source_path)
    if output_path.exists():
        raise ValueError("recovered replacement index output already exists")

    counts: Counter[str] = Counter()
    seen: set[str] = set()
    rows: list[tuple[str, str, str, str]] = []
    canonical: dict[str, str] = {}
    keep_outputs: dict[str, str] = {}
    for line_number, (old, supplied) in enumerate(zip(baseline, damaged, strict=True), start=2):
        source_tag, canonical_tag, old_action, old_output = old
        counts["sourceRestored"] += supplied[0] != source_tag.encode("utf-8")
        counts["canonicalRestored"] += supplied[1] != canonical_tag.encode("utf-8")
        action_bytes, output_bytes, action_column = _recover_action(supplied, line_number)
        counts[f"actionColumn{action_column}"] += 1
        action = action_bytes.decode("ascii")
        try:
            output = output_bytes.decode("utf-8")
        except UnicodeDecodeError:
            if action != old_action:
                raise ValueError(f"damaged output at row {line_number} cannot use the baseline action")
            output = old_output
            counts["outputRestored"] += 1
        _validate_rule(source_tag, action, output, seen, line_number)
        counts[action] += 1
        counts["pipeReplacement"] += action == "replace" and "|" in output
        counts["literalKeepPipe"] += source_tag == ":|" and action == "keep" and output == ":|"
        rows.append((source_tag, canonical_tag, action, output))
        canonical[source_tag] = canonical_tag
        if action == "keep":
            keep_outputs[source_tag] = output

    keep_non_canonical = sum(
        1 for source_tag, output in keep_outputs.items()
        if canonical.get(source_tag) and output != canonical[source_tag]
    )
    direction_conflicts = sum(
        1 for target in canonical.values()
        if target in canonical and canonical[target] != target
    )
    if (
        len(rows) != 86_922
        or {action: counts[action] for action in ("keep", "replace", "drop")} != EXPECTED_COUNTS
        or counts["pipeReplacement"] != EXPECTED_PIPE_REPLACEMENTS
        or counts["literalKeepPipe"] != 1
        or keep_non_canonical != EXPECTED_KEEP_NON_CANONICAL
        or direction_conflicts != 0
    ):
        raise ValueError("recovered replacement index statistics do not match the audited result")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\r\n")
            writer.writerow(HEADER)
            writer.writerows(rows)
        with output_path.open("r", encoding="utf-8", newline="") as check:
            if sum(1 for _ in csv.reader(check)) != 86_923:
                raise ValueError("recovered replacement index write verification failed")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise

    return {
        "path": str(output_path.resolve()),
        "sizeBytes": output_path.stat().st_size,
        "sha256": sha256(output_path),
        "rowCount": len(rows),
        "actionCounts": {action: counts[action] for action in ("keep", "replace", "drop")},
        "pipeReplacementCount": counts["pipeReplacement"],
        "literalKeepPipeCount": counts["literalKeepPipe"],
        "keepNonCanonical": keep_non_canonical,
        "canonicalDirectionConflict": direction_conflicts,
        "repairCounts": {
            "sourceRestored": counts["sourceRestored"],
            "canonicalRestored": counts["canonicalRestored"],
            "actionRecovered": counts["actionColumn0"] + counts["actionColumn1"],
            "outputRestored": counts["outputRestored"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(recover(arguments.source, arguments.baseline, arguments.output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
