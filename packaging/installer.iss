; Inno Setup script for retypeset.
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=0.8.0 packaging\installer.iss
;
; Per-user install by default (PrivilegesRequired=lowest): university machines
; routinely refuse administrator rights, and nothing here needs them -- no
; service, no driver, no PATH edit, no registry beyond the uninstall entry.

#ifndef AppVersion
  #define AppVersion "0.8.0"
#endif

[Setup]
AppName=retypeset
AppVersion={#AppVersion}
AppPublisher=Chouaib Ammari, University Kasdi Merbah-Ouargla
AppPublisherURL=https://github.com/USERNAME/retypeset
DefaultDirName={autopf}\retypeset
DefaultGroupName=retypeset
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=retypeset-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
LicenseFile=..\LICENSE
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\retypeset.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\retypeset\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\retypeset"; Filename: "{app}\retypeset.exe"
Name: "{group}\Uninstall retypeset"; Filename: "{uninstallexe}"
Name: "{autodesktop}\retypeset"; Filename: "{app}\retypeset.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\retypeset.exe"; Description: "Start retypeset"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Streamlit's own state, written on first run.
Type: filesandordirs; Name: "{localappdata}\retypeset"
