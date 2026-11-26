"""Unified CLI E2E Test Runner for iso_engine (MVP + 4-Tier E2E Suite).

Supports:
  --tier <1|2|3|4>     Run specific E2E tier(s); repeatable or comma-separated
  --mvp-only           Run only tests/test_mvp_engine.py
  --all                Run test_mvp_engine.py + Tiers 1-4 (default)
  -v / --verbose       Verbose unittest output (verbosity=2)
  --json-report <path> Write machine-readable JSON summary report

Performs global pre/post isolation verification on rp/webui/ and rplab.db
using os.stat + sha256 + read-only immutable SQLite inspection (when -wal == 0).
Exits 0 iff all selected tests pass and system isolation holds.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
import unittest
from typing import Any


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ISO_ENGINE_DIR = os.path.dirname(TESTS_DIR)
RP_DIR = os.path.dirname(ISO_ENGINE_DIR)
WEBUI_DIR = os.path.join(RP_DIR, "webui")
RPLAB_DB_PATH = os.path.join(WEBUI_DIR, "data", "rplab.db")
RPLAB_WAL_PATH = os.path.join(WEBUI_DIR, "data", "rplab.db-wal")

TIER_CLASS_MAP = {
    1: "TestTier1FeatureCoverage",
    2: "TestTier2BoundaryAndNegative",
    3: "TestTier3CrossFeaturePairwise",
    4: "TestTier4RealWorldScenarios",
}


def _reconfigure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def capture_isolation_snapshot() -> dict[str, Any]:
    """Capture non-mutating snapshot of rp/webui/ and rplab.db."""
    files: dict[str, tuple[int, int]] = {}
    hashes: dict[str, str] = {}
    if os.path.isdir(WEBUI_DIR):
        for root, _, filenames in os.walk(WEBUI_DIR):
            for fn in sorted(filenames):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, WEBUI_DIR).replace("\\", "/")
                st = os.stat(full)
                files[rel] = (st.st_size, st.st_mtime_ns)
                if fn.endswith((".py", ".html", ".json", ".bat")) or fn == "rplab.db":
                    hashes[rel] = _sha256_file(full)

    row_counts: dict[str, int] | None = None
    if os.path.isfile(RPLAB_DB_PATH):
        wal_size = os.path.getsize(RPLAB_WAL_PATH) if os.path.isfile(RPLAB_WAL_PATH) else 0
        if wal_size == 0:
            conn = sqlite3.connect(
                f"file:{RPLAB_DB_PATH}?mode=ro&immutable=1",
                uri=True,
                timeout=2.0,
            )
            try:
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                ]
                row_counts = {}
                for tbl in sorted(tables):
                    row_counts[tbl] = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            finally:
                conn.close()

    return {
        "webui_exists": os.path.isdir(WEBUI_DIR),
        "file_count": len(files),
        "files": files,
        "hashes": hashes,
        "row_counts": row_counts,
    }


def compare_isolation_snapshots(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Compare pre/post snapshots and return (ok, details)."""
    added = sorted(set(after["files"]) - set(before["files"]))
    removed = sorted(set(before["files"]) - set(after["files"]))
    modified_stat = sorted(
        k for k in set(before["files"]) & set(after["files"]) if before["files"][k] != after["files"][k]
    )
    modified_hash = sorted(
        k for k in set(before["hashes"]) & set(after["hashes"]) if before["hashes"][k] != after["hashes"][k]
    )
    row_count_mismatch = before["row_counts"] != after["row_counts"]
    ok = not (added or removed or modified_stat or modified_hash or row_count_mismatch)
    return ok, {
        "webui_exists": after["webui_exists"],
        "file_count_before": before["file_count"],
        "file_count_after": after["file_count"],
        "added_files": added,
        "removed_files": removed,
        "modified_stat": modified_stat,
        "modified_hash": modified_hash,
        "row_count_mismatch": row_count_mismatch,
    }


def _load_module_from_path(mod_name: str, file_path: str) -> Any:
    if ISO_ENGINE_DIR not in sys.path:
        sys.path.insert(0, ISO_ENGINE_DIR)
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load test module: {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _parse_tiers(raw_tiers: list[str] | None) -> list[int]:
    if raw_tiers is None:
        return []
    out: list[int] = []
    for item in raw_tiers:
        if not item or not item.strip():
            raise ValueError("Empty --tier argument; expected 1..4")
        for part in item.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"Empty tier entry in {item!r}; expected 1..4")
            val = int(part)
            if val not in TIER_CLASS_MAP:
                raise ValueError(f"Invalid tier {val}; expected 1..4")
            if val not in out:
                out.append(val)
    if not out:
        raise ValueError("No valid tiers specified; expected 1..4")
    return sorted(out)


