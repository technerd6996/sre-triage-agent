; Compiled by packaging\build_windows.ps1, which supplies AppVersion, Dist and Stage.
[Setup]
AppName=TRIAGE-1
AppVersion={#AppVersion}
AppId={{7D2C6B1E-4A53-4F0E-9B71-3E5A1C9F2D10}
DefaultDirName={autopf}\TRIAGE-1
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=TRIAGE-1-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
SetupLogging=yes

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "{#Stage}\pgsql\*"; DestDir: "{app}\pgsql"; Flags: recursesubdirs ignoreversion
Source: "{#Stage}\service\*"; DestDir: "{app}\service"; Flags: ignoreversion

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service\provision.ps1"" -AppDir ""{app}"""; StatusMsg: "Setting up the database and the Windows service..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service\remove_services.ps1"" -AppDir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveServices"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('TRIAGE-1 was removed. Your data was kept in C:\ProgramData\triage. Delete that folder only if you are sure you no longer need it.', mbInformation, MB_OK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Exec('sc.exe', 'stop TRIAGE1', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(3000);
  Result := '';
end;
