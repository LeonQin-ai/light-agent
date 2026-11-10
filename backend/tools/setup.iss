; setup.iss — Inno Setup script for the light-agent desktop edition.
;
; Produces a standard Windows installer (wizard + uninstaller + Add/Remove
; Programs entry). Per-user install (no admin) to %LOCALAPPDATA%\light-agent.
; The desktop and start-menu shortcuts launch the native WebView2 shell
; (light-agent-desktop.exe), which starts the backend and opens the web console
; in a single window.
;
; Build (package.sh does this):
;   ISCC.exe tools/setup.iss

#define MyAppName "light-agent"
#define MyAppVersion "3.1.2"
#define MyAppPublisher "light-agent"
#define MyAppExeName "light-agent-desktop.exe"

[Setup]
AppId={{2636E16C-C375-4BA7-867B-E6AACE2045F7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\light-agent
; Always offer destination selection, including when upgrading an existing install.
DisableDirPage=no
UsePreviousAppDir=yes
AlwaysShowDirOnReadyPage=yes
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=light-agent-setup
SetupIconFile=..\dist\light-agent-logo.ico
UninstallDisplayIcon={app}\light-agent-logo.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\NOTICE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\light-agent.exe";         DestDir: "{app}"; DestName: "light-agent.exe";          Flags: ignoreversion
Source: "..\build\light-agent-desktop.exe"; DestDir: "{app}";                                   Flags: ignoreversion
Source: "..\dist\light-agent.ico";         DestDir: "{app}";                                   Flags: ignoreversion
Source: "..\dist\light-agent-logo.ico";          DestDir: "{app}";                                   Flags: ignoreversion
Source: "..\assets\light-agent-pet.png";          DestDir: "{app}";                                   Flags: ignoreversion
Source: "..\assets\light-agent-pet-thinking.png"; DestDir: "{app}";                                   Flags: ignoreversion
Source: "..\README.md";                DestDir: "{app}"; DestName: "README.txt";           Flags: ignoreversion

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\light-agent-logo.ico"; WorkingDir: "{app}"
Name: "{group}\{#MyAppName}";       Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\light-agent-logo.ico"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
