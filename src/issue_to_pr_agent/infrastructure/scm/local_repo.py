from __future__ import annotations

from pathlib import Path
import subprocess

from ...domain.entities import RepoSnapshot


class LocalRepoInspector:
    def __init__(self, root: Path) -> None:
        self.root = root

    def snapshot(self, *, max_files: int = 50) -> RepoSnapshot:
        if self._is_git_repo():
            branch = self._git_output(["rev-parse", "--abbrev-ref", "HEAD"]) or None
            status_short = self._git_output(["status", "--short"])
            files = self._git_output(["ls-files"]).splitlines()
            return RepoSnapshot(
                root=self.root,
                is_git_repo=True,
                branch=branch,
                status_short=status_short,
                tracked_files=files[:max_files],
                is_dirty=bool(status_short.strip()),
            )

        files = self._filesystem_files(max_files=max_files)
        return RepoSnapshot(
            root=self.root,
            is_git_repo=False,
            branch=None,
            status_short="",
            tracked_files=files,
            is_dirty=False,
        )

    def create_branch(self, branch_name: str) -> None:
        if not self._is_git_repo():
            raise RuntimeError("Cannot create a branch outside a git repository.")
        if self.branch_exists(branch_name):
            raise RuntimeError(f"Branch already exists: {branch_name}")
        self._run_git(["checkout", "-b", branch_name], check=True)

    def branch_exists(self, branch_name: str) -> bool:
        proc = self._run_git(["rev-parse", "--verify", branch_name], check=False)
        return proc.returncode == 0

    def current_branch(self) -> str | None:
        branch = self._git_output(["rev-parse", "--abbrev-ref", "HEAD"])
        return branch or None

    def changed_paths(self) -> list[str]:
        if not self._is_git_repo():
            return []
        status = self._git_output(["status", "--short"])
        paths: list[str] = []
        for line in status.splitlines():
            if not line.strip():
                continue
            path_text = line[3:].strip()
            if " -> " in path_text:
                path_text = path_text.split(" -> ", maxsplit=1)[1].strip()
            if path_text:
                paths.append(path_text)
        return paths

    def checkout_branch(self, branch_name: str) -> None:
        if not self.branch_exists(branch_name):
            raise RuntimeError(f"Branch does not exist: {branch_name}")
        self._run_git(["checkout", branch_name], check=True)

    def has_remote(self, remote_name: str) -> bool:
        proc = self._run_git(["remote", "get-url", remote_name], check=False)
        return proc.returncode == 0

    def stage_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        self._run_git(["add", "--", *paths], check=True)

    def staged_paths(self) -> list[str]:
        return [line for line in self._git_output(["diff", "--cached", "--name-only"]).splitlines() if line.strip()]

    def head_commit_sha(self) -> str | None:
        sha = self._git_output(["rev-parse", "HEAD"])
        return sha or None

    def commit_sha_for_ref(self, ref_name: str) -> str | None:
        sha = self._git_output(["rev-parse", ref_name])
        return sha or None

    def commit(self, message: str) -> str:
        if not self.staged_paths():
            raise RuntimeError("No staged changes are available to commit.")
        self._run_git(["commit", "-m", message], check=True)
        sha = self.head_commit_sha()
        if not sha:
            raise RuntimeError("Unable to determine commit SHA after git commit.")
        return sha

    def push_branch(self, remote_name: str, branch_name: str) -> None:
        self._run_git(["push", "--set-upstream", remote_name, branch_name], check=True)

    def _is_git_repo(self) -> bool:
        proc = self._run_git(["rev-parse", "--is-inside-work-tree"], check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def _git_output(self, args: list[str]) -> str:
        proc = self._run_git(args, check=False)
        return proc.stdout.rstrip() if proc.returncode == 0 else ""

    def _run_git(self, args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "color.ui=false", *args],
            cwd=self.root,
            capture_output=True,
            check=check,
            text=True,
        )

    def _filesystem_files(self, *, max_files: int) -> list[str]:
        ignored_dirs = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
        files: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if len(files) >= max_files:
                break
            if not path.is_file():
                continue
            if any(part in ignored_dirs for part in path.parts):
                continue
            files.append(str(path.relative_to(self.root)))
        return files
