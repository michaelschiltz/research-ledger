#!/usr/bin/env python3
"""Anchor the current commit of private repositories in this public ledger, with OpenTimestamps proofs.

Each run takes a commit of each repository (the one being pushed, when run from the pre-push hook; else
HEAD, read from the .git files without running git), writes one entry file per new commit under entries/, appends a row to ledger.csv, and submits each unstamped entry's
SHA-256 to public OpenTimestamps calendars, writing a standard .ots proof beside it. The proof starts as
"pending" and becomes a Bitcoin attestation within hours; `--upgrade` completes it with the official
client (`ots upgrade`) when that is installed. Nothing in the anchored repositories is read beyond HEAD
and its ref, and nothing in them is written.

    python3 anchor.py                      # anchor the default repositories (see REPOS)
    python3 anchor.py --repo ~/x --label x # anchor one repository's HEAD
    python3 anchor.py --repo ~/x --label x --commit SHA --branch main   # what the pre-push hook runs
    python3 anchor.py --check              # check every entry, row and proof is internally consistent
    python3 anchor.py --upgrade            # complete pending proofs (needs the `ots` client)

Standard library only. A failed network call never loses an entry: the entry is written first and an
unstamped entry is stamped on the next run. Exit status is 0 in --hook mode whatever happens, so a push is
never disturbed; problems go to anchor.log.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRIES = HERE / "entries"
LEDGER = HERE / "ledger.csv"
LOG = HERE / "anchor.log"
LOCK = HERE / ".anchor.lock"
FIELDS = ["recorded_utc", "repo", "branch", "commit", "entry"]

REPOS = [  # label, path: the defaults for a bare `python3 anchor.py`
    ("historee_codebooks", "~/GitHub/HistorEE_codebooks"),
    ("myfoamrepo", "~/GitHub/myfoamrepo"),
]

CALENDARS = [
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
]

# OpenTimestamps detached-proof framing (opentimestamps.core.timestamp.DetachedTimestampFile)
OTS_MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
OTS_VERSION = b"\x01"
OP_SHA256 = b"\x08"
OP_APPEND = b"\xf0"
FORK = b"\xff"


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- reading a repository


def git_dir(repo: Path) -> Path:
    g = repo / ".git"
    if g.is_file():  # worktree or submodule: "gitdir: <path>"
        target = g.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
        g = (repo / target).resolve()
    if not (g / "HEAD").is_file():
        raise FileNotFoundError(f"{repo} is not a git repository")
    return g


def read_head(repo: Path) -> tuple[str, str]:
    """Return (branch, commit) without running git: HEAD, then the loose ref, then packed-refs."""
    g = git_dir(repo)
    head = (g / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return "(detached)", head
    ref = head.removeprefix("ref:").strip()
    branch = ref.removeprefix("refs/heads/")
    common = g
    if (g / "commondir").is_file():  # linked worktrees keep refs in the common dir
        common = (g / (g / "commondir").read_text(encoding="utf-8").strip()).resolve()
    for base in (g, common):
        loose = base / ref
        if loose.is_file():
            return branch, loose.read_text(encoding="utf-8").strip()
    for base in (g, common):
        packed = base / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref) and not line.startswith(("#", "^")):
                    return branch, line.split()[0]
    raise LookupError(f"{repo}: {ref} has no commit yet")


# --------------------------------------------------------------------------- ledger


def ledger_rows() -> list[dict]:
    if not LEDGER.exists():
        return []
    with LEDGER.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def append_row(row: dict) -> None:
    new = not LEDGER.exists()
    with LEDGER.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def previous_digest() -> str:
    """SHA-256 of the last entry file in ledger order, or 'genesis'.

    Chaining makes the ledger's completeness checkable: an entry cannot be dropped without re-creating every
    later entry, and the re-created entries' proofs would carry Bitcoin times visibly later than the
    recorded_utc they claim.
    """
    rows = ledger_rows()
    if not rows:
        return "genesis"
    return hashlib.sha256((HERE / rows[-1]["entry"]).read_bytes()).hexdigest()


def anchor(label: str, repo: Path, commit: str | None = None, branch: str | None = None) -> Path | None:
    """Anchor `commit` (default: the repository's HEAD) for `label`; None if it is already anchored."""
    if commit is None:
        branch, commit = read_head(repo)
    else:
        git_dir(repo)  # still refuse a path that is not a repository
        commit = commit.strip().lower()
        if len(commit) not in (40, 64) or any(c not in "0123456789abcdef" for c in commit):
            raise ValueError(f"not a commit id: {commit!r}")
        branch = branch or "(unnamed)"
    if any(r["repo"] == label and r["commit"] == commit for r in ledger_rows()):
        return None  # already anchored
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    entry = ENTRIES / now.strftime("%Y") / f"{stamp}-{label}-{commit[:12]}.txt"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "research-ledger anchor\n"
        f"repo: {label}\n"
        f"branch: {branch}\n"
        f"commit: {commit}\n"
        f"recorded_utc: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"previous: {previous_digest()}\n",
        encoding="utf-8",
        newline="\n",
    )
    append_row({
        "recorded_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": label,
        "branch": branch,
        "commit": commit,
        "entry": entry.relative_to(HERE).as_posix(),
    })
    log(f"anchored {label} {branch} {commit}")
    return entry


