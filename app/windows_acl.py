from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .windows_service_config import (
    BASE_TOKEN_PRINCIPALS,
    SERVICE_SID,
    SERVICE_SID_ACCOUNT,
    ServicePaths,
    validate_machine_path,
)

FORBIDDEN_BROAD_SIDS = frozenset(
    {
        "S-1-1-0",  # Everyone
        "S-1-2-0",  # Local
        "S-1-2-1",  # Console logon
        "S-1-3-4",  # Owner rights
        "S-1-5-4",  # Interactive
        "S-1-5-6",  # All services
        "S-1-5-11",  # Authenticated users
        "S-1-5-13",  # Terminal Server users
        "S-1-5-19",  # All LocalService processes
        "S-1-5-20",  # All NetworkService processes
        "S-1-5-32-545",  # Built-in users
        "S-1-5-32-546",  # Built-in guests
        "S-1-5-32-547",  # Power users
        "S-1-5-32-555",  # Remote Desktop users
    }
)
ALLOWED_TOKEN_READER_SID_TYPES = frozenset({1, 9})  # user or computer/gMSA
WELL_KNOWN_ACCOUNT_SIDS = {
    "SYSTEM": "S-1-5-18",
    r"BUILTIN\Administrators": "S-1-5-32-544",
    SERVICE_SID_ACCOUNT: SERVICE_SID,
}
SYSTEM_SID = WELL_KNOWN_ACCOUNT_SIDS["SYSTEM"]
ADMINISTRATORS_SID = WELL_KNOWN_ACCOUNT_SIDS[r"BUILTIN\Administrators"]
LOCAL_SERVICE_SID = "S-1-5-19"
OWNER_RIGHTS_SID = "S-1-3-4"
OWNER_RIGHTS_READ_CONTROL = 0x00020000


def _windows_modules() -> tuple[Any, Any]:
    if sys.platform != "win32":
        raise OSError("Windows-DACLs können nur unter Windows gesetzt werden.")
    try:
        import ntsecuritycon
        import win32security
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; die Dienst-DACL kann nicht sicher gesetzt werden.") from exc
    return win32security, ntsecuritycon


def _windows_directory_modules() -> tuple[Any, Any]:
    if sys.platform != "win32":
        raise OSError("Geschützte Windows-Verzeichnisse können nur unter Windows erstellt werden.")
    try:
        import pywintypes
        import win32file
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; das Dienstverzeichnis kann nicht sicher erstellt werden.") from exc
    return pywintypes, win32file


