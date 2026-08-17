from __future__ import annotations

import importlib.util
import re
import shlex
import tomllib
from pathlib import Path

import pytest

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
    versions = module.collect_versions()
    current_version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert module._read_start_here_version() == current_version
    assert versions["START_HERE.txt"] == current_version
    assert module.verify() == current_version


def test_changelog_has_one_exact_dated_heading_for_the_current_version():
    module = _load_script("verify_version.py")
    content = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    current_version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    release_date = module._changelog_release_date(content, current_version)

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date)


@pytest.mark.parametrize(
    "heading",
    [
        "## 2.0.0 - 2026-08-03",
        "## 2.0.0 – 03.08.2026",
        "## 2.0.0 – 2026-02-30",
        "## 1.5.0 – 2026-08-03",
    ],
)
def test_changelog_rejects_missing_malformed_or_invalid_current_release_heading(heading: str):
    module = _load_script("verify_version.py")
    content = f"# Änderungsprotokoll\n\n## Unveröffentlicht\n\n{heading}\n"

    with pytest.raises(ValueError, match="CHANGELOG.md"):
        module._changelog_release_date(content, "2.0.0")


def test_release_filter_excludes_local_and_sensitive_files():
    module = _load_script("build_release.py")

    assert module.should_include(Path("app/examples/cii-rechnung-demo.xml")) is True
    assert module.should_include(Path("tests/fixtures/cii-category-o.xml")) is True
    assert module.should_include(Path("customer-invoice.xml")) is False
    assert module.should_include(Path("invoice.pdf")) is False
    assert module.should_include(Path("secret.key")) is False
    assert module.should_include(Path(".env")) is False
    assert module.should_include(Path(".env.local")) is False
    assert module.should_include(Path("config/.env.production")) is False
    assert module.should_include(Path("config/.env.example.backup")) is False
    assert module.should_include(Path(".env.example")) is True
    assert module.should_include(Path("config/.env.example")) is True
    assert module.should_include(Path("vendor/kosit/validator.jar")) is False
    assert module.should_include(Path("runtime/java/bin/java.exe")) is False
    assert module.should_include(Path(".cache/windows-components/java.zip")) is False
    assert module.should_include(Path("e_rechnung_pruefer.egg-info/PKG-INFO")) is False
    assert module.should_include(Path("app/main.py")) is True


def test_clean_target_limits_bytecode_cleanup_to_owned_code_roots():
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    clean_recipe = makefile.partition("\nclean:\n")[2]

    assert clean_recipe
    pycache_commands = [line.strip() for line in clean_recipe.splitlines() if "-name __pycache__" in line]
    assert len(pycache_commands) == 1

    arguments = shlex.split(pycache_commands[0])
    type_index = arguments.index("-type")
    assert arguments[0] == "find"
    assert arguments[1:type_index] == ["app", "tests", "scripts", "packaging"]
    assert not {".", ".venv", "venv", "vendor", "local-data"} & set(arguments[1:type_index])


def test_python_and_windows_package_manifests_include_required_runtime_files():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "presentation_contract.json" in pyproject["tool"]["setuptools"]["package-data"]["app"]
    for expected in (
        "include app/presentation_contract.json",
        "include packaging/kosit/components.lock.json",
        "recursive-include docs/acceptance-templates *.json",
        "recursive-include docs/examples *.json",
        "recursive-include tests *.mjs",
    ):
        assert expected in manifest

    contract_data = '(str(PROJECT_ROOT / "app" / "presentation_contract.json"), "app")'
    for relative in (
        "packaging/windows/e_rechnungs_pruefer.spec",
        "packaging/windows/e_rechnungs_pruefer_service.spec",
    ):
        assert contract_data in (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_repository_release_artifact_inventory_contains_required_runtime_and_support_files():
    module = _load_script("build_release.py")
    names = {path.relative_to(PROJECT_ROOT).as_posix() for path in module.repository_files()}

    assert {
        "app/presentation_contract.json",
        "packaging/kosit/components.lock.json",
        "docs/acceptance-templates/acceptance-plan.json",
        "docs/acceptance-templates/ui-task.json",
        "docs/examples/node-red-e-rechnungs-pruefer-flow.json",
        "tests/node_red_flow.test.mjs",
        "tests/frontend/test_app_schema_v2.mjs",
    } <= names


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
    assert "python -m pip_audit --strict --disable-pip --require-hashes" in workflow
    assert "-r packaging/windows/requirements-release.txt" in workflow
    assert "--ignore-vuln" not in workflow


def test_github_release_notes_explicitly_document_the_schema_2_breaking_change():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    create_release = workflow[
        workflow.index('gh release create "${GITHUB_REF_NAME}"') : workflow.index(
            'gh release upload "${GITHUB_REF_NAME}"'
        )
    ]

    assert "--generate-notes" in create_release
    assert "--notes" in create_release
    assert "Breaking Change" in create_release
    assert "Analyseschema 2" in create_release
    assert (
        "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/blob/${GITHUB_REF_NAME}/docs/API_MIGRATION_V2.md" in create_release
    )
    assert "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/blob/${GITHUB_REF_NAME}/CHANGELOG.md" in create_release
