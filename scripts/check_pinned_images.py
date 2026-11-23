#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Protocol, Sequence
from urllib.parse import unquote, urlparse

DEFAULT_REPO = "ycareerstrategy/pstack-pin-check-repro"

RAW_GITHUB_URL_RE = re.compile(
    r"https://raw\.githubusercontent\.com/"
    r"(?P<owner>[^/\s)\"'<>?#]+)/"
    r"(?P<repo>[^/\s)\"'<>?#]+)/"
    r"(?P<sha>[0-9a-fA-F]{40})/"
    r"(?P<path>[^\s)\"'<>?#]+)"
)
OPEN_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
SSH_GITHUB_REMOTE_RE = re.compile(
    r"^(?:[^@\s]+@)?github\.com:(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$",
    re.IGNORECASE,
)


class PinStatus(str, Enum):
    UP_TO_DATE = "up_to_date"
    STALE = "stale"
    MISSING_AT_TARGET = "missing_at_target"
    MISSING_AT_PIN = "missing_at_pin"
    UNKNOWN_COMMIT = "unknown_commit"
    NON_ANCESTOR = "non_ancestor"


ERROR_STATUSES: frozenset[PinStatus] = frozenset(
    {
        PinStatus.MISSING_AT_TARGET,
        PinStatus.MISSING_AT_PIN,
        PinStatus.UNKNOWN_COMMIT,
        PinStatus.NON_ANCESTOR,
    }
)


@dataclass(frozen=True)
class PinnedUrl:
    file_path: Path
    line: int
    start: int
    end: int
    url: str
    repo: str
    pinned_sha: str
    asset_path: str


@dataclass(frozen=True)
class CheckFinding:
    pin: PinnedUrl
    status: PinStatus
    pinned_blob: str | None
    target_blob: str | None
    latest_sha: str | None
    detail: str


@dataclass(frozen=True)
class UpdateResult:
    file_path: Path
    replacements: int
    changed: bool


class GitBlobResolver(Protocol):
    def commit_exists(self, sha: str) -> bool: ...

    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...

    def blob_hash(self, rev: str, asset_path: str) -> str | None: ...

    def latest_commit_for_path(
        self, target_ref: str, asset_path: str, expected_blob: str
    ) -> str: ...


def extract_pinned_urls(
    text: str, file_path: Path, target_repo: str
) -> list[PinnedUrl]:
    target_repo_folded = target_repo.casefold()
    results: list[PinnedUrl] = []
    active_fence: tuple[str, int] | None = None
    offset = 0

    for line_no, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        stripped_eol = raw_line.rstrip("\r\n")
        if active_fence is not None:
            fence_char, fence_len = active_fence
            closing_re = re.compile(
                rf"^[ ]{{0,3}}{re.escape(fence_char)}{{{fence_len},}}[ \t]*$"
            )
            if closing_re.match(stripped_eol):
                active_fence = None
            offset += len(raw_line)
            continue

        open_match = OPEN_FENCE_RE.match(stripped_eol)
        if open_match is not None:
            fence_seq, info = open_match.group(1), open_match.group(2)
            if not (fence_seq[0] == "`" and "`" in info):
                active_fence = (fence_seq[0], len(fence_seq))
                offset += len(raw_line)
                continue

        for match in RAW_GITHUB_URL_RE.finditer(raw_line):
            repo = f"{match.group('owner')}/{match.group('repo')}"
            if repo.casefold() != target_repo_folded:
                continue
            results.append(
                PinnedUrl(
                    file_path=file_path,
                    line=line_no,
                    start=offset + match.start("sha"),
                    end=offset + match.end("sha"),
                    url=match.group(0),
                    repo=repo,
                    pinned_sha=match.group("sha"),
                    asset_path=unquote(match.group("path")),
                )
            )

        offset += len(raw_line)

    return results


