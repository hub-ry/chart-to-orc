"""Run with python -m evals [--label LABEL]. Failures score; harness errors raise."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from evals.support import Context, ROOT


async def exercise(case, path):
    with Context(path) as ctx:
        await asyncio.wait_for(case(ctx), timeout=20)


def run(cases):
    names = [name for _, name, _ in cases]
    if not cases or len(names) != len(set(names)):
        raise RuntimeError("eval cases must have unique names and must not be empty")
    failed = []
    for category, name, case in cases:
        reason = ""
        # Each case gets its own loop, module globals, patches and temporary files.
        with tempfile.TemporaryDirectory(prefix=".tmp-", dir=ROOT / "evals") as tmp:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    asyncio.run(exercise(case, Path(tmp)))
                except NotImplementedError:
                    reason = "not implemented"
                except AssertionError as exc:
                    reason = " ".join(str(exc).split())[:200] or "outcome assertion failed"
                except TimeoutError:
                    reason = "case exceeded 20s safety limit"
        if reason:
            failed.append(name)
        print(f"{'FAIL' if reason else 'PASS'} [{category}] {name}" + (f": {reason}" if reason else ""))
    return failed


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None, help="optional run label stored in history")
    args = parser.parse_args()
    # Capture before temporary files or this run's history append change the tree.
    commit = git("rev-parse", "--short", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=all"))
    from evals.agent_cases import CASES as agent_cases
    from evals.host_cases import CASES as host_cases

    cases = host_cases + agent_cases
    failed = run(cases)
    total = len(cases)
    passed = total - len(failed)
    score = 100.0 * passed / total
    print(f"\nTotal: {passed}/{total} passed; score: {score:.2f}%")
    record = dict(ts=datetime.now(timezone.utc).isoformat(), commit=commit, dirty=dirty,
                  label=args.label, passed=passed, total=total, score=score, failed=failed)
    with (ROOT / "evals" / "history.jsonl").open("a", encoding="utf-8") as history:
        history.write(json.dumps(record) + "\n")
    # Case failures are measurements, not a CI gate. Unexpected exceptions above
    # (including history-write or setup failures) remain nonzero harness crashes.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
