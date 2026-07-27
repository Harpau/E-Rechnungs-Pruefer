# ADR 0002: Windows-Betriebsarten ohne automatische Migration

- Status: angenommen
- Datum: 2026-07-26
- Ersetzt: die Migrations-, Tokenübernahme- und Desktop-Seal-Anteile aus
  [ADR 0001](0001-windows-service-mode.md)

## Kontext

Desktop-/Tray-Modus und Windows-Dienst verwenden denselben Prüfcode, besitzen jedoch unterschiedliche
Installations-, Identitäts- und Schutzgrenzen. Der Desktopmodus gehört einem Benutzerprofil und verwendet HKCU,
`%LOCALAPPDATA%` und ein benutzerbezogenes API-Token. Der Dienstmodus gehört der Maschine und verwendet SCM,
`%ProgramFiles%`, geschütztes `%ProgramData%` und ein Maschinentoken.

Die in Version 1.4.0 eingeführte automatische Migration musste über die UAC-Grenze hinweg den ursprünglichen
Benutzer bestimmen, Desktopprozesse und Autostart kontrollieren, die Desktop-EXE quarantänisieren, optional das
Token übertragen und Desktop- sowie Dienstrollback gemeinsam persistieren. Diese Kopplung vergrößerte den
sicherheitskritischen Installerzustand und die Zahl der nach Prozessabbruch möglichen Zwischenzustände erheblich.

## Entscheidung

Desktop und Dienst bleiben getrennte, gegenseitig ausgeschlossene Betriebsarten, werden aber nicht mehr
automatisch ineinander überführt:

1. Vor der Dienstinstallation muss der Desktopmodus regulär und vollständig deinstalliert werden. Der
   Dienst-Installer beendet oder verändert keine Desktopinstallation. Ein read-only Scanner prüft
   Standardinstallationsordner, Uninstall-Key und Autostart aller registrierten lokalen und Entra-ID-Profile. Bei
   abgemeldeten Profilen wird genau ein no-follow geprüfter `NTUSER.DAT`- oder `NTUSER.MAN`-Hive privat mit
   `RegLoadAppKeyW` und `KEY_READ` geöffnet. Fehlende, doppelte oder anderweitig unsichere Inventuren blockieren
   geschlossen.
2. Solange der eigene SCM-Dienst registriert ist, verweigert der Desktop-Installer die Installation. Er verändert
   weder Dienstbundle noch SCM-Metadaten oder ProgramData.
3. Das Diensttoken wird nicht aus dem Desktopprofil übernommen. Die frühere Inno-Option
   `/MIGRATEDESKTOPTOKEN=1` wird ersatzlos nicht mehr unterstützt. Automatisierungen werden mit dem Token der neu
   installierten Betriebsart kontrolliert neu provisioniert.
4. Bei einer Dienstdeinstallation ausdrücklich erhaltenes `%ProgramData%\E-Rechnungs-Pruefer` ist allein kein
   installierter Gegenmodus. Der Desktopmodus darf bei diesem Zustand installiert werden, ohne ihn zu lesen oder
   zu verändern. Eine spätere Dienstneuinstallation verwendet das erhaltene Maschinentoken weiter.
5. Neue Dienstinstallationen verwenden eine service-only Transaktion für SCM-, Bundle- und
   Maschinenzustands-Recovery. Sie ist nicht an einen Desktop-Seal oder eine Benutzer-SID gebunden.
6. Unvollständige v1.4.0-Migrations-, Transfer-, Seal-, Quarantäne- und kombinierte Alttransaktionszustände werden
   nicht übernommen oder automatisch wiederhergestellt. Tests beginnen auf einer sauberen Wegwerf-VM.

Der gemeinsame Backend-Mutex und die feste Loopback-Portreservierung bleiben als Laufzeitgrenze bestehen. Sie
verhindern parallele Backends, ersetzen jedoch nicht den Installationsausschluss.

## Folgen

- Der UAC-übergreifende Originalbenutzer-Transfer, Tokentransfer, Desktop-Seal, EXE-Quarantäne und
  Desktop-Hard-Kill-Recovery entfallen.
- Ein Betriebsartenwechsel besteht sichtbar aus Deinstallation, Installation und erneuter Provisionierung der
  lokalen Automatisierungen.
- Die Diensttransaktion behält `PREPARED`, `COMMIT_STARTED`, Rollback, Roll-forward und die reale Reboot-Abnahme,
  ist aber unabhängig vom Desktopzustand.
- Der Windows-Modusausschlusstest prüft beide Blockierrichtungen und den Preserve-Fall mit reinem ProgramData.
- Historische v1.4.0-Zwischenzustände benötigen vor einer Neuinstallation eine gesicherte Diagnose und einen
  dokumentiert sauberen Ausgangszustand; einzelne Marker dürfen nicht versuchsweise gelöscht werden.