def classify_pin(
    pin: PinnedUrl, resolver: GitBlobResolver, target_ref: str
) -> CheckFinding:
    if not resolver.commit_exists(pin.pinned_sha):
        return CheckFinding(
            pin=pin,
            status=PinStatus.UNKNOWN_COMMIT,
            pinned_blob=None,
            target_blob=None,
            latest_sha=None,
            detail=f"Pinned commit {pin.pinned_sha} does not exist",
        )

    if not resolver.is_ancestor(pin.pinned_sha, target_ref):
        return CheckFinding(
            pin=pin,
            status=PinStatus.NON_ANCESTOR,
            pinned_blob=None,
            target_blob=None,
            latest_sha=None,
            detail=f"Pinned commit {pin.pinned_sha} is not an ancestor of {target_ref}",
        )

    pinned_blob = resolver.blob_hash(pin.pinned_sha, pin.asset_path)
    if pinned_blob is None:
        return CheckFinding(
            pin=pin,
            status=PinStatus.MISSING_AT_PIN,
            pinned_blob=None,
            target_blob=None,
            latest_sha=None,
            detail=f"Path {pin.asset_path} missing at pinned commit {pin.pinned_sha}",
        )

    target_blob = resolver.blob_hash(target_ref, pin.asset_path)
    if target_blob is None:
        return CheckFinding(
            pin=pin,
            status=PinStatus.MISSING_AT_TARGET,
            pinned_blob=pinned_blob,
            target_blob=None,
            latest_sha=None,
            detail=f"Path {pin.asset_path} missing at target ref {target_ref}",
        )

    if pinned_blob == target_blob:
        return CheckFinding(
            pin=pin,
            status=PinStatus.UP_TO_DATE,
            pinned_blob=pinned_blob,
            target_blob=target_blob,
            latest_sha=None,
            detail="Pinned blob matches target ref",
        )

    latest_sha = resolver.latest_commit_for_path(
        target_ref, pin.asset_path, target_blob
    )
    return CheckFinding(
        pin=pin,
        status=PinStatus.STALE,
        pinned_blob=pinned_blob,
        target_blob=target_blob,
        latest_sha=latest_sha,
        detail=f"Content changed; latest commit for {pin.asset_path} is {latest_sha}",
    )


def apply_updates_to_text(
    text: str, findings: Sequence[CheckFinding]
) -> tuple[str, int]:
    stale_findings: list[tuple[CheckFinding, str]] = [
        (finding, finding.latest_sha)
        for finding in findings
        if finding.status == PinStatus.STALE and finding.latest_sha is not None
    ]
    if not stale_findings:
        return text, 0

    ordered = sorted(
        stale_findings, key=lambda item: item[0].pin.start, reverse=True
    )
    updated = text
    prev_start: int | None = None
    replacements = 0

    for finding, latest_sha in ordered:
        start, end = finding.pin.start, finding.pin.end
        if prev_start is not None and end > prev_start:
            raise ValueError(
                f"Overlapping replacement spans in {finding.pin.file_path}"
            )
        actual_sha = updated[start:end]
        if actual_sha != finding.pin.pinned_sha:
            raise ValueError(
                f"Span mismatch in {finding.pin.file_path}:{finding.pin.line}: "
                f"expected {finding.pin.pinned_sha}, found {actual_sha}"
            )
        updated = updated[:start] + latest_sha + updated[end:]
        prev_start = start
        replacements += 1

    return updated, replacements


def parse_github_repo_from_remote(remote_url: str) -> str | None:
    cleaned = remote_url.strip()
    if not cleaned:
        return None

    ssh_match = SSH_GITHUB_REMOTE_RE.match(cleaned)
    if ssh_match is not None:
        owner = ssh_match.group("owner")
        repo = ssh_match.group("repo").rstrip("/").removesuffix(".git")
        if owner and repo:
            return f"{owner}/{repo}"
        return None

    parsed = urlparse(cleaned)
    if parsed.hostname and parsed.hostname.lower() == "github.com":
        path = parsed.path.strip("/").removesuffix(".git")
        parts = path.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"
    return None


def compute_exit_code(
    findings: Sequence[CheckFinding], *, update_mode: bool
) -> int:
    has_errors = any(finding.status in ERROR_STATUSES for finding in findings)
    if update_mode:
        return 1 if has_errors else 0
    has_stale = any(finding.status == PinStatus.STALE for finding in findings)
    return 1 if (has_errors or has_stale) else 0


def format_text_report(
    findings: Sequence[CheckFinding],
    *,
    target_ref: str,
    target_repo: str,
    updates: Sequence[UpdateResult] = (),
    update_mode: bool = False,
) -> str:
    lines: list[str] = []
    for finding in findings:
        location = f"{finding.pin.file_path}:{finding.pin.line}"
        status = finding.status.value
        asset = finding.pin.asset_path
        if finding.status == PinStatus.STALE and finding.latest_sha is not None:
            action = "updated" if update_mode else "stale"
            lines.append(
                f"[{action}] {location} {asset} "
                f"{finding.pin.pinned_sha} -> {finding.latest_sha}"
            )
        elif finding.status == PinStatus.UP_TO_DATE:
            lines.append(
                f"[{status}] {location} {asset} {finding.pin.pinned_sha}"
            )
        else:
            lines.append(
                f"[{status}] {location} {asset} "
                f"{finding.pin.pinned_sha} ({finding.detail})"
            )

    counts = {status: 0 for status in PinStatus}
    for finding in findings:
        counts[finding.status] += 1
    error_count = sum(counts[status] for status in ERROR_STATUSES)

    summary = (
        f"Summary ({target_repo} @ {target_ref}): "
        f"{len(findings)} total, {counts[PinStatus.UP_TO_DATE]} up_to_date, "
        f"{counts[PinStatus.STALE]} stale, {error_count} errors"
    )
    if update_mode:
        urls_updated = sum(result.replacements for result in updates)
        files_updated = sum(1 for result in updates if result.changed)
        summary += f"; updated {urls_updated} URL(s) in {files_updated} file(s)"
    lines.append(summary)
    return "\n".join(lines)


