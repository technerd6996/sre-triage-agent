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

[InstallDelete]
; Removes the previous version's program files, so nothing old lingers. Customer data is in ProgramData and is not touched.
Type: filesandordirs; Name: "{app}\Lib"
Type: filesandordirs; Name: "{app}\DLLs"
Type: filesandordirs; Name: "{app}\static"
Type: filesandordirs; Name: "{app}\skills"
Type: filesandordirs; Name: "{app}\db"
Type: files; Name: "{app}\app.*.pyd"

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

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
var Code: Integer; Exe, Launch, Line: String;
begin
  Result := '';
  Exe := ExpandConstant('{app}\triage1.exe');
  Launch := ExpandConstant('{app}\launch.py');
  if FileExists(Exe) then begin
    { An upgrade: back up with the installed version before any program file is replaced. }
    Line := '/C "set TRIAGE_SERVICE_ACCOUNT=NT SERVICE\TRIAGE1&& "' + Exe + '" "' + Launch + '" backup-now --reason before-upgrade"';
    if (not Exec(ExpandConstant('{cmd}'), Line, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then begin
      Result := 'The backup before this upgrade failed, so nothing was changed. Check the Backups section in the admin panel, then run the installer again.';
      Exit;
    end;
    Exec('sc.exe', 'stop TRIAGE1', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(5000);
    { The bundled database restarts with the service, because the service depends on it. }
    Exec('sc.exe', 'stop triage1-postgres', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(5000);
  end;
end;
