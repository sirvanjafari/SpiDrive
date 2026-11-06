; SpiDrive installer (Inno Setup 6). build.bat compiles it into dist\SpiDrive-Setup.exe
#define AppName "SpiDrive"
#define AppVersion "1.0.0"
#define HasWinFsp FileExists(AddBackslash(SourcePath) + "build\winfsp.msi")

[Setup]
AppId={{6E3B0D52-8C1F-4C2B-9A55-5D1E7B6F9A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=SpiDrive-Setup
SetupIconFile=build\spidrive.ico
UninstallDisplayIcon={app}\SpiDrive.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "autostart"; Description: "Start SpiDrive when I sign in to Windows"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\SpiDrive\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "THIRD_PARTY.txt"; DestDir: "{app}"; Flags: ignoreversion
#if HasWinFsp
Source: "build\winfsp.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedWinFsp
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\SpiDrive.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\SpiDrive.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SpiDrive"; ValueData: """{app}\SpiDrive.exe"" --autostart"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
#if HasWinFsp
Filename: "msiexec.exe"; Parameters: "/i ""{tmp}\winfsp.msi"" /qb"; StatusMsg: "Installing WinFsp (needed for the drive letter)..."; Flags: shellexec waituntilterminated; Verb: runas; Check: NeedWinFsp
#endif
Filename: "{app}\SpiDrive.exe"; Description: "Start SpiDrive now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /t /im SpiDrive.exe"; Flags: runhidden; RunOnceId: "StopSpiDrive"

[Messages]
FinishedLabel=SpiDrive is installed. It opens a setup page in your browser the first time it starts.

[Code]
function NeedWinFsp: Boolean;
begin
  Result := not FileExists(ExpandConstant('{commonpf32}\WinFsp\bin\winfsp-x64.dll'));
end;
