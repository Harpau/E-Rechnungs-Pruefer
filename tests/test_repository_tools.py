from __future__ import annotations

import importlib.util
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = PROJECT_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_declarations_are_synchronized():
    module = _load_script("verify_version.py")
    assert module.verify() == "1.4.0"


def test_release_filter_excludes_local_and_sensitive_files():
    module = _load_script("build_release.py")

    assert module.should_include(Path("app/examples/cii-rechnung-demo.xml")) is True
    assert module.should_include(Path("tests/fixtures/cii-category-o.xml")) is True
    assert module.should_include(Path("customer-invoice.xml")) is False
    assert module.should_include(Path("invoice.pdf")) is False
    assert module.should_include(Path("secret.key")) is False
    assert module.should_include(Path(".env")) is False
    assert module.should_include(Path("vendor/kosit/validator.jar")) is False
    assert module.should_include(Path("runtime/java/bin/java.exe")) is False
    assert module.should_include(Path(".cache/windows-components/java.zip")) is False
    assert module.should_include(Path("e_rechnung_pruefer.egg-info/PKG-INFO")) is False
    assert module.should_include(Path("app/main.py")) is True


def test_github_actions_are_pinned_to_commit_shas():
    workflow_root = PROJECT_ROOT / ".github" / "workflows"
    action_reference = re.compile(r"^\s*-?\s*uses:\s*(?P<action>[^@\s]+)@(?P<ref>[^\s#]+)", re.MULTILINE)

    references: list[tuple[Path, str, str]] = []
    for workflow in sorted(workflow_root.glob("*.yml")):
        content = workflow.read_text(encoding="utf-8")
        checkout_count = content.count("uses: actions/checkout@")
        assert content.count("persist-credentials: false") == checkout_count
        references.extend(
            (workflow, match.group("action"), match.group("ref")) for match in action_reference.finditer(content)
        )

    assert references
    unpinned = [
        f"{workflow.relative_to(PROJECT_ROOT)}: {action}@{reference}"
        for workflow, action, reference in references
        if not re.fullmatch(r"[0-9a-f]{40}", reference)
    ]
    assert unpinned == []


def test_dependency_audit_uses_strict_local_project_mode():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "dependency-audit.yml").read_text(encoding="utf-8")

    assert "python -m pip_audit --strict ." in workflow
    assert "--ignore-vuln" not in workflow
