; ClipForge Windows installer (Inno Setup 6)
; Build:  iscc packaging\installer.iss   (run after pyinstaller)
#define AppVersion "0.1.0"

[Setup]
AppName=ClipForge
AppVersion={#AppVersion}
AppVerName=ClipForge {#AppVersion}
AppPublisher=Aditi Tech Solutions
AppPublisherURL=https://github.com/ashudhanda/clipforge
DefaultDirName={autopf}\ClipForge
DefaultGroupName=ClipForge
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ClipForge-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The exe is unsigned for now — Windows SmartScreen will show "Unknown
; publisher". Users click "More info → Run anyway". (Signing needs a paid cert.)

[Files]
Source: "..\dist\ClipForge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\ClipForge"; Filename: "{app}\ClipForge.exe"
Name: "{autodesktop}\ClipForge"; Filename: "{app}\ClipForge.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\ClipForge.exe"; Description: "Launch ClipForge"; Flags: nowait postinstall skipifsilent