# --------------------------------------------------------------------------- OpenTimestamps


def varbytes(b: bytes) -> bytes:
    n, out = len(b), bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out) + b


def submit(url: str, tip: bytes, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(
        url.rstrip("/") + "/digest",
        data=tip,
        headers={"Accept": "application/vnd.opentimestamps.v1", "User-Agent": "research-ledger-anchor"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        body = resp.read(10001)
    if not body or len(body) > 10000:
        raise RuntimeError("empty or oversized response")
    return body


def merge(responses: list[bytes]) -> bytes:
    """Fork several calendar timestamps at one message: every item but the last gets a 0xff prefix.

    A calendar response is a serialised Timestamp on the submitted digest. One that does not itself start
    with a fork is a single item and can take the prefix; at most one forked response can stand last.
    """
    single = [r for r in responses if not r.startswith(FORK)]
    forked = [r for r in responses if r.startswith(FORK)]
    order = single + forked[:1]
    return b"".join(FORK + r for r in order[:-1]) + order[-1]


def stamp(entry: Path, calendars=CALENDARS) -> bool:
    digest = hashlib.sha256(entry.read_bytes()).digest()
    nonce = os.urandom(16)  # as the official client does: the calendars see only sha256(digest || nonce)
    tip = hashlib.sha256(digest + nonce).digest()
    got = []
    for url in calendars:
        try:
            got.append(submit(url, tip))
        except Exception as exc:  # noqa: BLE001 - any failure of one calendar is survivable
            log(f"calendar {url} failed for {entry.name}: {exc}")
    if not got:
        return False
    proof = OTS_MAGIC + OTS_VERSION + OP_SHA256 + digest + OP_APPEND + varbytes(nonce) + OP_SHA256 + merge(got)
    entry.with_name(entry.name + ".ots").write_bytes(proof)
    log(f"stamped {entry.name} via {len(got)} of {len(calendars)} calendars")
    return True


def stamp_pending() -> tuple[int, int]:
    done = failed = 0
    for entry in sorted(ENTRIES.rglob("*.txt")):
        if entry.with_name(entry.name + ".ots").exists():
            continue
        if stamp(entry):
            done += 1
        else:
            failed += 1
    return done, failed


def find_ots() -> str | None:
    for cand in (shutil.which("ots"), *(str(Path(p).expanduser()) for p in (
        "~/.local/bin/ots", "/opt/homebrew/bin/ots", "/usr/local/bin/ots",
    ))):
        if cand and Path(cand).is_file():
            return cand
    return None


# --------------------------------------------------------------------------- checks


def check() -> int:
    problems = 0
    rows = ledger_rows()
    seen = set()
    expected_prev = "genesis"
    last_time = ""
    for r in rows:
        p = HERE / r["entry"]
        if not p.is_file():
            print(f"row without entry file: {r['entry']}")
            problems += 1
            expected_prev = "(unknown: missing entry)"
            continue
        seen.add(p.resolve())
        text = p.read_text(encoding="utf-8")
        if f"previous: {expected_prev}\n" not in text:
            print(f"{r['entry']}: chain broken (previous is not the hash of the entry before it)")
            problems += 1
        expected_prev = hashlib.sha256(p.read_bytes()).hexdigest()
        if r["recorded_utc"] < last_time:
            print(f"{r['entry']}: recorded earlier than the row before it")
            problems += 1
        last_time = r["recorded_utc"]
        for key in ("repo", "branch", "commit", "recorded_utc"):
            if f"{key}: {r[key]}\n" not in text:
                print(f"{r['entry']}: {key} disagrees with ledger.csv")
                problems += 1
        ots = p.with_name(p.name + ".ots")
        if not ots.is_file():
            print(f"{r['entry']}: no proof yet (run anchor.py again with network)")
            problems += 1
            continue
        b = ots.read_bytes()
        head = OTS_MAGIC + OTS_VERSION + OP_SHA256
        if not b.startswith(head) or b[len(head):len(head) + 32] != hashlib.sha256(p.read_bytes()).digest():
            print(f"{ots.name}: does not commit to its entry file")
            problems += 1
    for p in ENTRIES.rglob("*.txt"):
        if p.resolve() not in seen:
            print(f"entry file with no ledger row: {p.relative_to(HERE)}")
            problems += 1
    print(f"{len(rows)} rows; {problems} problem(s)")
    return 1 if problems else 0


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", action="append", help="repository to anchor (repeatable; default: REPOS)")
    ap.add_argument("--label", action="append", help="public label for each --repo, in order")
    ap.add_argument("--commit", help="anchor this commit instead of HEAD (with a single --repo; the pre-push hook)")
    ap.add_argument("--branch", help="branch name recorded with --commit")
    ap.add_argument("--hook", action="store_true", help="never fail: for use from the pre-push hook")
    ap.add_argument("--check", action="store_true", help="check entries, rows and proofs; anchor nothing")
    ap.add_argument("--upgrade", action="store_true", help="complete pending proofs with the `ots` client")
    ap.add_argument("--no-stamp", action="store_true", help="write entries only (testing)")
    args = ap.parse_args()

    if args.check:
        return check()
    if args.upgrade:
        ots = find_ots()
        if not ots:
            print("the OpenTimestamps client is not installed: pipx install opentimestamps-client")
            return 1
        proofs = [str(p) for p in sorted(ENTRIES.rglob("*.ots"))]
        return subprocess.call([ots, "upgrade", *proofs]) if proofs else 0

    try:
        if args.repo:
            labels = args.label or []
            if len(labels) != len(args.repo):
                ap.error("give one --label per --repo")
            targets = list(zip(labels, args.repo))
        else:
            targets = REPOS
        if args.commit and len(targets) != 1:
            ap.error("--commit needs exactly one --repo")
        # One run at a time: two quick pushes start two background runs, and without the lock they read the
        # same last entry, chain to it twice and race on ledger.csv (seen in testing, 2026-10-23).
        with LOCK.open("a") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            for label, path in targets:
                try:
                    anchor(label, Path(path).expanduser(), args.commit, args.branch)
                except (FileNotFoundError, LookupError, ValueError) as exc:
                    log(f"skipped {label}: {exc}")
            if not args.no_stamp:
                done, failed = stamp_pending()
                if failed:
                    log(f"{failed} entr{'y' if failed == 1 else 'ies'} still unstamped; they will be stamped on the next run")
    except Exception as exc:  # noqa: BLE001
        log(f"error: {exc!r}")
        return 0 if args.hook else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