def format_json_report(
    findings: Sequence[CheckFinding],
    *,
    target_ref: str,
    target_repo: str,
    updates: Sequence[UpdateResult] = (),
    update_mode: bool = False,
) -> str:
    counts = {status: 0 for status in PinStatus}
    for finding in findings:
        counts[finding.status] += 1
    error_count = sum(counts[status] for status in ERROR_STATUSES)

    payload = {
        "target_ref": target_ref,
        "target_repo": target_repo,
        "mode": "update" if update_mode else "check",
        "summary": {
            "total": len(findings),
            "up_to_date": counts[PinStatus.UP_TO_DATE],
            "stale": counts[PinStatus.STALE],
            "errors": error_count,
            "by_status": {status.value: counts[status] for status in PinStatus},
            "files_updated": sum(1 for result in updates if result.changed),
            "urls_updated": sum(result.replacements for result in updates),
        },
        "findings": [
            {
                "file": str(finding.pin.file_path),
                "line": finding.pin.line,
                "start": finding.pin.start,
                "end": finding.pin.end,
                "url": finding.pin.url,
                "repo": finding.pin.repo,
                "asset_path": finding.pin.asset_path,
                "pinned_sha": finding.pin.pinned_sha,
                "status": finding.status.value,
                "pinned_blob": finding.pinned_blob,
                "target_blob": finding.target_blob,
                "latest_sha": finding.latest_sha,
                "detail": finding.detail,
            }
            for finding in findings
        ],
        "updates": [
            {
                "file": str(result.file_path),
                "replacements": result.replacements,
                "changed": result.changed,
            }
            for result in updates
        ],
    }
    return json.dumps(payload, indent=2)


class GitRepoResolver:
    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._commit_exists_cache: dict[str, bool] = {}
        self._is_ancestor_cache: dict[tuple[str, str], bool] = {}
        self._blob_hash_cache: dict[tuple[str, str], str | None] = {}
        self._latest_commit_cache: dict[tuple[str, str, str], str] = {}
        self._rev_parse_cache: dict[str, str] = {}

    def _run_git(self, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=self._repo_root,
            capture_output=True,
            check=False,
        )

    def resolve_commit(self, rev: str) -> str:
        cached = self._rev_parse_cache.get(rev)
        if cached is not None:
            return cached
        proc = self._run_git(["rev-parse", "--verify", f"{rev}^{{commit}}"])
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Unable to resolve git commit '{rev}': {stderr}")
        resolved = proc.stdout.decode("ascii").strip()
        self._rev_parse_cache[rev] = resolved
        return resolved

    def commit_exists(self, sha: str) -> bool:
        cached = self._commit_exists_cache.get(sha)
        if cached is not None:
            return cached
        proc = self._run_git(["cat-file", "-e", f"{sha}^{{commit}}"])
        exists = proc.returncode == 0
        self._commit_exists_cache[sha] = exists
        return exists

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        key = (ancestor, descendant)
        cached = self._is_ancestor_cache.get(key)
        if cached is not None:
            return cached
        proc = self._run_git(["merge-base", "--is-ancestor", ancestor, descendant])
        result = proc.returncode == 0
        self._is_ancestor_cache[key] = result
        return result

    def blob_hash(self, rev: str, asset_path: str) -> str | None:
        key = (rev, asset_path)
        if key in self._blob_hash_cache:
            return self._blob_hash_cache[key]
        proc = self._run_git(["ls-tree", "-z", rev, "--", asset_path])
        blob: str | None = None
        if proc.returncode == 0:
            for entry in proc.stdout.split(b"\0"):
                if not entry:
                    continue
                meta, sep, path_bytes = entry.partition(b"\t")
                if not sep:
                    continue
                parts = meta.decode("ascii", errors="replace").split()
                if len(parts) != 3:
                    continue
                _mode, obj_type, obj_hash = parts
                entry_path = path_bytes.decode("utf-8", errors="surrogateescape")
                if obj_type == "blob" and entry_path == asset_path:
                    blob = obj_hash
                    break
        self._blob_hash_cache[key] = blob
        return blob

    def latest_commit_for_path(
        self, target_ref: str, asset_path: str, expected_blob: str
    ) -> str:
        key = (target_ref, asset_path, expected_blob)
        cached = self._latest_commit_cache.get(key)
        if cached is not None:
            return cached
        proc = self._run_git(
            ["log", "-n", "1", "--format=%H", target_ref, "--", asset_path]
        )
        candidate = proc.stdout.decode("ascii", errors="replace").strip()
        if (
            proc.returncode == 0
            and len(candidate) == 40
            and self.blob_hash(candidate, asset_path) == expected_blob
        ):
            result = candidate
        else:
            result = self.resolve_commit(target_ref)
        self._latest_commit_cache[key] = result
        return result


