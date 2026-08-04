#!/usr/bin/env python3
"""Every upstream thread we have a stake in, and who it is waiting on.

A hand-written ledger of open issues and PRs drifts. Ours did, twice: it listed two PRs as
ours that belonged to another contributor, and it went a full day without noticing that a
maintainer's branch had started conflicting with one of our open PRs. Both were failures of
recall, not of access, so this asks GitHub instead of remembering.

    python3 tools/upstream_status.py
    python3 tools/upstream_status.py --conflicts        # also diff changed files across open PRs

Three questions it answers:

  * What is open, and what happened to it last.
  * Is it waiting on us or on them? A thread whose last comment is somebody else's is waiting
    on us; one whose last comment is ours is waiting on them. That is a blunt rule and it is
    stated as such in the output, because "waiting on them" is exactly the state that quietly
    becomes "we forgot".
  * Do any of our open PRs touch files another open PR also touches? That is the check that
    was missed: a maintainer refactor can start conflicting with our work without anyone
    commenting on our thread at all, so nothing shows up in a query about our threads.

Read-only. It issues GET requests through `gh api` and never writes anything.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

DEFAULT_USER = "m4bard"
DEFAULT_REPOS = ("Listenarrs/Listenarr", "m4bard/listenarr-testdata")


class GhError(RuntimeError):
    """A gh call failed. Never swallowed: a partial ledger is worse than no ledger."""


def run_gh(args: list[str]) -> Any:
    """Call `gh` and parse its JSON. Fails closed.

    An empty or malformed answer is raised rather than returned. This tool exists to be
    trusted, and a ledger that silently reports "nothing open" because a token expired would
    be the most damaging possible output.
    """
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    if not proc.stdout.strip():
        raise GhError(f"gh {' '.join(args)} returned nothing")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh {' '.join(args)} returned non-JSON: {exc}") from exc


@dataclass
class Thread:
    """One issue or PR we authored or commented on."""

    repo: str
    number: int
    title: str
    state: str
    is_pr: bool
    ours: bool
    updated_at: str
    last_comment_by: str | None = None
    last_comment_at: str | None = None
    last_mentions_us: bool = False
    comment_count: int = 0

    @property
    def kind(self) -> str:
        return "PR " if self.is_pr else "iss"

    def waiting_on(self, user: str) -> str:
        """Who owes the next move.

        On a thread we authored, somebody else speaking last means the ball is ours. On a
        thread we merely commented on, it is not: most activity there is the author and the
        maintainer talking to each other, and counting that as our turn produces a flag that
        fires constantly and therefore gets ignored. The first version of this rule did
        exactly that, calling three threads ours when one was a bystander saying the PR looked
        useful and two were the maintainer addressing the PR's author. So on other people's
        threads only an explicit @mention counts.
        """
        if self.state.upper() != "OPEN":
            return "closed"
        if self.last_comment_by is None:
            return "them" if self.ours else "-"
        if self.last_comment_by == user:
            return "them"
        if self.ours or self.last_mentions_us:
            return "US"
        return "-"


def search_threads(repo: str, user: str, qualifier: str) -> list[dict[str, Any]]:
    """Search one repo for issues/PRs matching `author:` or `commenter:`."""
    query = f"repo:{repo}+{qualifier}:{user}"
    payload = run_gh(["api", f"search/issues?q={query}&per_page=100"])
    items = payload.get("items")
    if items is None:
        raise GhError(f"search for {query} returned no items field")
    return list(items)


def last_comment(repo: str, number: int, user: str) -> tuple[str | None, str | None, bool, int]:
    """Who commented last, when, and whether that comment @mentions us.

    Returns (None, None, False, 0) when nobody has commented.
    """
    comments = run_gh(
        ["api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate"]
    )
    if not isinstance(comments, list) or not comments:
        return None, None, False, 0
    tail = comments[-1]
    mentions = f"@{user}".lower() in (tail.get("body") or "").lower()
    return tail["user"]["login"], tail["created_at"], mentions, len(comments)


def collect(repos: tuple[str, ...], user: str) -> list[Thread]:
    """Every thread we authored or commented on, deduplicated, newest activity first."""
    seen: dict[tuple[str, int], Thread] = {}
    for repo in repos:
        for qualifier in ("author", "commenter"):
            authored = qualifier == "author"
            for item in search_threads(repo, user, qualifier):
                key = (repo, int(item["number"]))
                if key in seen:
                    # A thread we both authored and commented on arrives twice. OR rather than
                    # assignment so the mark does not depend on which search ran first: whether
                    # it is ours is a property of the thread, not of the order we found it in.
                    seen[key].ours = seen[key].ours or authored
                    continue
                seen[key] = Thread(
                    repo=repo,
                    number=int(item["number"]),
                    title=item["title"],
                    state=item["state"],
                    is_pr="pull_request" in item,
                    ours=authored,
                    updated_at=item["updated_at"],
                )
    for thread in seen.values():
        if thread.state.upper() == "OPEN":
            who, when, mentions, count = last_comment(thread.repo, thread.number, user)
            thread.last_comment_by = who
            thread.last_comment_at = when
            thread.last_mentions_us = mentions
            thread.comment_count = count
    return sorted(seen.values(), key=lambda t: t.updated_at, reverse=True)


def changed_files(repo: str, number: int) -> set[str]:
    files = run_gh(["api", f"repos/{repo}/pulls/{number}/files?per_page=100", "--paginate"])
    if not isinstance(files, list):
        raise GhError(f"{repo}#{number} files returned no list")
    return {entry["filename"] for entry in files}


@dataclass
class Overlap:
    ours: int
    theirs: int
    theirs_title: str
    shared: set[str] = field(default_factory=set)


def find_conflicts(repo: str, user: str) -> list[Overlap]:
    """Our open PRs whose changed files another open PR also changes.

    This is the check whose absence let a maintainer's refactor silently invalidate one of our
    PRs. Nobody commented on our thread, so no amount of watching our own threads would have
    surfaced it.
    """
    open_prs = run_gh(["api", f"repos/{repo}/pulls?state=open&per_page=100", "--paginate"])
    if not isinstance(open_prs, list):
        raise GhError(f"{repo} open pulls returned no list")
    ours = [pr for pr in open_prs if pr["user"]["login"] == user]
    others = [pr for pr in open_prs if pr["user"]["login"] != user]
    if not ours:
        return []
    our_files = {pr["number"]: changed_files(repo, pr["number"]) for pr in ours}
    overlaps: list[Overlap] = []
    for other in others:
        their_files = changed_files(repo, other["number"])
        for number, files in our_files.items():
            shared = files & their_files
            if shared:
                overlaps.append(
                    Overlap(
                        ours=number,
                        theirs=other["number"],
                        theirs_title=other["title"],
                        shared=shared,
                    )
                )
    return overlaps


def render(threads: list[Thread], user: str) -> str:
    lines: list[str] = []
    waiting_on_us = [t for t in threads if t.waiting_on(user) == "US"]
    open_threads = [t for t in threads if t.state.upper() == "OPEN"]

    lines.append(f"{len(open_threads)} open, {len(threads)} total, as {user}")
    if waiting_on_us:
        lines.append(f"** {len(waiting_on_us)} WAITING ON US **")
    lines.append("")
    lines.append(f"{'':4}{'thread':28} {'waiting':8} {'last activity':12} title")
    for thread in threads:
        if thread.state.upper() != "OPEN":
            continue
        mark = "*" if thread.ours else " "
        ident = f"{thread.repo.split('/')[-1]}#{thread.number}"
        lines.append(
            f"{mark:2}{thread.kind} {ident:24} {thread.waiting_on(user):8} "
            f"{thread.updated_at[:10]:12} {thread.title[:60]}"
        )
    lines.append("")
    lines.append("* = authored by us.  US = our thread and somebody else spoke last, or another")
    lines.append("  thread whose last comment @mentions us.  them = we spoke last.  - = someone")
    lines.append("  else's thread, last word not ours and not addressed to us.")
    return "\n".join(lines)


def render_conflicts(overlaps: list[Overlap], repo: str) -> str:
    if not overlaps:
        return f"\nNo file overlap between our open PRs and other open PRs in {repo}."
    lines = [f"\nFILE OVERLAP with other open PRs in {repo}:"]
    for overlap in overlaps:
        lines.append(f"  our #{overlap.ours} vs #{overlap.theirs} ({overlap.theirs_title[:50]})")
        for name in sorted(overlap.shared):
            lines.append(f"      {name}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument(
        "--repo", action="append", dest="repos",
        help="repo to scan; repeatable. Defaults to the two we work in.",
    )
    parser.add_argument(
        "--conflicts", action="store_true",
        help="also diff changed files across open PRs (slow: one request per open PR)",
    )
    args = parser.parse_args()
    repos = tuple(args.repos) if args.repos else DEFAULT_REPOS

    threads = collect(repos, args.user)
    print(render(threads, args.user))
    if args.conflicts:
        for repo in repos:
            print(render_conflicts(find_conflicts(repo, args.user), repo))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GhError as exc:
        print(f"upstream_status: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
