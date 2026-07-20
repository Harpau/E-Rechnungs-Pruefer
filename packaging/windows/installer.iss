#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef OutputDir
  #error OutputDir muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef ProjectRoot
  #error ProjectRoot muss beim Aufruf von ISCC gesetzt werden.
#endif

#define AppName "E-Rechnungs-Prüfer"
#define AppExeName "E-Rechnungs-Pruefer.exe"

[Setup]
AppId={{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=E-Rechnungs-Pruefer contributors
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName}
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\E-Rechnungs-Pruefer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=E-Rechnungs-Pruefer-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
AppMutex=Local\E-Rechnungs-Pruefer-Desktop
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile={#ProjectRoot}\LICENSE
InfoAfterFile={#ProjectRoot}\THIRD_PARTY.md

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Symbole:"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#AppExeName}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\THIRD_PARTY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{#AppName} starten"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{localappdata}\E-Rechnungs-Pruefer\runtime.json"
Type: dirifempty; Name: "{localappdata}\E-Rechnungs-Pruefer"
