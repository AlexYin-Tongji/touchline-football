"""Small offline test entry point: fast feedback, focused modules, or full release checks."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAST = (
    "test_agent", "test_agent_delivery", "test_agent_v2", "test_provider", "test_anthropic_provider",
    "test_verification", "test_reader_tools", "test_tool_reliability", "test_model_harness", "test_agent_runtime", "test_agent_delegation",
    "test_runtime_optimization", "test_research_concurrency", "test_team_refresh_parts", "test_report_concurrency",
    "test_skills_tools_quality", "test_news_sources", "test_verification_efficiency",
    "test_workflows", "test_report_workflows",
)


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []

    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append({"test": test.id(), "seconds": round(time.perf_counter() - self.started, 4)})
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("fast", "full"), default="fast")
    parser.add_argument("--module", nargs="+", help="Specific test modules or fully qualified test names")
    parser.add_argument("--json", type=Path, help="Optional timing report path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file() and Path(sys.executable).resolve() != python.resolve():
        return subprocess.call([str(python), "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    # The release suite must never start real background collection. Scheduler
    # behavior is covered with temporary stores and controlled source adapters.
    os.environ["TOUCHLINE_COLLECTION_ENABLED"] = "0"
    # Individual test durations are reported below; asyncio's debug slow-task
    # lines otherwise obscure failures on slower Windows filesystems.
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    loader = unittest.TestLoader()
    if args.module:
        names = [name if name.startswith("tests.") else "tests." + name.removesuffix(".py") for name in args.module]
        suite = loader.loadTestsFromNames(names)
    elif args.suite == "full":
        suite = loader.discover(str(ROOT / "tests"))
    else:
        suite = loader.loadTestsFromNames(["tests." + name for name in FAST])
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, resultclass=TimedResult).run(suite)
    summary = {"suite": "focused" if args.module else args.suite, "tests": result.testsRun,
               "seconds": round(time.perf_counter() - started, 3), "passed": result.wasSuccessful(),
               "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
               "slowest": sorted(result.timings, key=lambda row: row["seconds"], reverse=True)[:10]}
    print(f"\n{summary['suite']}: {summary['tests']} tests, {summary['seconds']:.2f}s")
    for row in summary["slowest"][:5]:
        print(f"  {row['seconds']:6.2f}s  {row['test']}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
