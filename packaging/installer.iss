; ClipForge2 Windows installer (Inno Setup 6)
; Build:  iscc packaging\installer.iss   (run after pyinstaller)
#define AppVersion "0.1.0"

[Setup]
AppName=ClipForge2
AppVersion={#AppVersion}
AppVerName=ClipForge2 {#AppVersion}
AppPublisher=Aditi Tech Solutions
AppPublisherURL=https://github.com/ashudhanda/clipforge2
DefaultDirName={autopf}\ClipForge2
DefaultGroupName=ClipForge2
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ClipForge2-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The exe is unsigned for now — Windows SmartScreen will show "Unknown
; publisher". Users click "More info → Run anyway". (Signing needs a paid cert.)

[Files]
Source: "..\dist\ClipForge2\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\ClipForge2"; Filename: "{app}\ClipForge2.exe"
Name: "{autodesktop}\ClipForge2"; Filename: "{app}\ClipForge2.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\ClipForge2.exe"; Description: "Launch ClipForge2"; Flags: nowait postinstall skipifsilent
