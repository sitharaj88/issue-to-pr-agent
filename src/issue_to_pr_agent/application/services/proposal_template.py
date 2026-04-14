from __future__ import annotations


class ProposalTemplateBuilder:
    def build(self, *, run_id: str, payload: dict[str, object]) -> dict[str, object]:
        plan = payload.get("plan", {})
        planning_context = payload.get("planning_context", {})

        files_to_inspect = (
            plan.get("files_to_inspect", []) if isinstance(plan, dict) else []
        )
        ranked_files = (
            planning_context.get("ranked_files", []) if isinstance(planning_context, dict) else []
        )
        ranked_paths = [
            item.get("path")
            for item in ranked_files
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        allowed_existing_paths = _dedupe(
            [path for path in files_to_inspect if isinstance(path, str)] + ranked_paths
        )
        suggested_new_dirs = _dedupe(
            sorted({path.rsplit("/", 1)[0] for path in allowed_existing_paths if "/" in path})
        )

        return {
            "proposal_id": f"{run_id}-proposal",
            "linked_run_id": run_id,
            "summary": plan.get("summary", "Patch proposal"),
            "rationale": "Fill in operations after reviewing the planning context and target files.",
            "allowed_existing_paths": allowed_existing_paths,
            "suggested_new_file_directories": suggested_new_dirs,
            "operations": [],
            "examples": [
                {
                    "type": "replace_text",
                    "path": allowed_existing_paths[0] if allowed_existing_paths else "src/example.py",
                    "find_text": "old text",
                    "replace_text": "new text",
                },
                {
                    "type": "append_text",
                    "path": allowed_existing_paths[0] if allowed_existing_paths else "tests/test_example.py",
                    "content": "\n# extra assertion\n",
                },
                {
                    "type": "write_file",
                    "path": "tests/test_new_case.py",
                    "content": "def test_placeholder():\n    assert True\n",
                    "allow_overwrite": False,
                },
            ],
        }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
