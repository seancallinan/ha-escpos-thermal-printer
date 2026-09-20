#!/usr/bin/env python3
"""
Merge the upstream project into this fork, keeping the fork's deleted CI workflows deleted.

This fork dropped every file under ``.github/workflows/`` (commit b5dffd4). Upstream
still maintains them, so each upstream CI change lands here as a modify/delete
conflict: "deleted in HEAD and modified in upstream/main".

Git cannot express "our side deleted it, keep it deleted" declaratively. A
``.gitattributes`` merge driver (``merge=ours``) is only invoked when *both* sides
have content to merge; a deleted path has none, so git skips the driver and reports
a conflict. ``-X ours`` does not help either, for the same reason. The only way to
stop being asked is to resolve those paths after the merge, which is what this does.

Conflicts anywhere else -- and any workflow file that somehow exists on both sides --
are deliberately left alone for a human.

Usage:
    python scripts/merge_upstream.py [--ref upstream/main] [--no-fetch]
"""

from __future__ import annotations

import argparse
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Paths this fork has deleted on purpose and never wants back from upstream.
DELETED_PREFIXES = (".github/workflows/",)


def git(*args: str, check: bool = True) -> str:
    """Run a git command and return stdout."""
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if check and proc.returncode != 0:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
        sys.exit(proc.returncode)
    return proc.stdout


def unmerged_paths(prefix: str) -> list[str]:
    """Conflicted paths under ``prefix``."""
    out = git("diff", "--name-only", "--diff-filter=U", "--", prefix)
    return sorted({line for line in out.splitlines() if line.strip()})


def deleted_on_our_side(path: str) -> bool:
    """True when the conflict has no stage-2 (ours) entry, i.e. we deleted it.

    ``git ls-files -u`` prints one line per unmerged stage: 1=base, 2=ours, 3=theirs.
    """
    for line in git("ls-files", "-u", "--", path).splitlines():
        # "<mode> <sha> <stage>\t<path>"
        fields = line.split("\t", 1)[0].split()
        if len(fields) == 3 and fields[2] == "2":
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref", default="upstream/main", help="ref to merge (default: upstream/main)"
    )
    parser.add_argument("--no-fetch", action="store_true", help="skip fetching first")
    args = parser.parse_args()

    if git("status", "--porcelain").strip():
        print("❌ Working tree is not clean; commit or stash first.", file=sys.stderr)
        return 1

    remote = args.ref.split("/", 1)[0]
    if not args.no_fetch:
        print(f"Fetching {remote}...")
        git("fetch", remote)

    print(f"Merging {args.ref}...")
    merge = subprocess.run(
        ["git", "merge", "--no-edit", args.ref],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    print(merge.stdout, end="")
    if merge.stderr:
        print(merge.stderr, end="", file=sys.stderr)

    if merge.returncode == 0:
        print("✅ Merged with no conflicts.")
        return 0

    kept_deleted = []
    for prefix in DELETED_PREFIXES:
        for path in unmerged_paths(prefix):
            if not deleted_on_our_side(path):
                print(f"⚠️  {path}: modified on both sides — leaving for you to resolve")
                continue
            git("rm", "--quiet", "--force", "--", path)
            kept_deleted.append(path)

    for path in kept_deleted:
        print(f"✅ kept deleted: {path}")

    remaining = [
        line for line in git("diff", "--name-only", "--diff-filter=U").splitlines() if line
    ]
    if remaining:
        print("\nConflicts still needing you:")
        for path in remaining:
            print(f"  - {path}")
        print("\nResolve them, then: git commit")
        return 1

    print("\n✅ All conflicts were auto-resolvable. Review the staged merge, then: git commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
