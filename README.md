# research-ledger

A public, timestamped record of the commit history of two public research repositories at github.com/michaelschiltz: the coding datasets of the HistorEE project (`historee_codebooks`) and the author's working notes (`myfoamrepo`). GitHub shows their history, but a commit's date is set by the author's own machine, and pushed history can be rewritten. This ledger fixes each pushed commit to a Bitcoin-attested time, independently of GitHub and of the author. Anyone can therefore check the order of the work, for instance that expectations were committed before the coding that tests them, and that the history has not been rewritten since.

Maintainer: Michael Schiltz (Hokkaido University).

## What is recorded

Each time either repository is pushed, `anchor.py` writes one entry for each pushed branch head under `entries/`, and one row for it in `ledger.csv`:

```
research-ledger anchor
repo: historee_codebooks
branch: main
commit: <40-character git commit id>
recorded_utc: <time the entry was written>
previous: <SHA-256 of the previous entry, or "genesis">
```

A commit id reveals nothing about the repository's content, but it commits to all of it: every file, and the entire history before that commit. Beside each entry is an [OpenTimestamps](https://opentimestamps.org) proof (`.ots`), which ties the entry's SHA-256 to a Bitcoin block. The proof shows the entry existed **no later than** that block's time. `recorded_utc` is the machine's own clock and proves nothing by itself.

Entries form a hash chain through `previous`. Removing an entry breaks the chain for every later one. Repairing the chain means re-creating those later entries, and their new proofs would carry block times well after the `recorded_utc` they claim.

## Checking it

1. **Each proof.** Use the OpenTimestamps client: `ots verify entries/<year>/<entry>.txt.ots`. Or drop the `.txt` and its `.ots` onto the verifier at opentimestamps.org. A proof that is still *pending* has not yet reached a block; `python3 anchor.py --upgrade` completes it.
2. **The ledger's own consistency.** `python3 anchor.py --check` confirms that every row has its entry, every entry agrees with its row, every proof commits to its entry, and the chain is unbroken.
3. **The repositories.** Every commit anchored here for a branch must be an ancestor of that branch's current history on GitHub: `git merge-base --is-ancestor <commit> <branch>`. Anchoring happens at push, so commits amended or rebased before they were pushed were never anchored and leave no gap. An anchored commit missing from the history means the history was rewritten after the push. Deliberate rewrites are listed in [`REWRITES.md`](REWRITES.md).
4. **A date for a file.** `git log --diff-filter=A -- <file>` names the commit that added the file. The earliest anchored commit descended from it (test descent with `git merge-base --is-ancestor`) dates the file: that entry's proof bounds when the file existed.

## Limits

The ledger proves existence by a date and the integrity of anchored history. It does not prove what the author knew when, beyond what the anchored files contain. It covers only what was pushed: unpushed commits and uncommitted work are not anchored.

Git commit ids are SHA-1. Git has used a collision-detecting SHA-1 since version 2.13, which rejects objects built with the known collision attacks. Forging a different history to fit an id that is already anchored would need a second preimage, which is not feasible for SHA-1.

## Mechanics

`anchor.py` uses only the Python standard library (3.9 or later). It reads a repository's `HEAD` and ref from the `.git` directory, never runs git, and never writes to the anchored repositories. Runs take a lock, so two pushes in quick succession are recorded one after the other. An entry written while offline is stamped on the next run.

A `pre-push` hook in each anchored repository does two things. First, it **refuses a push that would remove commits from the remote history**: a force push, or deleting a remote branch. Second, it runs `anchor.py` in the background for each pushed branch head, so the push is never delayed. The guard can be overridden deliberately, for example to purge a password committed by mistake, with `LEDGER_ALLOW_REWRITE=1 git push …`. Every such override is to be recorded in [`REWRITES.md`](REWRITES.md), so that the gap it leaves in the anchored history is explained rather than discovered. This ledger's own default branch is protected against force pushes on GitHub.
