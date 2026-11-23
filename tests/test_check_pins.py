"""Tests for tools/pin-check/check_pins.py.

Every test builds a disposable git repository and only looks at the
command's output and the files it rewrote, the same style as the run
described in the pstack practice guide.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "pin-check" / "check_pins.py"
SLUG = "acme/docs"


def url(sha: str, path: str, slug: str = SLUG) -> str:
    return f"https://raw.githubusercontent.com/{slug}/{sha}/{path}"


class TempRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.git("remote", "add", "origin", f"https://github.com/{SLUG}.git")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip()

    def write(self, rel: str, content: bytes | str) -> Path:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8", newline="")
        return p

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("GITHUB_REPOSITORY", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def summary_of(stdout: str) -> dict[str, int]:
    for line in stdout.splitlines():
        if line.startswith("summary:"):
            out = {}
            for token in line[len("summary:"):].split():
                key, _, value = token.partition("=")
                out[key] = int(value)
            return out
    raise AssertionError(f"no summary line in:\n{stdout}")


class CheckPinsTest(unittest.TestCase):
    def make_repo(self, tmp: str) -> TempRepo:
        return TempRepo(Path(tmp))

    def test_current_when_content_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            counts = summary_of(proc.stdout)
            self.assertEqual(counts["CURRENT"], 1)
            self.assertEqual(counts["total"], 1)

    def test_stale_detected_after_redraw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            repo.write("images/a.png", b"v2")
            repo.commit("redraw a")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 1)
            counts = summary_of(proc.stdout)
            self.assertEqual(counts["STALE"], 1)
            self.assertIn("STALE", proc.stdout)

    def test_touched_but_identical_content_is_current(self) -> None:
        # Blob IDs are compared, so a later commit that restores the same
        # bytes leaves the pin CURRENT.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"same-bytes")
            v1 = repo.commit("add a")
            repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            repo.write("images/a.png", b"other-bytes")
            repo.commit("change a")
            repo.write("images/a.png", b"same-bytes")
            repo.commit("revert a")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(summary_of(proc.stdout)["CURRENT"], 1)

    def test_unknown_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            repo.commit("add a")
            unknown = "0" * 40
            repo.write("doc.md", f"![a]({url(unknown, 'images/a.png')})\n")
            repo.commit("add doc")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(summary_of(proc.stdout)["UNKNOWN_COMMIT"], 1)

    def test_missing_at_pin_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            repo.write(
                "doc.md", f"![x]({url(v1, 'images/never.png')})\n"
            )
            repo.commit("add doc")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(summary_of(proc.stdout)["MISSING_AT_PIN"], 1)

    def test_missing_at_head_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            (repo.path / "images/a.png").unlink()
            repo.commit("delete a")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(summary_of(proc.stdout)["MISSING_AT_HEAD"], 1)

    def test_not_a_file_is_reported_but_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            repo.write("doc.md", f"Base: {url(v1, 'images')}\n")
            repo.commit("add doc")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(summary_of(proc.stdout)["NOT_A_FILE"], 1)

    def test_fix_rewrites_to_last_commit_touching_file_not_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            doc = repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            repo.write("images/a.png", b"v2")
            v2 = repo.commit("redraw a")
            repo.write("unrelated.txt", "noise")
            head = repo.commit("unrelated change")
            self.assertNotEqual(v2, head)
            proc = run(repo.path, "--fix")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            fixed_lines = [
                line for line in proc.stdout.splitlines()
                if line.startswith("fixed ")
            ]
            self.assertEqual(len(fixed_lines), 1)
            self.assertIn(v2, doc.read_text(encoding="utf-8"))
            self.assertNotIn(head, doc.read_text(encoding="utf-8"))

    def test_stale_target_under_merge_head_is_the_branch_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            doc = repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            repo.git("checkout", "-b", "redraw")
            repo.write("images/a.png", b"v2")
            branch_commit = repo.commit("redraw a on branch")
            repo.git("checkout", "main")
            repo.write("other.txt", "main work")
            repo.commit("main work")
            repo.git("merge", "--no-ff", "-m", "merge redraw", "redraw")
            merge_head = repo.git("rev-parse", "HEAD")
            self.assertNotEqual(branch_commit, merge_head)
            proc = run(repo.path, "--fix")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = doc.read_text(encoding="utf-8")
            self.assertIn(branch_commit, text)
            self.assertNotIn(merge_head, text)

    def test_second_fix_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            doc = repo.write("doc.md", f"![a]({url(v1, 'images/a.png')})\n")
            repo.commit("add doc")
            repo.write("images/a.png", b"v2")
            repo.commit("redraw a")
            first = run(repo.path, "--fix")
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = doc.read_bytes()
            second = run(repo.path, "--fix")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("fixed ", second.stdout)
            self.assertEqual(doc.read_bytes(), after_first)
            self.assertEqual(summary_of(second.stdout)["STALE"], 0)

    def test_fix_changes_only_the_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            repo.write("images/b.png", b"keep")
            v1 = repo.commit("add figures")
            before = (
                "# Title\r\n"
                f"![a]({url(v1, 'images/a.png')}) and "
                f"![b]({url(v1, 'images/b.png')})\r\n"
                "tail line\r\n"
            )
            doc = repo.write("doc.md", before)
            repo.commit("add doc")
            repo.write("images/a.png", b"v2")
            v2 = repo.commit("redraw a")
            proc = run(repo.path, "--fix")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(doc, encoding="utf-8", newline="") as handle:
                after = handle.read()
            self.assertEqual(after, before.replace(v1, v2, 1))
            self.assertEqual(after.count("\r\n"), 3)

    def test_other_repos_urls_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            repo.write(
                "doc.md",
                f"![x]({url(v1, 'images/a.png', slug='someone/else')})\n",
            )
            repo.commit("add doc")
            repo.write("images/a.png", b"v2")
            repo.commit("redraw a")
            proc = run(repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(summary_of(proc.stdout)["total"], 0)

    def test_fix_leaves_failing_states_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            repo.write("images/a.png", b"v1")
            v1 = repo.commit("add a")
            content = (
                f"![missing]({url(v1, 'images/never.png')})\n"
                f"![unknown]({url('f' * 40, 'images/a.png')})\n"
            )
            doc = repo.write("doc.md", content)
            repo.commit("add doc")
            proc = run(repo.path, "--fix")
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("fixed ", proc.stdout)
            self.assertEqual(doc.read_text(encoding="utf-8"), content)

    def test_exit_code_2_outside_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(Path(tmp))
            self.assertEqual(proc.returncode, 2)

    def test_exit_code_2_on_bad_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(tmp)
            proc = run(repo.path, "--no-such-option")
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
