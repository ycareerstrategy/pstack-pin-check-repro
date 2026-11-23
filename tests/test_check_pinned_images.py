from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "check_pinned_images.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_pinned_images as cpi


def raw_url(repo: str, sha: str, asset_path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{asset_path}"


class TempGitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def write(self, rel_path: str, content: bytes | str) -> Path:
        full_path = self.path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full_path.write_bytes(content)
        else:
            with full_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        return full_path

    def commit_all(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


def run_cli(
    *args: str,
    cwd: Path,
    env_overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("GITHUB_REPOSITORY", None)
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestCheckPinnedImages(unittest.TestCase):
    def test_stale_vs_up_to_date_across_multiple_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.git("remote", "add", "origin", f"https://github.com/{target_repo}.git")

            repo.write("images/fig1.png", b"fig1-v1")
            repo.write("images/fig2.png", b"fig2-v1")
            repo.write("images/fig3.png", b"fig3-v1")
            v1 = repo.commit_all("Add initial figures")

            repo.write(
                "articles/post.md",
                (
                    f"![1]({raw_url(target_repo, v1, 'images/fig1.png')})\n"
                    f"![2]({raw_url(target_repo, v1, 'images/fig2.png')})\n"
                    f"![3]({raw_url(target_repo, v1, 'images/fig3.png')})\n"
                ),
            )
            repo.commit_all("Add article")

            repo.write("images/fig1.png", b"fig1-v2")
            v2 = repo.commit_all("Redraw fig1")

            repo.write("images/fig3.png", b"fig3-v2")
            v3 = repo.commit_all("Redraw fig3")

            proc = run_cli("--format", "json", cwd=repo.path)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)

            self.assertEqual(report["summary"]["total"], 3)
            self.assertEqual(report["summary"]["stale"], 2)
            self.assertEqual(report["summary"]["up_to_date"], 1)
            self.assertEqual(report["summary"]["errors"], 0)

            by_asset = {item["asset_path"]: item for item in report["findings"]}
            self.assertEqual(by_asset["images/fig1.png"]["status"], "stale")
            self.assertEqual(by_asset["images/fig1.png"]["latest_sha"], v2)
            self.assertEqual(by_asset["images/fig2.png"]["status"], "up_to_date")
            self.assertIsNone(by_asset["images/fig2.png"]["latest_sha"])
            self.assertEqual(by_asset["images/fig3.png"]["status"], "stale")
            self.assertEqual(by_asset["images/fig3.png"]["latest_sha"], v3)

    def test_reverted_file_is_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.git("remote", "add", "origin", f"git@github.com:{target_repo}.git")

            repo.write("images/chart.png", b"original-bytes")
            v1 = repo.commit_all("Add chart")

            repo.write(
                "doc.md",
                f"![chart]({raw_url(target_repo, v1, 'images/chart.png')})\n",
            )
            repo.commit_all("Pin chart at v1")

            repo.write("images/chart.png", b"temporary-change")
            repo.commit_all("Modify chart")

            repo.write("images/chart.png", b"original-bytes")
            repo.commit_all("Revert chart content")

            proc = run_cli("--format", "json", cwd=repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["total"], 1)
            self.assertEqual(report["summary"]["up_to_date"], 1)
            self.assertEqual(report["summary"]["stale"], 0)
            self.assertEqual(report["findings"][0]["status"], "up_to_date")

    def test_update_rewrites_only_stale_sha_preserving_crlf_and_other_urls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            other_repo = "other/repo"
            repo.git("remote", "add", "origin", f"https://github.com/{target_repo}.git")

            repo.write("images/stale fig.png", b"stale-v1")
            repo.write("images/same.png", b"same-v1")
            v1 = repo.commit_all("Initial images")

            repo.write("images/stale fig.png", b"stale-v2")
            v2 = repo.commit_all("Update stale fig")

            other_sha = "a" * 40
            original_content = (
                "# Title\r\n"
                "\r\n"
                "Prose stays untouched.\r\n"
                f"![stale]({raw_url(target_repo, v1, 'images/stale%20fig.png')}?raw=1#top)\r\n"
                f"![same]({raw_url(target_repo, v1, 'images/same.png')})\r\n"
                f"![external]({raw_url(other_repo, other_sha, 'images/stale%20fig.png')})\r\n"
            )
            doc_path = repo.write("articles/mixed.md", original_content)

            proc = run_cli("--update", "--format", "json", cwd=repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["mode"], "update")
            self.assertEqual(report["summary"]["files_updated"], 1)
            self.assertEqual(report["summary"]["urls_updated"], 1)

            expected_content = (
                "# Title\r\n"
                "\r\n"
                "Prose stays untouched.\r\n"
                f"![stale]({raw_url(target_repo, v2, 'images/stale%20fig.png')}?raw=1#top)\r\n"
                f"![same]({raw_url(target_repo, v1, 'images/same.png')})\r\n"
                f"![external]({raw_url(other_repo, other_sha, 'images/stale%20fig.png')})\r\n"
            )
            self.assertEqual(doc_path.read_bytes(), expected_content.encode("utf-8"))

            second = run_cli("--update", "--format", "json", cwd=repo.path)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_report = json.loads(second.stdout)
            self.assertEqual(second_report["summary"]["stale"], 0)
            self.assertEqual(second_report["summary"]["files_updated"], 0)
            self.assertEqual(second_report["summary"]["urls_updated"], 0)
            self.assertEqual(doc_path.read_bytes(), expected_content.encode("utf-8"))

    def test_fenced_code_blocks_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.git("remote", "add", "origin", f"https://github.com/{target_repo}.git")

            repo.write("images/fig.png", b"v1")
            v1 = repo.commit_all("Add fig v1")
            repo.write("images/fig.png", b"v2")
            v2 = repo.commit_all("Add fig v2")

            md = (
                "```markdown\n"
                f"![ignored1]({raw_url(target_repo, v1, 'images/fig.png')})\n"
                "```\n\n"
                "~~~~\n"
                "```\n"
                f"![ignored2]({raw_url(target_repo, v1, 'images/fig.png')})\n"
                "~~~~\n\n"
                "````markdown\n"
                "```\n"
                f"![ignored3]({raw_url(target_repo, v1, 'images/fig.png')})\n"
                "````\n\n"
                f"![real]({raw_url(target_repo, v1, 'images/fig.png')})\n"
            )
            doc_path = repo.write("README.md", md)

            proc = run_cli("--format", "json", cwd=repo.path)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["total"], 1)
            self.assertEqual(report["findings"][0]["line"], 15)
            self.assertEqual(report["findings"][0]["latest_sha"], v2)

            update_proc = run_cli("--update", cwd=repo.path)
            self.assertEqual(update_proc.returncode, 0, update_proc.stderr)
            updated_lines = doc_path.read_text(encoding="utf-8").splitlines()
            self.assertIn(v1, updated_lines[1])
            self.assertIn(v1, updated_lines[6])
            self.assertIn(v1, updated_lines[11])
            self.assertIn(v2, updated_lines[14])

    def test_multiple_urls_on_single_line_updated_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.git("remote", "add", "origin", f"https://github.com/{target_repo}.git")

            repo.write("images/a.png", b"a-v1")
            repo.write("images/b.png", b"b-v1")
            repo.write("images/c.png", b"c-v1")
            v1 = repo.commit_all("v1")

            repo.write("images/a.png", b"a-v2")
            va = repo.commit_all("update a")

            repo.write("images/c.png", b"c-v2")
            vc = repo.commit_all("update c")

            line = (
                f"![a]({raw_url(target_repo, v1, 'images/a.png')}) | "
                f"![b]({raw_url(target_repo, v1, 'images/b.png')}) | "
                f'<img src="{raw_url(target_repo, v1, "images/c.png")}">\n'
            )
            doc_path = repo.write("single_line.md", line)

            proc = run_cli("--update", "--format", "json", cwd=repo.path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["total"], 3)
            self.assertEqual(report["summary"]["stale"], 2)
            self.assertEqual(report["summary"]["up_to_date"], 1)
            self.assertEqual(report["summary"]["urls_updated"], 2)

            expected = (
                f"![a]({raw_url(target_repo, va, 'images/a.png')}) | "
                f"![b]({raw_url(target_repo, v1, 'images/b.png')}) | "
                f'<img src="{raw_url(target_repo, vc, "images/c.png")}">\n'
            )
            self.assertEqual(doc_path.read_text(encoding="utf-8"), expected)

    def test_error_cases_reported_and_left_untouched_by_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.git("remote", "add", "origin", f"https://github.com/{target_repo}.git")

            repo.write("images/deleted.png", b"will-delete")
            repo.write("images/kept.png", b"kept-v1")
            base_sha = repo.commit_all("Base commit")

            repo.git("checkout", "-b", "side-branch")
            repo.write("images/kept.png", b"side-branch-content")
            side_sha = repo.commit_all("Side branch commit")

            repo.git("checkout", "main")
            (repo.path / "images/deleted.png").unlink()
            repo.write("images/kept.png", b"kept-v2")
            repo.commit_all("Delete one image and update kept")

            unknown_sha = "0123456789abcdef0123456789abcdef01234567"
            content = (
                f"![unknown]({raw_url(target_repo, unknown_sha, 'images/kept.png')})\n"
                f"![non_anc]({raw_url(target_repo, side_sha, 'images/kept.png')})\n"
                f"![miss_pin]({raw_url(target_repo, base_sha, 'images/never_existed.png')})\n"
                f"![miss_tgt]({raw_url(target_repo, base_sha, 'images/deleted.png')})\n"
            )
            doc_path = repo.write("errors.md", content)

            check_proc = run_cli("--format", "json", cwd=repo.path)
            self.assertEqual(check_proc.returncode, 1, check_proc.stderr)
            check_report = json.loads(check_proc.stdout)
            statuses = [item["status"] for item in check_report["findings"]]
            self.assertEqual(
                statuses,
                [
                    "unknown_commit",
                    "non_ancestor",
                    "missing_at_pin",
                    "missing_at_target",
                ],
            )
            self.assertEqual(check_report["summary"]["errors"], 4)

            update_proc = run_cli("--update", "--format", "json", cwd=repo.path)
            self.assertEqual(update_proc.returncode, 1, update_proc.stderr)
            update_report = json.loads(update_proc.stdout)
            self.assertEqual(update_report["summary"]["files_updated"], 0)
            self.assertEqual(update_report["summary"]["urls_updated"], 0)
            self.assertEqual(doc_path.read_text(encoding="utf-8"), content)

    def test_json_structure_and_exit_codes_for_mixed_stale_and_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.git("remote", "add", "origin", f"https://github.com/{target_repo}.git")

            repo.write("images/fig.png", b"v1")
            v1 = repo.commit_all("v1")
            repo.write("images/fig.png", b"v2")
            v2 = repo.commit_all("v2")

            unknown_sha = "f" * 40
            doc_path = repo.write(
                "mixed.md",
                (
                    f"![stale]({raw_url(target_repo, v1, 'images/fig.png')})\n"
                    f"![bad]({raw_url(target_repo, unknown_sha, 'images/fig.png')})\n"
                ),
            )

            ro_proc = run_cli("--format", "json", cwd=repo.path)
            self.assertEqual(ro_proc.returncode, 1)
            ro_data = json.loads(ro_proc.stdout)
            self.assertEqual(ro_data["mode"], "check")
            self.assertEqual(ro_data["target_ref"], "HEAD")
            self.assertEqual(ro_data["target_repo"], target_repo)
            self.assertEqual(ro_data["summary"]["stale"], 1)
            self.assertEqual(ro_data["summary"]["errors"], 1)
            self.assertEqual(ro_data["updates"], [])

            upd_proc = run_cli("--update", "--format", "json", cwd=repo.path)
            self.assertEqual(upd_proc.returncode, 1)
            upd_data = json.loads(upd_proc.stdout)
            self.assertEqual(upd_data["mode"], "update")
            self.assertEqual(upd_data["summary"]["files_updated"], 1)
            self.assertEqual(upd_data["summary"]["urls_updated"], 1)
            self.assertEqual(
                upd_data["updates"],
                [{"file": "mixed.md", "replacements": 1, "changed": True}],
            )
            self.assertEqual(
                doc_path.read_text(encoding="utf-8"),
                (
                    f"![stale]({raw_url(target_repo, v2, 'images/fig.png')})\n"
                    f"![bad]({raw_url(target_repo, unknown_sha, 'images/fig.png')})\n"
                ),
            )

    def test_repo_resolution_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            repo.write("images/fig.png", b"v1")
            repo.commit_all("init")

            self.assertEqual(
                cpi.resolve_target_repo(repo.path, explicit_repo=None, env={}),
                cpi.DEFAULT_REPO,
            )

            repo.git("remote", "add", "origin", "git@github.com:RemoteOrg/RemoteRepo.git")
            self.assertEqual(
                cpi.resolve_target_repo(repo.path, explicit_repo=None, env={}),
                "RemoteOrg/RemoteRepo",
            )

            self.assertEqual(
                cpi.resolve_target_repo(
                    repo.path,
                    explicit_repo=None,
                    env={"GITHUB_REPOSITORY": "EnvOrg/EnvRepo"},
                ),
                "EnvOrg/EnvRepo",
            )

            self.assertEqual(
                cpi.resolve_target_repo(
                    repo.path,
                    explicit_repo="FlagOrg/FlagRepo",
                    env={"GITHUB_REPOSITORY": "EnvOrg/EnvRepo"},
                ),
                "FlagOrg/FlagRepo",
            )

            proc = run_cli(
                "--format",
                "json",
                "--repo",
                "FlagOrg/FlagRepo",
                cwd=repo.path,
                env_overrides={"GITHUB_REPOSITORY": "EnvOrg/EnvRepo"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                json.loads(proc.stdout)["target_repo"], "FlagOrg/FlagRepo"
            )

            proc_env = run_cli(
                "--format",
                "json",
                cwd=repo.path,
                env_overrides={"GITHUB_REPOSITORY": "EnvOrg/EnvRepo"},
            )
            self.assertEqual(proc_env.returncode, 0, proc_env.stderr)
            self.assertEqual(
                json.loads(proc_env.stdout)["target_repo"], "EnvOrg/EnvRepo"
            )

            proc_remote = run_cli("--format", "json", cwd=repo.path)
            self.assertEqual(proc_remote.returncode, 0, proc_remote.stderr)
            self.assertEqual(
                json.loads(proc_remote.stdout)["target_repo"],
                "RemoteOrg/RemoteRepo",
            )

    def test_case_insensitive_repo_and_directory_positional_arg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            repo.write("images/a.png", b"v1")
            v1 = repo.commit_all("v1")
            repo.write("images/a.png", b"v2")
            v2 = repo.commit_all("v2")

            repo.write(
                "docs/sub/page.md",
                f"![a]({raw_url('Acme/Docs', v1, 'images/a.png')})\n",
            )
            repo.write(
                "other/ignored.md",
                f"![a]({raw_url('acme/docs', v1, 'images/a.png')})\n",
            )

            proc = run_cli(
                "--repo",
                "acme/docs",
                "--format",
                "json",
                "docs",
                cwd=repo.path,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["total"], 1)
            self.assertEqual(report["findings"][0]["file"], "docs/sub/page.md")
            self.assertEqual(report["findings"][0]["latest_sha"], v2)

    def test_custom_ref_and_public_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = TempGitRepo(Path(tmp))
            target_repo = "acme/docs"
            repo.write("images/a.png", b"v1")
            v1 = repo.commit_all("v1")
            repo.write("images/a.png", b"v2")
            v2 = repo.commit_all("v2")

            text = f"![a]({raw_url(target_repo, v1, 'images/a.png')})\n"
            repo.write("doc.md", text)

            pins = cpi.extract_pinned_urls(text, Path("doc.md"), target_repo)
            self.assertEqual(len(pins), 1)
            self.assertEqual(text[pins[0].start : pins[0].end], v1)

            resolver = cpi.GitRepoResolver(repo.path)
            finding_at_v1 = cpi.classify_pin(pins[0], resolver, v1)
            self.assertEqual(finding_at_v1.status, cpi.PinStatus.UP_TO_DATE)

            finding_at_head = cpi.classify_pin(pins[0], resolver, "HEAD")
            self.assertEqual(finding_at_head.status, cpi.PinStatus.STALE)
            self.assertEqual(finding_at_head.latest_sha, v2)

            updated, count = cpi.apply_updates_to_text(text, [finding_at_head])
            self.assertEqual(count, 1)
            self.assertEqual(
                updated, f"![a]({raw_url(target_repo, v2, 'images/a.png')})\n"
            )

            proc = run_cli(
                "--repo", target_repo, "--ref", v1, "--format", "json", cwd=repo.path
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["summary"]["up_to_date"], 1)


if __name__ == "__main__":
    unittest.main()
