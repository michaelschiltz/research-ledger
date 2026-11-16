#!/usr/bin/env python3
"""Cut a release of the ledger: the snapshot that Zenodo archives.

    python3 release.py              # tag = today's date in UTC, as YYYY.MM.DD
    python3 release.py --tag 2026.11.16
    python3 release.py --dry-run    # report what would happen; change nothing

In order: complete every pending proof; run the ledger's own check; refuse if a proof older than
MAX_PENDING_HOURS is still pending (a deposit should not archive a proof that depends on the calendar
servers when it need not); set version and date-released in CITATION.cff; commit ledger.csv, entries/ and
CITATION.cff; tag; push the branch and the tag; publish the GitHub release with `gh` if it is installed,
else print the page on which to publish it. Zenodo archives the release once it is published.

Refuses to start if any tracked file other than ledger.csv, entries/ and CITATION.cff has uncommitted
changes, so that a release never sweeps up work in progress. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import re
import shutil
import subprocess
import sys
from pathlib import Path

import anchor
from publish import git

HERE = Path(__file__).resolve().parent
CITATION = HERE / "CITATION.cff"
OWN = ("ledger.csv", "entries/", "CITATION.cff")
MAX_PENDING_HOURS = 24
RELEASES = "https://github.com/michaelschiltz/research-ledger/releases"


def stop(msg: str) -> int:
    print(f"release stopped: {msg}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", help="release tag (default: today's UTC date, YYYY.MM.DD)")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    args = ap.parse_args()
    today = dt.datetime.now(dt.timezone.utc).date()
    tag = args.tag or today.strftime("%Y.%m.%d")

    if git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
        return stop(f"tag {tag} exists already; give another with --tag (for a second release today, {tag}.1)")
    dirty = [line[3:] for line in git("status", "--porcelain").stdout.splitlines()
             if not line[3:].startswith(OWN)]
    if dirty:
        return stop("uncommitted changes outside the ledger's own files: " + ", ".join(dirty))
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    with anchor.LOCK.open("a") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        if args.dry_run:
            print(f"dry run: would try to complete {len(anchor.pending_proofs())} pending proof(s)")
        else:
            if anchor.pending_proofs() and not anchor.find_ots():
                return stop("the OpenTimestamps client is not installed: pipx install opentimestamps-client")
            anchor.upgrade()
        if anchor.check() != 0:
            return stop("anchor.py --check reports problems")
        old = anchor.pending_proofs(MAX_PENDING_HOURS)
        if old and not args.dry_run:
            return stop(f"{len(old)} proof(s) older than {MAX_PENDING_HOURS} hours still pending, the first "
                        f"{old[0].name}; run again later, or look at anchor.log")
        proofs = sorted(anchor.ENTRIES.rglob("*.txt.ots"))
        pending = len(anchor.pending_proofs())
        rows = len(anchor.ledger_rows())
        summary = f"{rows} entries, {len(proofs) - pending} proofs completed (Bitcoin-attested), " + (
            f"{pending} pending (from the last hours before the release)" if pending else "none pending")
        if args.dry_run:
            print(f"dry run: would release {tag} from {branch}: {summary}")
            if old:
                print(f"dry run: but {len(old)} proof(s) older than {MAX_PENDING_HOURS} hours are pending, which stops a release unless the upgrade completes them")
            return 0

        text = CITATION.read_text(encoding="utf-8")
        text, n1 = re.subn(r"(?m)^version:.*$", f'version: "{tag}"', text)
        text, n2 = re.subn(r"(?m)^date-released:.*$", f"date-released: {today.isoformat()}", text)
        if n1 != 1 or n2 != 1:
            return stop("CITATION.cff has no single version and date-released line to set")
        CITATION.write_text(text, encoding="utf-8", newline="\n")

        git("add", "--", "ledger.csv", "entries", "CITATION.cff")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            git("commit", "-q", "-m", f"release {tag}: {summary}", "--", "ledger.csv", "entries", "CITATION.cff")
        git("tag", "-a", tag, "-m", f"research-ledger {tag}: {summary}")
        res = git("push", "-q", "origin", branch, tag, check=False, timeout=180.0)
        if res.returncode != 0:
            return stop("committed and tagged locally, but the push failed: " + res.stderr.strip())
    anchor.log(f"released {tag}: {summary}")

    notes = (f"Snapshot of the ledger at {tag}. {summary}.\n\n"
             "Check it with `python3 anchor.py --check`; verify a proof with `ots verify entries/<year>/<entry>.txt.ots`.")
    gh = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if Path(p).is_file()), None)
    if gh:
        res = subprocess.run([gh, "release", "create", tag, "--title", f"research-ledger {tag}", "--notes", notes,
                              "--verify-tag"], cwd=HERE, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"released {tag}: {res.stdout.strip()}")
            return 0
        print(f"tag {tag} is pushed, but `gh release create` failed: {res.stderr.strip()}")
    print(f"tag {tag} is pushed. Publish the release at {RELEASES}/new?tag={tag}")
    print("Suggested notes:\n" + notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
