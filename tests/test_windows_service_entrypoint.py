from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = PROJECT_ROOT / "packaging" / "windows" / "service_entrypoint.py"


def _load_entrypoint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_service_entrypoint_test", ENTRYPOINT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entrypoint_explains_direct_start_only_for_the_dedicated_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    main = Mock(return_value=entrypoint.DIRECT_START_EXIT_CODE)
    notice = Mock()
    monkeypatch.setattr(entrypoint, "main", main)
    monkeypatch.setattr(entrypoint, "_show_direct_start_notice", notice)

    assert entrypoint._run([]) == entrypoint.DIRECT_START_EXIT_CODE
    main.assert_called_once_with([])
    notice.assert_called_once_with()

    main.reset_mock()
    notice.reset_mock()
    main.return_value = 0

    assert entrypoint._run(["--health-check"]) == 0
    main.assert_called_once_with(["--health-check"])
    notice.assert_not_called()


@pytest.mark.parametrize(("session_id", "expected_calls"), [(None, 0), (0, 0), (1, 1), (7, 1)])
def test_direct_start_notice_never_opens_ui_in_session_zero(
    monkeypatch: pytest.MonkeyPatch,
    session_id: int | None,
    expected_calls: int,
) -> None:
    entrypoint = _load_entrypoint()
    display = Mock()
    monkeypatch.setattr(entrypoint, "_current_process_session_id", Mock(return_value=session_id))
    monkeypatch.setattr(entrypoint, "_display_direct_start_message", display)

    entrypoint._show_direct_start_notice()

    assert display.call_count == expected_calls


def test_direct_start_notice_is_german_and_points_to_the_open_client() -> None:
    entrypoint = _load_entrypoint()

    assert "kann nicht direkt ausgeführt werden" in entrypoint._DIRECT_START_MESSAGE
    assert "E-Rechnungs-Pruefer-Oeffnen.exe" in entrypoint._DIRECT_START_MESSAGE
    assert entrypoint._DIRECT_START_TITLE == "E-Rechnungs-Prüfer Dienst"
