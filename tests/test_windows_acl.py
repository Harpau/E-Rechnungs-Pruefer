from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app import windows_acl
from app.windows_acl import WindowsServiceAcl


class _FakeSecurity:
    @staticmethod
    def LookupAccountName(_system, _account):
        return "S-1-5-32-545", "BUILTIN", 4

    @staticmethod
    def ConvertSidToStringSid(sid):
        return sid

    @staticmethod
    def ConvertStringSidToSid(sid):
        return sid


def test_broad_reader_alias_is_rejected_before_token_acl_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    acl = WindowsServiceAcl()
    set_acl = Mock()
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (_FakeSecurity(), object()))
    monkeypatch.setattr(acl, "_set", set_acl)

    with pytest.raises(ValueError, match="Breite lokale Gruppen"):
        acl.grant_token_reader(Path("api-token.txt"), "Lokalisierter-Benutzer-Alias")

    set_acl.assert_not_called()


def test_token_rotation_protector_preserves_only_verified_reader_sids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acl = WindowsServiceAcl()
    verify = Mock()
    set_acl = Mock()
    monkeypatch.setattr(acl, "_verify", verify)
    monkeypatch.setattr(acl, "_additional_reader_sids", lambda _path: {"S-1-5-21-123-456"})
    monkeypatch.setattr(acl, "_set", set_acl)
    token = Path("api-token.txt")

    protector = acl.token_protector_preserving(token)
    protector(Path(".api-token.tmp"))

    verify.assert_called_once_with(token, allow_readers=True)
    set_acl.assert_called_once_with(
        Path(".api-token.tmp"),
        directory=False,
        reader_sids=("S-1-5-21-123-456",),
    )