def build_test_suite(run_mvp: bool, tiers: list[int]) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()

    if run_mvp:
        mvp_path = os.path.join(TESTS_DIR, "test_mvp_engine.py")
        if not os.path.isfile(mvp_path):
            raise FileNotFoundError(f"Missing MVP test file: {mvp_path}")
        mvp_mod = _load_module_from_path("test_mvp_engine", mvp_path)
        suite.addTests(loader.loadTestsFromModule(mvp_mod))

    if tiers:
        tiers_path = os.path.join(TESTS_DIR, "test_e2e_tiers.py")
        tiers_mod = _load_module_from_path("test_e2e_tiers", tiers_path)
        for t in tiers:
            cls = getattr(tiers_mod, TIER_CLASS_MAP[t])
            suite.addTests(loader.loadTestsFromTestCase(cls))

    return suite


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="iso_engine E2E Test Suite Runner")
    parser.add_argument(
        "--tier",
        action="append",
        help="Run specific E2E tier(s) 1..4 (repeatable or comma-separated, e.g. --tier 1,3)",
    )
    parser.add_argument(
        "--mvp-only",
        action="store_true",
        help="Run only tests/test_mvp_engine.py",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run test_mvp_engine.py + all 4 E2E tiers (default if no selection flag given)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose unittest output",
    )
    parser.add_argument(
        "--json-report",
        metavar="PATH",
        help="Write structured JSON test & isolation report to PATH",
    )
    return parser.parse_args(argv)


def _base_test_id(test_obj: Any) -> str:
    parent = getattr(test_obj, "test_case", None)
    if parent is not None and hasattr(parent, "id"):
        return str(parent.id())
    return str(test_obj.id()) if hasattr(test_obj, "id") else repr(test_obj)


def main(argv: list[str] | None = None) -> int:
    _reconfigure_utf8()
    args = parse_args(argv)

    try:
        selected_tiers = _parse_tiers(args.tier)
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        return 2

    if args.mvp_only and selected_tiers and not args.all:
        print("Argument error: --mvp-only cannot be combined with --tier unless --all is used", file=sys.stderr)
        return 2

    if args.all or (not args.mvp_only and not selected_tiers):
        run_mvp = True
        tiers_to_run = [1, 2, 3, 4]
    elif args.mvp_only:
        run_mvp = True
        tiers_to_run = []
    else:
        run_mvp = False
        tiers_to_run = selected_tiers

    before_snap = capture_isolation_snapshot()
    t0 = time.monotonic()

    try:
        suite = build_test_suite(run_mvp=run_mvp, tiers=tiers_to_run)
        verbosity = 2 if args.verbose else 1
        runner = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stdout)
        result = runner.run(suite)
        tests_run = result.testsRun
        failures = len(result.failures)
        errors = len(result.errors)
        skipped = len(result.skipped)
        failed_method_ids = {_base_test_id(t) for t, _ in result.failures} | {
            _base_test_id(t) for t, _ in result.errors
        }
        skipped_method_ids = {_base_test_id(t) for t, _ in result.skipped}
        passed = max(0, tests_run - len(failed_method_ids | skipped_method_ids))
        methods_failed = len(failed_method_ids)
        subtest_failures = sum(1 for t, _ in result.failures if getattr(t, "test_case", None) is not None)
        subtest_errors = sum(1 for t, _ in result.errors if getattr(t, "test_case", None) is not None)
        tests_ok = result.wasSuccessful()
    except Exception as exc:
        print(f"Runner error: {exc}", file=sys.stderr)
        tests_run = 0
        passed = 0
        methods_failed = 0
        failures = 0
        errors = 1
        skipped = 0
        subtest_failures = 0
        subtest_errors = 0
        tests_ok = False

    duration_s = round(time.monotonic() - t0, 3)
    after_snap = capture_isolation_snapshot()
    isolation_ok, isolation_details = compare_isolation_snapshots(before_snap, after_snap)

    if not isolation_ok:
        print(f"ISOLATION VIOLATION: {json.dumps(isolation_details, ensure_ascii=False)}", file=sys.stderr)
    else:
        print(
            f"[Isolation OK] rp/webui/ ({isolation_details['file_count_after']} files) & rplab.db untouched.",
            file=sys.stdout,
        )

    report = {
        "selected": {"mvp": run_mvp, "tiers": tiers_to_run},
        "tests_run": tests_run,
        "methods_run": tests_run,
        "passed": passed,
        "methods_passed": passed,
        "methods_failed": methods_failed,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "subtest_failures": subtest_failures,
        "subtest_errors": subtest_errors,
        "duration_s": duration_s,
        "tests_ok": tests_ok,
        "isolation_ok": isolation_ok,
        "isolation_details": isolation_details,
    }

    if args.json_report:
        report_dir = os.path.dirname(os.path.abspath(args.json_report))
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)
        with open(args.json_report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    return 0 if (tests_ok and isolation_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
