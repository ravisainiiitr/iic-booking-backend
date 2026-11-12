; Inno Setup script for the SRIC Invoice Signer: per-user install (no administrator rights), Start-menu and desktop
; shortcuts, uninstaller, version info. Built by packaging/build.ps1 (or the build-sric-signer workflow):
;   ISCC.exe /DAppVersion=1.1.0 /DExeDir=..\dist /DOutDir=..\dist packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef ExeDir
  #define ExeDir "..\dist"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif
#define AppName "SRIC Invoice Signer"
#define AppExe "SRIC Invoice Signer.exe"
#define Publisher "Indian Institute of Technology Roorkee"

[Setup]
AppId={{6F1B0C7E-3D52-4C8E-9C1A-5E7B2D9A41F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL=https://equip.iitr.ac.in
AppSupportURL=https://equip.iitr.ac.in
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription={#AppName} setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=SRIC-Invoice-Signer-Setup-{#AppVersion}
SetupIconFile=..\assets\sric_signer.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#ExeDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Sign SRIC tax invoices with your DSC"
Name: "{autoprograms}\{#AppName} - detect DSC token"; Filename: "{app}\{#AppExe}"; Parameters: "--detect"
Name: "{autoprograms}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The saved settings (portal address, utility token, chosen token) live in %APPDATA%\SRIC Invoice Signer.
Type: filesandordirs; Name: "{userappdata}\{#AppName}"