class WindowsServiceAcl:
    """Create and verify protected, service-specific ProgramData DACLs."""

    def __init__(self, *, token_readers: tuple[str, ...] = (), administrative: bool = False) -> None:
        forbidden_names = {"everyone", "users", "authenticated users"}
        if any(reader.strip().casefold() in forbidden_names for reader in token_readers):
            raise ValueError("Breite lokale Gruppen dürfen das Diensttoken nicht lesen.")
        self.token_readers = token_readers
        self.administrative = administrative

    @staticmethod
    def _lookup(account: str, win32security: Any) -> Any:
        fixed_sid = WELL_KNOWN_ACCOUNT_SIDS.get(account)
        if fixed_sid is not None:
            return win32security.ConvertStringSidToSid(fixed_sid)
        try:
            sid, _domain, _kind = win32security.LookupAccountName(None, account)
        except Exception as exc:
            raise RuntimeError(f"Die Windows-Identität {account!r} konnte nicht aufgelöst werden.") from exc
        return sid

    @staticmethod
    def _reader_sid_is_specific(sid_text: str, sid_type: int) -> bool:
        return sid_type in ALLOWED_TOKEN_READER_SID_TYPES or (sid_type == 5 and sid_text.startswith("S-1-5-80-"))

    @staticmethod
    def _specific_reader(account: str, win32security: Any) -> Any:
        try:
            sid, _domain, sid_type = win32security.LookupAccountName(None, account)
        except Exception as exc:
            raise RuntimeError(f"Die Windows-Identität {account!r} konnte nicht aufgelöst werden.") from exc
        sid_text = win32security.ConvertSidToStringSid(sid)
        if sid_text in FORBIDDEN_BROAD_SIDS or not WindowsServiceAcl._reader_sid_is_specific(sid_text, int(sid_type)):
            raise ValueError("Breite lokale Gruppen und andere Sammelidentitäten dürfen das Diensttoken nicht lesen.")
        return sid

    @staticmethod
    def _validate_reader_sid(sid: Any, win32security: Any) -> str:
        sid_text = win32security.ConvertSidToStringSid(sid)
        if sid_text in FORBIDDEN_BROAD_SIDS:
            raise RuntimeError("Die DACL gewährt einer zu breiten Windows-Identität Tokenzugriff.")
        try:
            _name, _domain, sid_type = win32security.LookupAccountSid(None, sid)
        except Exception as exc:
            raise RuntimeError("Eine provisionierte Token-Leseidentität kann nicht mehr aufgelöst werden.") from exc
        if not WindowsServiceAcl._reader_sid_is_specific(sid_text, int(sid_type)):
            raise RuntimeError("Die DACL gewährt einer Windows-Gruppe statt einer konkreten Identität Tokenzugriff.")
        return sid_text

    @staticmethod
    def _current_elevated_administrator_sid(win32security: Any) -> str:
        try:
            import win32api
            import win32con

            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                elevated = bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
                administrator = win32security.ConvertStringSidToSid(ADMINISTRATORS_SID)
                is_administrator = bool(win32security.CheckTokenMembership(None, administrator))
                token_user = win32security.GetTokenInformation(token, win32security.TokenUser)
                user_sid = token_user[0]
            finally:
                token.Close()
        except Exception as exc:
            raise RuntimeError("Die erhöhte Windows-Administratoridentität konnte nicht geprüft werden.") from exc
        if not elevated or not is_administrator:
            raise RuntimeError("Die Dienstverwaltung benötigt eine aktuell erhöhte Administratoridentität.")
        return win32security.ConvertSidToStringSid(user_sid)

    @staticmethod
    def _direct_local_administrator_sids(win32security: Any) -> frozenset[str]:
        """Return concrete users that are direct members of local Administrators."""

        if sys.platform != "win32":
            raise OSError("Lokale Windows-Administratoren können nur unter Windows geprüft werden.")
        try:
            import win32net

            administrators_sid = win32security.ConvertStringSidToSid(ADMINISTRATORS_SID)
            group_name, _domain, _sid_type = win32security.LookupAccountSid(None, administrators_sid)
            resume_handle = 0
            seen_resume_handles: set[int] = set()
            direct_users: set[str] = set()
            for _page in range(128):
                records, _total, next_resume_handle = win32net.NetLocalGroupGetMembers(
                    None,
                    group_name,
                    1,
                    resume_handle,
                    65_536,
                )
                if not isinstance(records, (list, tuple)):
                    raise RuntimeError("Die lokale Administratorgruppe lieferte ein unbekanntes Ergebnis.")
                for record in records:
                    if not isinstance(record, dict) or "sid" not in record or "sidusage" not in record:
                        raise RuntimeError("Ein Mitglied der lokalen Administratorgruppe ist unvollständig.")
                    if int(record["sidusage"]) == 1:  # SidTypeUser
                        direct_users.add(win32security.ConvertSidToStringSid(record["sid"]))
                next_resume_handle = int(next_resume_handle or 0)
                if next_resume_handle == 0:
                    return frozenset(direct_users)
                if next_resume_handle in seen_resume_handles:
                    raise RuntimeError("Die lokale Administratorgruppe konnte nicht vollständig gelesen werden.")
                seen_resume_handles.add(next_resume_handle)
                resume_handle = next_resume_handle
        except Exception as exc:
            raise RuntimeError(
                "Die direkten Benutzer der lokalen Administratorgruppe konnten nicht sicher geprüft werden."
            ) from exc
        raise RuntimeError(
            "Die lokale Administratorgruppe enthält unerwartet viele nicht abgeschlossene Ergebnisseiten."
        )

    def _owner_is_trusted(
        self,
        owner_sid: str,
        win32security: Any,
        *,
        allow_local_service_owner: bool,
    ) -> bool:
        if owner_sid in {SYSTEM_SID, ADMINISTRATORS_SID}:
            return True
        if allow_local_service_owner and owner_sid == LOCAL_SERVICE_SID:
            return True
        return self.administrative and owner_sid == self._current_elevated_administrator_sid(win32security)

    def _validate_owner(
        self,
        path: Path,
        win32security: Any,
        *,
        allow_local_service_owner: bool,
        require_administrators: bool = False,
    ) -> None:
        owner_information = getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001)
        try:
            descriptor = win32security.GetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                owner_information,
            )
            owner = descriptor.GetSecurityDescriptorOwner()
            owner_sid = win32security.ConvertSidToStringSid(owner)
        except Exception as exc:
            raise RuntimeError(f"Der Besitzer konnte für {path} nicht sicher geprüft werden.") from exc
        if require_administrators:
            trusted = owner_sid == ADMINISTRATORS_SID
        else:
            trusted = self._owner_is_trusted(
                owner_sid,
                win32security,
                allow_local_service_owner=allow_local_service_owner,
            )
        if not trusted:
            raise RuntimeError(f"Der Maschinenpfad {path} besitzt keine vertrauenswürdige Windows-Identität.")

    def _access_control_list(
        self,
        win32security: Any,
        ntsecuritycon: Any,
        *,
        directory: bool,
        readers: tuple[str, ...] = (),
        reader_sids: tuple[str, ...] = (),
        allow_local_service_owner: bool = False,
    ) -> Any:
        dacl = win32security.ACL()
        inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
        for account in BASE_TOKEN_PRINCIPALS:
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                inheritance,
                ntsecuritycon.FILE_ALL_ACCESS,
                self._lookup(account, win32security),
            )
        if allow_local_service_owner:
            # LocalService is shared by unrelated services. An explicit Owner
            # Rights ACE suppresses the owner's implicit WRITE_DAC while this
            # service retains full control through its service-specific SID.
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                inheritance,
                OWNER_RIGHTS_READ_CONTROL,
                win32security.ConvertStringSidToSid(OWNER_RIGHTS_SID),
            )
        for account in readers:
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                0,
                ntsecuritycon.FILE_GENERIC_READ,
                self._specific_reader(account, win32security),
            )
        for sid_text in reader_sids:
            sid = win32security.ConvertStringSidToSid(sid_text)
            self._validate_reader_sid(sid, win32security)
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                0,
                ntsecuritycon.FILE_GENERIC_READ,
                sid,
            )
        return dacl

    def _set(
        self,
        path: Path,
        *,
        directory: bool,
        readers: tuple[str, ...] = (),
        reader_sids: tuple[str, ...] = (),
        allow_local_service_owner: bool = False,
    ) -> None:
        win32security, ntsecuritycon = _windows_modules()
        existed = validate_machine_path(path, directory=directory)
        if existed:
            self._validate_owner(
                path,
                win32security,
                allow_local_service_owner=allow_local_service_owner and not self.administrative,
            )
        dacl = self._access_control_list(
            win32security,
            ntsecuritycon,
            directory=directory,
            readers=readers,
            reader_sids=reader_sids,
            allow_local_service_owner=allow_local_service_owner,
        )
        information = win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION
        owner = None
        if self.administrative:
            information |= getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001)
            owner = self._lookup(r"BUILTIN\Administrators", win32security)
        try:
            win32security.SetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                information,
                owner,
                None,
                dacl,
                None,
            )
        except Exception as exc:
            raise RuntimeError(f"Die restriktive DACL konnte für {path} nicht gesetzt werden.") from exc
        validate_machine_path(path, directory=directory)
        if existed:
            self._validate_owner(
                path,
                win32security,
                allow_local_service_owner=allow_local_service_owner and not self.administrative,
                require_administrators=self.administrative,
            )

    def _create_protected_directory(
        self,
        path: Path,
        *,
        allow_local_service_owner: bool,
    ) -> None:
        win32security, ntsecuritycon = _windows_modules()
        pywintypes, win32file = _windows_directory_modules()
        dacl = self._access_control_list(
            win32security,
            ntsecuritycon,
            directory=True,
            allow_local_service_owner=allow_local_service_owner,
        )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        if self.administrative:
            descriptor.SetSecurityDescriptorOwner(
                self._lookup(r"BUILTIN\Administrators", win32security),
                0,
            )
        descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        descriptor.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED,
            win32security.SE_DACL_PROTECTED,
        )
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        try:
            win32file.CreateDirectoryW(str(path), attributes)
        except Exception as exc:
            raise RuntimeError(f"Das geschützte Dienstverzeichnis {path} konnte nicht erstellt werden.") from exc

    def protect_directory(self, path: Path, *, allow_local_service_owner: bool = False) -> None:
        existed = validate_machine_path(path, directory=True)
        if sys.platform == "win32" and not existed:
            self.create_protected_directory(
                path,
                allow_local_service_owner=allow_local_service_owner,
            )
            return
        path.mkdir(parents=True, exist_ok=True)
        validate_machine_path(path, directory=True)
        self._set(
            path,
            directory=True,
            allow_local_service_owner=allow_local_service_owner,
        )
        self._verify(
            path,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=allow_local_service_owner,
            require_exact_ace_count=True,
        )

    def create_protected_directory(self, path: Path, *, allow_local_service_owner: bool = False) -> None:
        """Atomically create a new Windows directory with its final protected DACL."""

        if sys.platform != "win32":
            raise OSError("Ein geschütztes Dienstverzeichnis kann nur unter Windows atomar erstellt werden.")
        if validate_machine_path(path, directory=True):
            raise FileExistsError(f"Das geschützte Dienstverzeichnis existiert bereits: {path}")
        if not validate_machine_path(path.parent, directory=True):
            raise RuntimeError(f"Der übergeordnete Maschinenpfad {path.parent} fehlt.")
        self._create_protected_directory(
            path,
            allow_local_service_owner=allow_local_service_owner,
        )
        try:
            self._verify(
                path,
                allow_readers=False,
                directory=True,
                allow_local_service_owner=allow_local_service_owner,
                require_exact_ace_count=True,
            )
        except Exception:
            # Creation succeeded, so do not leak an unverified empty random
            # directory when a native verification call fails. rmdir never
            # traverses a replacement or reparse target and deliberately
            # leaves a populated path untouched for fail-closed diagnosis.
            try:
                path.rmdir()
            except OSError:
                pass
            raise

    def protect_configuration(self, path: Path) -> None:
        self._set(path, directory=False)
        self._verify(path, allow_readers=False, require_exact_ace_count=True)

    def protect_token(self, path: Path) -> None:
        self._set(path, directory=False, readers=self.token_readers)
        self._verify(path, allow_readers=True)

    def protect_log(self, path: Path) -> None:
        self._set(path, directory=False, allow_local_service_owner=True)
        self._verify(
            path,
            allow_readers=False,
            allow_local_service_owner=True,
            require_exact_ace_count=True,
        )

    def _verify(
        self,
        path: Path,
        *,
        allow_readers: bool,
        directory: bool = False,
        allow_local_service_owner: bool = False,
        allow_inherited_from_verified_parent: bool = False,
        require_exact_ace_count: bool = False,
        allow_redundant_administrator_ace: bool = False,
    ) -> bool:
        win32security, ntsecuritycon = _windows_modules()
        validate_machine_path(path, directory=directory)
        try:
            descriptor = win32security.GetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001),
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            control, _revision = descriptor.GetSecurityDescriptorControl()
            owner_sid = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        except Exception as exc:
            raise RuntimeError(f"Die DACL konnte für {path} nicht geprüft werden.") from exc
        if not self._owner_is_trusted(
            owner_sid,
            win32security,
            allow_local_service_owner=allow_local_service_owner,
        ):
            raise RuntimeError(f"Der Maschinenpfad {path} besitzt keine vertrauenswürdige Windows-Identität.")
        dacl_is_protected = bool(control & win32security.SE_DACL_PROTECTED)
        if dacl is None or (not dacl_is_protected and not allow_inherited_from_verified_parent):
            raise RuntimeError(f"Die DACL ist für {path} nicht vor Vererbung geschützt.")

        required = {
            win32security.ConvertSidToStringSid(self._lookup(account, win32security))
            for account in BASE_TOKEN_PRINCIPALS
        }
        object_inherit = getattr(win32security, "OBJECT_INHERIT_ACE", 0x01)
        container_inherit = getattr(win32security, "CONTAINER_INHERIT_ACE", 0x02)
        no_propagate = getattr(win32security, "NO_PROPAGATE_INHERIT_ACE", 0x04)
        inherit_only = getattr(win32security, "INHERIT_ONLY_ACE", 0x08)
        inherited = getattr(win32security, "INHERITED_ACE", 0x10)
        required_inheritance = object_inherit | container_inherit if directory else 0
        observed: set[str] = set()
        seen_sids: set[str] = set()
        owner_rights_count = 0
        redundant_administrator_count = 0
        direct_administrator_sids: frozenset[str] | None = None
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            header, mask, sid = ace[0], int(ace[1]), ace[2]
            ace_type = int(header[0])
            ace_flags = int(header[1])
            sid_text = win32security.ConvertSidToStringSid(sid)
            if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
                raise RuntimeError(f"Die DACL für {path} enthält einen nicht erlaubten ACE-Typ.")
            if ace_flags & inherit_only:
                raise RuntimeError(f"Die DACL für {path} enthält einen nicht anwendbaren INHERIT_ONLY-ACE.")
            if sid_text in seen_sids:
                raise RuntimeError(
                    f"Die DACL für {path} enthält eine Windows-Identität mehrfach und ist daher nicht exakt."
                )
            seen_sids.add(sid_text)
            owner_rights_ace = sid_text == OWNER_RIGHTS_SID
            redundant_administrator_ace = False
            if (
                allow_redundant_administrator_ace
                and directory
                and sid_text not in required
                and not owner_rights_ace
                and sid_text not in FORBIDDEN_BROAD_SIDS
            ):
                try:
                    _name, _domain, sid_type = win32security.LookupAccountSid(None, sid)
                except Exception as exc:
                    raise RuntimeError(
                        f"Eine zusätzliche Windows-Identität der DACL für {path} konnte nicht geprüft werden."
                    ) from exc
                if int(sid_type) == 1:  # SidTypeUser
                    if direct_administrator_sids is None:
                        direct_administrator_sids = self._direct_local_administrator_sids(win32security)
                    redundant_administrator_ace = sid_text in direct_administrator_sids
            expected_inheritance = (
                required_inheritance if sid_text in required or owner_rights_ace or redundant_administrator_ace else 0
            )
            inherited_log_flags_are_safe = (
                allow_inherited_from_verified_parent
                and not dacl_is_protected
                and not ace_flags & (inherit_only | no_propagate)
                and not ace_flags & ~(object_inherit | container_inherit | inherited)
            )
            if not inherited_log_flags_are_safe and ace_flags != expected_inheritance:
                raise RuntimeError(f"Die DACL für {path} enthält unerwartete OI/CI-Vererbungsflags.")
            if owner_rights_ace:
                if not allow_local_service_owner or mask != OWNER_RIGHTS_READ_CONTROL:
                    raise RuntimeError(f"Die DACL für {path} enthält einen ungültigen Owner-Rights-ACE.")
                owner_rights_count += 1
                continue
            if sid_text in FORBIDDEN_BROAD_SIDS:
                raise RuntimeError(f"Die DACL für {path} gewährt einer zu breiten lokalen Gruppe Zugriff.")
            if redundant_administrator_ace:
                if mask != int(ntsecuritycon.FILE_ALL_ACCESS):
                    raise RuntimeError(f"Die DACL für {path} enthält keine exakt begrenzte Administratorberechtigung.")
                redundant_administrator_count += 1
                if redundant_administrator_count > 1:
                    raise RuntimeError(f"Die DACL für {path} enthält mehr als eine zusätzliche Administratoridentität.")
                observed.add(sid_text)
                continue
            if sid_text not in required:
                if not allow_readers:
                    raise RuntimeError(f"Die DACL für {path} enthält eine nicht provisionierte Schreibberechtigung.")
                self._validate_reader_sid(sid, win32security)
                if mask != int(ntsecuritycon.FILE_GENERIC_READ):
                    raise RuntimeError(f"Die DACL für {path} enthält keine exakt provisionierte Leseberechtigung.")
            if sid_text in required:
                full_access = int(ntsecuritycon.FILE_ALL_ACCESS)
                if mask != full_access:
                    raise RuntimeError(f"Die DACL für {path} gewährt einer Dienstidentität nicht den Vollzugriff.")
            observed.add(sid_text)
        if not required <= observed:
            raise RuntimeError(f"Die DACL für {path} enthält nicht alle erforderlichen Dienstidentitäten.")
        if allow_local_service_owner and owner_rights_count != 1:
            raise RuntimeError(f"Die DACL für {path} begrenzt die impliziten Besitzerrechte nicht exakt.")
        if not allow_local_service_owner and owner_rights_count:
            raise RuntimeError(f"Die DACL für {path} enthält einen unerwarteten Owner-Rights-ACE.")
        required_ace_count = len(required) + int(allow_local_service_owner)
        if require_exact_ace_count and dacl.GetAceCount() != required_ace_count:
            raise RuntimeError(f"Die DACL für {path} enthält nicht exakt die erforderlichen Dienstidentitäten.")
        return redundant_administrator_count == 1

    def verify_service_paths(self, paths: ServicePaths) -> None:
        self._verify(paths.data_directory, allow_readers=False, directory=True)
        self._verify(paths.configuration, allow_readers=False)
        self._verify(paths.token, allow_readers=True)
        if validate_machine_path(paths.log.parent, directory=True):
            self._verify(
                paths.log.parent,
                allow_readers=False,
                directory=True,
                allow_local_service_owner=True,
            )
        if validate_machine_path(paths.log, directory=False):
            self._verify(paths.log, allow_readers=False, allow_local_service_owner=True)

    def verify_data_directory(self, path: Path) -> None:
        """Verify the private parent before creating transient service data below it."""

        self._verify(path, allow_readers=False, directory=True)

    def verify_runtime_directory(self, path: Path) -> None:
        """Verify an explicitly protected transient service directory."""

        self._verify(
            path,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=True,
            require_exact_ace_count=True,
        )

    def verify_runtime_entry_for_purge(self, path: Path, *, directory: bool) -> None:
        """Verify one child inheriting from a verified transient directory."""

        self._verify(
            path,
            allow_readers=False,
            directory=directory,
            allow_local_service_owner=True,
            allow_inherited_from_verified_parent=True,
            require_exact_ace_count=True,
        )

    def repair_explorer_directory_aces(self, paths: ServicePaths) -> None:
        """Normalize the one narrowly accepted Explorer directory-ACE shape."""

        repair_data_directory = self._verify(
            paths.data_directory,
            allow_readers=False,
            directory=True,
            allow_redundant_administrator_ace=True,
        )
        self._verify(paths.configuration, allow_readers=False)
        self._verify(paths.token, allow_readers=True)

        repair_log_directory = False
        if validate_machine_path(paths.log.parent, directory=True):
            repair_log_directory = self._verify(
                paths.log.parent,
                allow_readers=False,
                directory=True,
                allow_local_service_owner=True,
                allow_redundant_administrator_ace=True,
            )
        if validate_machine_path(paths.log, directory=False):
            self._verify(paths.log, allow_readers=False, allow_local_service_owner=True)

        if repair_data_directory:
            self.protect_directory(paths.data_directory)
        if repair_log_directory:
            self.protect_directory(paths.log.parent, allow_local_service_owner=True)
        self.verify_service_paths(paths)

    def verify_configuration(self, path: Path) -> None:
        self._verify(path, allow_readers=False)

    def verify_token(self, path: Path) -> None:
        self._verify(path, allow_readers=True)

    def verify_existing_service_paths(self, paths: ServicePaths, *, include_log_file: bool = True) -> None:
        checks = [
            (paths.data_directory, False, True, False),
            (paths.configuration, False, False, False),
            (paths.token, True, False, False),
            (paths.log.parent, False, True, True),
        ]
        if include_log_file:
            checks.append((paths.log, False, False, True))
        for path, allow_readers, directory, allow_local_service_owner in checks:
            if validate_machine_path(path, directory=directory):
                self._verify(
                    path,
                    allow_readers=allow_readers,
                    directory=directory,
                    allow_local_service_owner=allow_local_service_owner,
                )

    def verify_log(self, path: Path) -> None:
        self._verify(path, allow_readers=False, allow_local_service_owner=True)

    def verify_log_for_purge(self, path: Path) -> None:
        self._verify(
            path,
            allow_readers=False,
            allow_local_service_owner=True,
            allow_inherited_from_verified_parent=True,
            require_exact_ace_count=True,
        )

    def grant_token_reader(self, token_path: Path, account: str) -> None:
        win32security, _ntsecuritycon = _windows_modules()
        requested_sid = win32security.ConvertSidToStringSid(self._specific_reader(account, win32security))
        self._verify(token_path, allow_readers=True)
        readers = self._additional_reader_sids(token_path)
        self._set(
            token_path,
            directory=False,
            reader_sids=tuple(sorted(readers | {requested_sid})),
        )
        self._verify(token_path, allow_readers=True)

    def token_protector_preserving(self, token_path: Path) -> Any:
        self._verify(token_path, allow_readers=True)
        readers = tuple(sorted(self._additional_reader_sids(token_path)))
        return lambda temporary: self._set(temporary, directory=False, reader_sids=readers)

    def protect_token_preserving_readers(self, token_path: Path) -> None:
        self._verify(token_path, allow_readers=True)
        readers = tuple(sorted(self._additional_reader_sids(token_path)))
        self._set(token_path, directory=False, reader_sids=readers)
        self._verify(token_path, allow_readers=True)

    def _additional_reader_sids(self, path: Path) -> set[str]:
        win32security, _ntsecuritycon = _windows_modules()
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        required = {
            win32security.ConvertSidToStringSid(self._lookup(account, win32security))
            for account in BASE_TOKEN_PRINCIPALS
        }
        return {
            self._validate_reader_sid(dacl.GetAce(index)[2], win32security)
            for index in range(dacl.GetAceCount())
            if win32security.ConvertSidToStringSid(dacl.GetAce(index)[2]) not in required
        }


def expected_service_sid() -> str:
    return SERVICE_SID_ACCOUNT
