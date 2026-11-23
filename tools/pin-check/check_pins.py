#!/usr/bin/env python3
"""Check commit-pinned raw.githubusercontent.com URLs in Markdown files.

For every URL of the form

    https://raw.githubusercontent.com/<owner>/<repo>/<40-hex sha>/<path>

the file content at the pinned commit is compared with the content at HEAD
by git blob ID, so a file that was touched but ended up with identical
content still counts as CURRENT. Queries are batched through a single
``git cat-file --batch-check`` call.

Read-only by default. ``--fix`` rewrites the sha of STALE pins to the last
commit that touched the file (never HEAD itself: in PR CI, HEAD is a
temporary merge commit whose URL would not survive the merge).

Exit codes: 0 = no failing status, 1 = at least one failing status,
2 = bad arguments or a git error.

Python standard library + git only.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class GitError(Exception):
    pass


class Status(str, Enum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    MISSING_AT_HEAD = "MISSING_AT_HEAD"
    MISSING_AT_PIN = "MISSING_AT_PIN"
    UNKNOWN_COMMIT = "UNKNOWN_COMMIT"
    NOT_A_FILE = "NOT_A_FILE"


@dataclass(frozen=True)
class Policy:
    fails_ci: bool
    fixable: bool


# The single table that decides, per status, whether CI fails and whether
# --fix may rewrite the pin.
POLICY: dict[Status, Policy] = {
    Status.CURRENT: Policy(fails_ci=False, fixable=False),
    Status.STALE: Policy(fails_ci=True, fixable=True),
    Status.MISSING_AT_HEAD: Policy(fails_ci=True, fixable=False),
    Status.MISSING_AT_PIN: Policy(fails_ci=True, fixable=False),
    Status.UNKNOWN_COMMIT: Policy(fails_ci=True, fixable=False),
    Status.NOT_A_FILE: Policy(fails_ci=False, fixable=False),
}

URL_RE = re.compile(
    r"https://raw\.githubusercontent\.com/"
    r"(?P<owner>[^/\s)\"'<>]+)/"
    r"(?P<repo>[^/\s)\"'<>]+)/"
    r"(?P<sha>[0-9a-f]{40})/"
    r"(?P<path>[^\s)\"'<>?#]+)"
)


@dataclass(frozen=True)
class Pin:
    file: str  # repo-relative path of the Markdown file
    line: int  # 1-based
    sha_start: int  # char offset of the sha within the whole file text
    sha_end: int
    sha: str
    path: str  # path inside the repository, as written (no unquoting)


@dataclass
class Finding:
    pin: Pin
    status: Status
    target: str | None = None  # fix target commit, for STALE pins


def git(root: str, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}"
        )
    return proc.stdout


def repo_root() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError("not inside a git repository")
    return proc.stdout.strip()


def repo_slug(root: str) -> str | None:
    """The owner/repo this checkout belongs to, used to filter URLs."""
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env:
        return env
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def markdown_files(root: str, paths: list[str]) -> list[str]:
    if paths:
        found: list[str] = []
        for raw in paths:
            p = Path(root, raw)
            if p.is_dir():
                found.extend(
                    str(f.relative_to(root)) for f in sorted(p.rglob("*.md"))
                )
            elif p.is_file():
                found.append(str(p.relative_to(root)))
            else:
                raise GitError(f"path does not exist: {raw}")
        return sorted(set(found))
    out = git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "*.md",
    )
    return sorted(f for f in out.split("\0") if f)


def read_text(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def find_pins(root: str, files: list[str], slug: str | None) -> list[Pin]:
    pins: list[Pin] = []
    for rel in files:
        text = read_text(Path(root, rel))
        offset = 0
        for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
            for m in URL_RE.finditer(line):
                if slug is not None:
                    url_slug = f"{m.group('owner')}/{m.group('repo')}"
                    if url_slug.casefold() != slug.casefold():
                        continue
                pins.append(
                    Pin(
                        file=rel,
                        line=line_no,
                        sha_start=offset + m.start("sha"),
                        sha_end=offset + m.end("sha"),
                        sha=m.group("sha"),
                        path=m.group("path"),
                    )
                )
            offset += len(line)
    return pins


def batch_info(root: str, pins: list[Pin]) -> dict[str, tuple[str, str] | None]:
    """One cat-file --batch-check call for every spec every pin needs."""
    specs: list[str] = []
    seen: set[str] = set()
    for pin in pins:
        for spec in (
            f"{pin.sha}^{{commit}}",
            f"{pin.sha}:{pin.path}",
            f"HEAD:{pin.path}",
        ):
            if spec not in seen:
                seen.add(spec)
                specs.append(spec)
    if not specs:
        return {}
    out = git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        stdin="".join(s + "\n" for s in specs),
    )
    info: dict[str, tuple[str, str] | None] = {}
    lines = out.splitlines()
    if len(lines) != len(specs):
        raise GitError("unexpected cat-file --batch-check output")
    for spec, line in zip(specs, lines):
        if line.endswith(" missing"):
            info[spec] = None
        else:
            parts = line.split(" ")
            info[spec] = (parts[0], parts[1])
    return info


def classify(info: dict, sha: str, path: str) -> Status:
    if info[f"{sha}^{{commit}}"] is None:
        return Status.UNKNOWN_COMMIT
    at_pin = info[f"{sha}:{path}"]
    if at_pin is None:
        return Status.MISSING_AT_PIN
    if at_pin[1] != "blob":
        return Status.NOT_A_FILE
    at_head = info[f"HEAD:{path}"]
    if at_head is None or at_head[1] != "blob":
        return Status.MISSING_AT_HEAD
    return Status.CURRENT if at_pin[0] == at_head[0] else Status.STALE


def fix_target(root: str, path: str, head_blob: str) -> str:
    target = git(
        root, "log", "-1", "--format=%H", "HEAD", "--", f":(top,literal){path}"
    ).strip()
    blob = git(
        root, "rev-parse", "--verify", "--quiet", f"{target}:{path}"
    ).strip()
    if blob != head_blob:
        raise GitError(
            f"latest commit touching {path} ({target}) does not hold HEAD's content"
        )
    return target


def scan(root: str, files: list[str], slug: str | None) -> list[Finding]:
    pins = find_pins(root, files, slug)
    info = batch_info(root, pins)
    findings = []
    for pin in pins:
        status = classify(info, pin.sha, pin.path)
        target = None
        if POLICY[status].fixable:
            target = fix_target(root, pin.path, info[f"HEAD:{pin.path}"][0])
        findings.append(Finding(pin=pin, status=status, target=target))
    return findings


def apply_fixes(root: str, findings: list[Finding]) -> list[Finding]:
    """Rewrite STALE shas in place; returns the fixed findings."""
    fixed: list[Finding] = []
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        if POLICY[f.status].fixable and f.target is not None:
            by_file.setdefault(f.pin.file, []).append(f)
    for rel, group in by_file.items():
        path = Path(root, rel)
        text = read_text(path)
        for f in sorted(group, key=lambda x: x.pin.sha_start, reverse=True):
            start, end = f.pin.sha_start, f.pin.sha_end
            if text[start:end] != f.pin.sha:
                raise GitError(f"span mismatch in {rel}:{f.pin.line}")
            text = text[:start] + f.target + text[end:]
        mode = path.stat().st_mode
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        fixed.extend(group)
    return fixed


def summary_line(findings: list[Finding]) -> str:
    counts = {status: 0 for status in Status}
    for f in findings:
        counts[f.status] += 1
    summary = " ".join(f"{s.value}={counts[s]}" for s in Status)
    return f"summary: {summary} total={len(findings)}"


def report(findings: list[Finding]) -> None:
    for f in findings:
        pin = f.pin
        location = f"{pin.file}:{pin.line}"
        if f.status is Status.STALE:
            print(
                f"{f.status.value} {location} {pin.sha[:12]} "
                f"{pin.path} -> {f.target[:12] if f.target else '?'}"
            )
        else:
            print(f"{f.status.value} {location} {pin.sha[:12]} {pin.path}")
    print(summary_line(findings))


def exit_code(findings: list[Finding]) -> int:
    return 1 if any(POLICY[f.status].fails_ci for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check commit-pinned raw image URLs in Markdown files."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite STALE pins to the last commit that touched the file",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Markdown files or directories (default: all *.md in the repo)",
    )
    args = parser.parse_args(argv)

    try:
        root = repo_root()
        slug = repo_slug(root)
        files = markdown_files(root, args.paths)
        findings = scan(root, files, slug)
        if args.fix:
            fixed = apply_fixes(root, findings)
            for f in fixed:
                pin = f.pin
                print(
                    f"fixed {pin.file}:{pin.line} {pin.path} "
                    f"{pin.sha} -> {f.target}"
                )
            if fixed:
                # Re-scan after fixing and report the new state.
                findings = scan(root, files, slug)
                print(summary_line(findings))
            else:
                print(summary_line(findings))
            return exit_code(findings)
        report(findings)
        return exit_code(findings)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
