from __future__ import annotations

from collections import Counter
import re
from pathlib import Path

from ...application.services.evaluation import PlanningEvaluator
from ...domain.entities import (
    AgentPlan,
    EvaluationScore,
    IndexedSymbol,
    IssueContext,
    PlanningContext,
    RankedFile,
    RepoSnapshot,
    RepositoryIndex,
    RepositoryProfile,
)
from .base import ContextBuilder

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "bug",
    "but",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "this",
    "to",
    "when",
    "with",
}

_LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
}

_TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".cfg",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".sh",
}

_PYTHON_SYMBOL_RE = re.compile(r"^\s*(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:function|class|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


class RepositoryContextBuilder(ContextBuilder):
    def __init__(
        self,
        *,
        max_ranked_files: int = 8,
        snippet_line_limit: int = 12,
        max_preview_bytes: int = 24_000,
        max_indexed_symbols: int = 40,
    ) -> None:
        self._max_ranked_files = max_ranked_files
        self._snippet_line_limit = snippet_line_limit
        self._max_preview_bytes = max_preview_bytes
        self._max_indexed_symbols = max_indexed_symbols
        self._evaluator = PlanningEvaluator()

    def build(
        self,
        issue: IssueContext,
        repo_snapshot: RepoSnapshot,
        objective: str | None = None,
    ) -> PlanningContext:
        keywords = _extract_keywords(issue, objective)
        profile = self._detect_repository_profile(repo_snapshot)
        repository_index = self._build_repository_index(repo_snapshot, keywords)
        ranked_files = self._rank_files(repo_snapshot, keywords, profile, repository_index)
        summary = self._build_summary(repo_snapshot, keywords, profile, repository_index, ranked_files)
        provisional_context = PlanningContext(
            summary=summary,
            issue_keywords=keywords,
            repository_profile=profile,
            repository_index=repository_index,
            ranked_files=ranked_files,
            suggested_test_commands=profile.test_commands,
        )
        evaluation = self._evaluator.evaluate(
            planning_context=provisional_context,
            plan=_context_bootstrap_plan(ranked_files=ranked_files, profile=profile),
        )
        return PlanningContext(
            summary=summary,
            issue_keywords=keywords,
            repository_profile=profile,
            repository_index=repository_index,
            evaluation=evaluation,
            ranked_files=ranked_files,
            suggested_test_commands=profile.test_commands,
        )

    def _detect_repository_profile(self, repo_snapshot: RepoSnapshot) -> RepositoryProfile:
        tracked_files = repo_snapshot.tracked_files
        file_set = set(tracked_files)
        extension_counts = Counter(
            _LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower())
            for path in tracked_files
            if _LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower())
        )
        detected_languages = [name for name, _ in extension_counts.most_common()]
        primary_language = detected_languages[0] if detected_languages else "unknown"

        frameworks: set[str] = set()
        build_systems: set[str] = set()
        test_commands: list[str] = []

        pyproject_text = self._read_if_present(repo_snapshot.root, "pyproject.toml")
        package_json_text = self._read_if_present(repo_snapshot.root, "package.json")

        if "pyproject.toml" in file_set:
            build_systems.add("pyproject")
        if "package.json" in file_set:
            build_systems.add("npm")
        if "Cargo.toml" in file_set:
            build_systems.add("cargo")
        if "go.mod" in file_set:
            build_systems.add("go")

        if pyproject_text:
            lowered = pyproject_text.lower()
            if "fastapi" in lowered:
                frameworks.add("fastapi")
            if "django" in lowered:
                frameworks.add("django")
            if "flask" in lowered:
                frameworks.add("flask")
            if "pytest" in lowered:
                frameworks.add("pytest")
        if package_json_text:
            lowered = package_json_text.lower()
            if "\"react\"" in lowered:
                frameworks.add("react")
            if "\"next\"" in lowered or "\"next\":" in lowered:
                frameworks.add("nextjs")
            if "\"jest\"" in lowered:
                frameworks.add("jest")

        if primary_language == "python":
            if any(
                path.startswith("tests/") or path.endswith("_test.py") or "test_" in Path(path).name
                for path in tracked_files
            ):
                test_commands.append("python3 -m unittest discover -s tests -v")
            if "pytest" in frameworks or "pytest.ini" in file_set or any("conftest.py" in path for path in tracked_files):
                test_commands.insert(0, "python3 -m pytest")
        if primary_language in {"javascript", "typescript"} and "package.json" in file_set:
            test_commands.append("npm test")
        if primary_language == "go" and "go.mod" in file_set:
            test_commands.append("go test ./...")
        if primary_language == "rust" and "Cargo.toml" in file_set:
            test_commands.append("cargo test")

        if not test_commands and any(path.startswith("tests/") for path in tracked_files):
            test_commands.append("python3 -m unittest discover -s tests -v")

        return RepositoryProfile(
            primary_language=primary_language,
            detected_languages=detected_languages,
            detected_frameworks=sorted(frameworks),
            build_systems=sorted(build_systems),
            test_commands=_dedupe(test_commands),
        )

    def _build_repository_index(
        self,
        repo_snapshot: RepoSnapshot,
        keywords: list[str],
    ) -> RepositoryIndex:
        symbols: list[IndexedSymbol] = []
        files_indexed = 0
        for path in repo_snapshot.tracked_files:
            target = repo_snapshot.root / path
            if not target.exists() or not target.is_file() or target.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            extracted = _extract_symbols(path, content)
            if extracted:
                files_indexed += 1
                symbols.extend(extracted)
        symbols.sort(
            key=lambda item: (
                -_symbol_priority(item, keywords),
                len(item.path),
                item.path,
                item.line,
            )
        )
        complexity_score = min(
            100,
            (len(repo_snapshot.tracked_files) // 8)
            + (len(symbols) // 4)
            + len({Path(item.path).suffix.lower() for item in symbols}) * 4,
        )
        return RepositoryIndex(
            files_indexed=files_indexed,
            symbol_count=len(symbols),
            top_symbols=symbols[: self._max_indexed_symbols],
            complexity_score=complexity_score,
            index_version="v2",
        )

    def _rank_files(
        self,
        repo_snapshot: RepoSnapshot,
        keywords: list[str],
        profile: RepositoryProfile,
        repository_index: RepositoryIndex,
    ) -> list[RankedFile]:
        scored: list[RankedFile] = []
        for path in repo_snapshot.tracked_files:
            score, reasons = self._score_path(path, keywords, profile, repository_index)
            if score <= 0:
                continue
            scored.append(
                RankedFile(
                    path=path,
                    score=score,
                    reasons=reasons,
                    preview=self._preview_file(repo_snapshot.root / path),
                )
            )

        if not scored:
            fallback = repo_snapshot.tracked_files[: self._max_ranked_files]
            scored = [
                RankedFile(
                    path=path,
                    score=1,
                    reasons=["fallback candidate from repository snapshot"],
                    preview=self._preview_file(repo_snapshot.root / path),
                )
                for path in fallback
            ]

        scored.sort(key=lambda item: (-item.score, len(item.path), item.path))
        return scored[: self._max_ranked_files]

    def _score_path(
        self,
        path: str,
        keywords: list[str],
        profile: RepositoryProfile,
        repository_index: RepositoryIndex,
    ) -> tuple[int, list[str]]:
        lowered = path.lower()
        filename = Path(path).name.lower()
        score = 0
        reasons: list[str] = []

        for keyword in keywords:
            if keyword in lowered:
                score += 8
                reasons.append(f"matches issue keyword '{keyword}'")

        matching_symbols = [
            item.name
            for item in repository_index.top_symbols
            if item.path == path and any(keyword in item.name.lower() for keyword in keywords)
        ]
        if matching_symbols:
            score += min(10, len(matching_symbols) * 3)
            reasons.append("matches indexed symbol(s): " + ", ".join(sorted(set(matching_symbols))[:3]))

        suffix = Path(path).suffix.lower()
        if suffix and _LANGUAGE_BY_EXTENSION.get(suffix) == profile.primary_language:
            score += 2
            reasons.append(f"matches primary language '{profile.primary_language}'")

        if any(part in lowered for part in ("src/", "app/", "issue_to_pr_agent/")):
            score += 3
            reasons.append("lives in application source path")

        if any(part in lowered for part in ("tests/", "/test_", "_test.", "/spec")):
            if any(keyword in {"test", "tests", "pytest", "coverage"} for keyword in keywords):
                score += 2
                reasons.append("appears to be test coverage")

        if filename in {"pyproject.toml", "package.json", "go.mod", "cargo.toml", "readme.md"}:
            score += 1
            reasons.append("repository control file")

        return score, _dedupe(reasons)

    def _build_summary(
        self,
        repo_snapshot: RepoSnapshot,
        keywords: list[str],
        profile: RepositoryProfile,
        repository_index: RepositoryIndex,
        ranked_files: list[RankedFile],
    ) -> str:
        top_files = ", ".join(item.path for item in ranked_files[:3]) or "none"
        languages = ", ".join(profile.detected_languages[:3]) or "unknown"
        frameworks = ", ".join(profile.detected_frameworks[:3]) or "none"
        keywords_text = ", ".join(keywords[:6]) or "none"
        top_symbols = ", ".join(item.name for item in repository_index.top_symbols[:5]) or "none"
        return (
            f"Profiled {len(repo_snapshot.tracked_files)} files. "
            f"Primary language: {profile.primary_language}. "
            f"Detected languages: {languages}. "
            f"Frameworks: {frameworks}. "
            f"Issue keywords: {keywords_text}. "
            f"Indexed symbols: {repository_index.symbol_count} across {repository_index.files_indexed} files. "
            f"Top symbols: {top_symbols}. "
            f"Complexity score: {repository_index.complexity_score}. "
            f"Top candidate files: {top_files}."
        )

    def _preview_file(self, path: Path) -> str:
        if path.suffix.lower() not in _TEXT_EXTENSIONS or not path.is_file():
            return ""
        try:
            if path.stat().st_size > self._max_preview_bytes:
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = text.splitlines()[: self._snippet_line_limit]
        return "\n".join(lines).strip()

    def _read_if_present(self, root: Path, relative_path: str) -> str:
        path = root / relative_path
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _extract_keywords(issue: IssueContext, objective: str | None) -> list[str]:
    text = " ".join(part for part in (issue.title, issue.body, objective or "") if part)
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_\\-]{2,}", text.lower())
    filtered = [token for token in tokens if token not in _STOP_WORDS]
    counts = Counter(filtered)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [token for token, _ in ranked[:10]]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _extract_symbols(path: str, content: str) -> list[IndexedSymbol]:
    suffix = Path(path).suffix.lower()
    symbols: list[IndexedSymbol] = []
    if suffix == ".py":
        for match in _PYTHON_SYMBOL_RE.finditer(content):
            symbols.append(
                IndexedSymbol(
                    name=match.group(2),
                    kind=match.group(1),
                    path=path,
                    line=content[: match.start()].count("\n") + 1,
                    signature=match.group(0).strip(),
                )
            )
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for match in _JS_SYMBOL_RE.finditer(content):
            symbols.append(
                IndexedSymbol(
                    name=match.group(1),
                    kind="symbol",
                    path=path,
                    line=content[: match.start()].count("\n") + 1,
                    signature=match.group(0).strip(),
                )
            )
    return symbols


def _symbol_priority(symbol: IndexedSymbol, keywords: list[str]) -> int:
    score = 1
    lowered = symbol.name.lower()
    for keyword in keywords:
        if keyword in lowered:
            score += 5
    if symbol.kind == "class":
        score += 1
    return score


def _context_bootstrap_plan(*, ranked_files: list[RankedFile], profile: RepositoryProfile) -> AgentPlan:
    return AgentPlan(
        summary="Repository context bootstrap",
        files_to_inspect=[item.path for item in ranked_files[:5]],
        tests=profile.test_commands[:2],
    )