def find_repo_root(explicit_root: str | None) -> Path:
    if explicit_root is not None:
        root = Path(explicit_root).resolve()
        if not root.is_dir():
            raise RuntimeError(f"Repository root does not exist: {root}")
        return root

    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Unable to locate git repository root: {stderr}")
    return Path(proc.stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()


def resolve_target_repo(
    repo_root: Path,
    explicit_repo: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if explicit_repo is not None and explicit_repo.strip():
        return explicit_repo.strip()

    environment = os.environ if env is None else env
    github_repo = environment.get("GITHUB_REPOSITORY", "").strip()
    if github_repo:
        return github_repo

    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        remote_url = proc.stdout.decode("utf-8", errors="replace").strip()
        parsed = parse_github_repo_from_remote(remote_url)
        if parsed is not None:
            return parsed

    return DEFAULT_REPO


def discover_markdown_files(
    repo_root: Path, paths: Sequence[str]
) -> list[Path]:
    discovered: set[Path] = set()
    if not paths:
        proc = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.md",
            ],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to list markdown files: {stderr}")
        for entry in proc.stdout.split(b"\0"):
            if not entry:
                continue
            rel = entry.decode("utf-8", errors="surrogateescape")
            candidate = (repo_root / rel).resolve()
            if candidate.is_file():
                discovered.add(candidate)
        return sorted(discovered)

    cwd = Path.cwd()
    for raw_path in paths:
        input_path = Path(raw_path)
        if input_path.is_absolute():
            resolved = input_path.resolve()
        elif (cwd / input_path).exists():
            resolved = (cwd / input_path).resolve()
        else:
            resolved = (repo_root / input_path).resolve()

        if not resolved.exists():
            raise RuntimeError(f"Path does not exist: {raw_path}")
        if resolved.is_dir():
            for md_file in resolved.rglob("*.md"):
                if md_file.is_file():
                    discovered.add(md_file.resolve())
        elif resolved.is_file():
            discovered.add(resolved)

    return sorted(discovered)


def display_path(file_path: Path, repo_root: Path) -> Path:
    try:
        return file_path.relative_to(repo_root)
    except ValueError:
        return file_path


def write_text_atomically(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def run_check(
    repo_root: Path,
    files: Sequence[Path],
    target_ref: str,
    target_repo: str,
    *,
    update_mode: bool,
) -> tuple[list[CheckFinding], list[UpdateResult]]:
    resolver = GitRepoResolver(repo_root)
    resolver.resolve_commit(target_ref)

    all_findings: list[CheckFinding] = []
    updates: list[UpdateResult] = []

    for file_path in files:
        shown_path = display_path(file_path, repo_root)
        with file_path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()

        pins = extract_pinned_urls(text, shown_path, target_repo)
        file_findings = [
            classify_pin(pin, resolver, target_ref) for pin in pins
        ]
        all_findings.extend(file_findings)

        if update_mode:
            updated_text, replacements = apply_updates_to_text(
                text, file_findings
            )
            changed = replacements > 0 and updated_text != text
            if changed:
                write_text_atomically(file_path, updated_text)
            if replacements > 0:
                updates.append(
                    UpdateResult(
                        file_path=shown_path,
                        replacements=replacements,
                        changed=changed,
                    )
                )

    return all_findings, updates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check commit-pinned GitHub raw image URLs in Markdown files "
            "against git history and optionally update stale pins in place."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Markdown files or directories to check (defaults to repo *.md files).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update stale pinned commit SHAs in place.",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="Target git ref to compare pinned URLs against (default: HEAD).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Target GitHub repository in owner/repo format.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Git repository root directory.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
        help="Output format (default: text).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        repo_root = find_repo_root(args.root)
        target_repo = resolve_target_repo(repo_root, explicit_repo=args.repo)
        files = discover_markdown_files(repo_root, args.paths)
        findings, updates = run_check(
            repo_root=repo_root,
            files=files,
            target_ref=args.ref,
            target_repo=target_repo,
            update_mode=args.update,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output_format == "json":
        output = format_json_report(
            findings,
            target_ref=args.ref,
            target_repo=target_repo,
            updates=updates,
            update_mode=args.update,
        )
    else:
        output = format_text_report(
            findings,
            target_ref=args.ref,
            target_repo=target_repo,
            updates=updates,
            update_mode=args.update,
        )
    print(output)
    return compute_exit_code(findings, update_mode=args.update)


if __name__ == "__main__":
    raise SystemExit(main())
