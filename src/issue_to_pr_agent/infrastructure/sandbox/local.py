from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class LocalSandboxMaterialization:
    workspace_root: Path
    copied_file_count: int
    skipped_entry_count: int
    total_bytes: int
    skipped_entries: list[str]
    materialization_strategy: str = "copy"
    source_branch: str | None = None
    source_head_sha: str | None = None


class LocalSandboxManager:
    def __init__(
        self,
        *,
        max_file_bytes: int,
        ignored_dir_names: tuple[str, ...] = (
            ".git",
            ".issue-to-pr",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
        ),
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._ignored_dir_names = ignored_dir_names

    def prepare(
        self,
        *,
        source_repo_root: Path,
        workspace_root: Path,
        artifact_dir: Path,
    ) -> LocalSandboxMaterialization:
        source_repo_root = source_repo_root.resolve()
        workspace_root = workspace_root.resolve()
        artifact_dir = artifact_dir.resolve()

        if not source_repo_root.exists():
            raise FileNotFoundError(f"Repository root does not exist: {source_repo_root}")
        if not source_repo_root.is_dir():
            raise NotADirectoryError(f"Repository root is not a directory: {source_repo_root}")
        if workspace_root.exists():
            raise FileExistsError(f"Sandbox workspace already exists: {workspace_root}")

        artifact_relative = _relative_to(source_repo_root=source_repo_root, path=artifact_dir)
        if self._is_git_repo(source_repo_root) and not self._is_git_dirty(
            source_repo_root,
            artifact_relative=artifact_relative,
        ):
            return self._prepare_git_clone(
                source_repo_root=source_repo_root,
                workspace_root=workspace_root,
            )

        workspace_root.mkdir(parents=True, exist_ok=False)
        copied_file_count = 0
        skipped_entry_count = 0
        total_bytes = 0
        skipped_entries: list[str] = []

        for current_root, dirnames, filenames in os.walk(source_repo_root):
            current_path = Path(current_root)
            relative_dir = current_path.relative_to(source_repo_root)
            filtered_dirs: list[str] = []
            for dirname in dirnames:
                rel_dir = _join_relative(relative_dir, dirname)
                if self._should_skip_dir(rel_dir, artifact_relative):
                    skipped_entry_count += 1
                    skipped_entries.append(f"{rel_dir.as_posix()}/")
                    continue
                filtered_dirs.append(dirname)
            dirnames[:] = filtered_dirs

            destination_dir = workspace_root / relative_dir
            destination_dir.mkdir(parents=True, exist_ok=True)

            for filename in filenames:
                rel_file = _join_relative(relative_dir, filename)
                source_file = current_path / filename
                if source_file.is_symlink():
                    skipped_entry_count += 1
                    skipped_entries.append(f"{rel_file.as_posix()} (symlink)")
                    continue
                size = source_file.stat().st_size
                if size > self._max_file_bytes:
                    skipped_entry_count += 1
                    skipped_entries.append(f"{rel_file.as_posix()} (oversized)")
                    continue
                destination_file = destination_dir / filename
                shutil.copy2(source_file, destination_file)
                copied_file_count += 1
                total_bytes += size

        return LocalSandboxMaterialization(
            workspace_root=workspace_root,
            copied_file_count=copied_file_count,
            skipped_entry_count=skipped_entry_count,
            total_bytes=total_bytes,
            skipped_entries=skipped_entries,
            materialization_strategy="copy",
        )

    def cleanup(self, *, workspace_root: Path) -> None:
        target = workspace_root.resolve()
        if target.exists():
            shutil.rmtree(target)

    def _prepare_git_clone(
        self,
        *,
        source_repo_root: Path,
        workspace_root: Path,
    ) -> LocalSandboxMaterialization:
        source_branch = self._git_output(source_repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]) or None
        source_head_sha = self._git_output(source_repo_root, ["rev-parse", "HEAD"]) or None
        self._run_git(
            None,
            ["clone", "--quiet", "--no-hardlinks", str(source_repo_root), str(workspace_root)],
            check=True,
        )
        self._sync_remotes(source_repo_root=source_repo_root, workspace_root=workspace_root)
        self._sync_identity(source_repo_root=source_repo_root, workspace_root=workspace_root)
        copied_file_count, total_bytes = self._workspace_stats(workspace_root)
        return LocalSandboxMaterialization(
            workspace_root=workspace_root,
            copied_file_count=copied_file_count,
            skipped_entry_count=0,
            total_bytes=total_bytes,
            skipped_entries=[],
            materialization_strategy="git_clone",
            source_branch=None if source_branch == "HEAD" else source_branch,
            source_head_sha=source_head_sha,
        )

    def _should_skip_dir(self, relative_dir: Path, artifact_relative: Path | None) -> bool:
        if relative_dir.name in self._ignored_dir_names:
            return True
        if artifact_relative is not None:
            try:
                relative_dir.relative_to(artifact_relative)
                return True
            except ValueError:
                pass
        return False

    def _workspace_stats(self, workspace_root: Path) -> tuple[int, int]:
        count = 0
        total_bytes = 0
        ignored_dirs = set(self._ignored_dir_names)
        ignored_dirs.add(".git")
        for path in workspace_root.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = path.relative_to(workspace_root).parts
            if any(part in ignored_dirs for part in relative_parts):
                continue
            count += 1
            total_bytes += path.stat().st_size
        return count, total_bytes

    def _is_git_repo(self, root: Path) -> bool:
        completed = self._run_git(root, ["rev-parse", "--is-inside-work-tree"], check=False)
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    def _is_git_dirty(self, root: Path, *, artifact_relative: Path | None) -> bool:
        for line in self._git_lines(root, ["status", "--short"]):
            path_text = line[3:].strip()
            if " -> " in path_text:
                path_text = path_text.split(" -> ", maxsplit=1)[1].strip()
            relative_path = Path(path_text)
            if artifact_relative is not None:
                try:
                    relative_path.relative_to(artifact_relative)
                    continue
                except ValueError:
                    pass
            if relative_path.parts and relative_path.parts[0] in self._ignored_dir_names:
                continue
            return True
        return False

    def _sync_remotes(self, *, source_repo_root: Path, workspace_root: Path) -> None:
        workspace_remotes = self._git_lines(workspace_root, ["remote"])
        for remote in workspace_remotes:
            self._run_git(workspace_root, ["remote", "remove", remote], check=True)
        for remote in self._git_lines(source_repo_root, ["remote"]):
            remote_url = self._git_output(source_repo_root, ["remote", "get-url", remote])
            if remote_url:
                self._run_git(workspace_root, ["remote", "add", remote, remote_url], check=True)

    def _sync_identity(self, *, source_repo_root: Path, workspace_root: Path) -> None:
        for key in ("user.name", "user.email"):
            value = self._git_output(source_repo_root, ["config", "--get", key])
            if value:
                self._run_git(workspace_root, ["config", key, value], check=True)

    def _git_lines(self, root: Path, args: list[str]) -> list[str]:
        output = self._git_output(root, args)
        return [line for line in output.splitlines() if line.strip()]

    def _git_output(self, root: Path, args: list[str]) -> str:
        completed = self._run_git(root, args, check=False)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def _run_git(
        self,
        cwd: Path | None,
        args: list[str],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            check=check,
            text=True,
        )


def _relative_to(*, source_repo_root: Path, path: Path) -> Path | None:
    try:
        return path.relative_to(source_repo_root)
    except ValueError:
        return None


def _join_relative(base: Path, name: str) -> Path:
    if str(base) == ".":
        return Path(name)
    return base / name
