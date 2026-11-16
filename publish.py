#!/usr/bin/env python3
"""Commit and push this ledger's own new entries and completed proofs.

The pre-push hook of each anchored repository runs this after anchor.py, in the background, so that the
public ledger follows every push instead of waiting for a commit by hand. Only ledger.csv and entries/ are
committed: anything else that is modified or staged in this repository is left exactly as it is.

    python3 publish.py             # commit what is new, then push
    python3 publish.py --no-push   # commit only
    python3 publish.py --hook      # never fail (the pre-push hook)

It takes the same lock as anchor.py, so it never commits an entry whose proof is still being written. A
push that fails (offline, or the remote has commits this clone lacks) is logged to anchor.log and tried
again on the next run. Standard library only; runs git in this repository and nowhere else.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import anchor

HERE = Path(__file__).resolve().parent
PATHS = ["ledger.csv", "entries"]
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # in the background there is nobody to answer a prompt


def git(*args: str, check: bool = True, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(HERE), *args], env=ENV, check=check, timeout=timeout,
                          capture_output=True, text=True)


def staged() -> tuple[list[str], list[str]]:
    """(entries added, proofs added or changed) among what is staged under PATHS."""
    out = git("diff", "--cached", "--name-status", "--", *PATHS).stdout
    entries, proofs = [], []
    for line in out.splitlines():
        status, _, name = line.partition("\t")
        if name.endswith(".txt") and status.startswith("A"):
            entries.append(name)
        elif name.endswith(".txt.ots"):
            proofs.append(name)
    return entries, proofs


def message(entries: list[str], proofs: list[str]) -> str:
    completed = [p for p in proofs if p[: -len(".ots")] not in entries]
    parts = []
    if len(entries) == 1:
        # entries/2026/20261116T090145Z-historee_codebooks-8aeee479fe0a.txt
        stem = Path(entries[0]).stem
        _, _, rest = stem.partition("-")
        repo, _, commit = rest.rpartition("-")
        parts.append(f"{repo} {commit}")
    elif entries:
        parts.append(f"{len(entries)} entries")
    if completed:
        parts.append(f"{len(completed)} proof{'s' if len(completed) != 1 else ''} completed")
    return "ledger: " + "; ".join(parts or ["update"])


def publish(push: bool = True) -> bool:
    """Commit and push. True if the ledger on the remote is up to date afterwards (or push was not asked)."""
    with anchor.LOCK.open("a") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        git("add", "--", *PATHS)
        if git("diff", "--cached", "--quiet", "--", *PATHS, check=False).returncode != 0:
            msg = message(*staged())
            git("commit", "-q", "-m", msg, "--", *PATHS)
            anchor.log(f"committed: {msg}")
        if not push:
            return True
        ahead = git("rev-list", "--count", "@{u}..HEAD", check=False)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            return True
        res = git("push", "-q", check=False, timeout=180.0)
        if res.returncode != 0:
            anchor.log("push of the ledger failed; it is tried again on the next run: "
                       + (res.stderr.strip().splitlines() or ["(no message)"])[-1])
            return False
        anchor.log("pushed the ledger")
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-push", action="store_true", help="commit only")
    ap.add_argument("--hook", action="store_true", help="never fail: for use from the pre-push hook")
    args = ap.parse_args()
    try:
        ok = publish(push=not args.no_push)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "stderr", "") or ""
        anchor.log(f"publish error: {exc!r} {detail.strip()}")
        ok = False
    return 0 if (ok or args.hook) else 1


if __name__ == "__main__":
    sys.exit(main())
