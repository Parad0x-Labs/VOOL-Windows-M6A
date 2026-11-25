; Inno Setup script for the self-contained VOOL Windows installer.
; Packages the staging directory produced by build_bundle.ps1 into a double-click
; VOOL-Setup.exe. Per-user install (no admin needed). The .exe is unsigned unless you
; compile on a machine with a code-signing cert configured; unsigned installers trigger
; SmartScreen until signed.
;
; Build:  build_bundle.ps1 -Stage <stage>   then   ISCC /DStage=<stage> vool.iss

#ifndef Stage
  #define Stage "G:\vool-build\bundle"
#endif
#ifndef OutDir
  #define OutDir "G:\vool-build\dist"
#endif
#define AppName "VOOL"
; Passed by the build as /DAppVersion=<core/app_version.py VOOL_VERSION>. Hardcoding it here
; let the installer report 0.4.0 while the runtime and the release channel had moved on, so an
; installed build could not be identified from Add/Remove Programs.
#ifndef AppVersion
  #define AppVersion "0.0.0-unstamped"
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Parad0x Labs
DefaultDirName={autopf}\VOOL
DefaultGroupName=VOOL
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename=VOOL-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequired=lowest
; We stop the running VOOL server ourselves (see [Code]) before touching files, so Setup never
; needs to prompt the user to close "Python", and never tries to restart it (the launcher does).
RestartApplications=no
CloseApplications=no
SetupIconFile={#SourcePath}\..\assets\vool.ico
UninstallDisplayIcon={app}\vool.ico

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Enumerate the intended payload explicitly so compiler output and scratch directories under Stage
; cannot be copied into the installed bundle.
Source: "{#Stage}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
; Embedded models seed the persistent per-user store used by the launcher and are preserved on uninstall.
Source: "{#Stage}\models\*"; DestDir: "{localappdata}\VOOL\models"; Flags: recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist uninsneveruninstall
Source: "{#Stage}\ollama\*"; DestDir: "{app}\ollama"; Flags: recursesubdirs createallsubdirs ignoreversion
; The Windows broker, self-contained (windows\broker\publish.ps1). The API starts it with VOOL.
Source: "{#Stage}\broker\*"; DestDir: "{app}\broker"; Flags: recursesubdirs createallsubdirs ignoreversion
; The pin of the job launcher's folder {app}\broker\confine (every file there with its SHA-256).
; sandbox\windows_confinement_pin.py refuses the launcher unless the folder matches it exactly. No
; skipifsourcedoesntexist: a build without a pin must fail here, not ship a launcher VOOL refuses.
Source: "{#Stage}\config\confine-pin.json"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "{#Stage}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\bundle_manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\bundle_supervisor.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\VOOL.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool-open.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool_window.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool_protocol_handler.py"; DestDir: "{app}"; Flags: ignoreversion
; The shortcuts' icon. IconFilename is read on the USER's PC, so it must name an installed file:
; {#SourcePath} is expanded at compile time and names a folder on the build machine.
Source: "{#SourcePath}\..\assets\vool.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; The launcher folder must hold exactly the pinned files, so an upgrade clears it before copying:
; a file an older build shipped and this one dropped would otherwise stay behind as an unlisted
; file, and VOOL would refuse the launcher. Program files only; nothing of the user's lives there.
Type: filesandordirs; Name: "{app}\broker\confine"
; The bundle's runtime folders are replaced whole on every install, as the signed updater replaces them
; (core/updater/windows.py _MANAGED_DIRECTORIES). Copied over an older install, a package the new build
; ships at a new version left the old <name>-<version>.dist-info and any file the new version dropped, and a
; module a release removed stayed importable. PrepareToInstall has already stopped VOOL's own python;
; ollama goes down with it, since the supervisor runs ollama only inside a kill-on-close job. Program files only: the user's data, models, backups and update staging live elsewhere.
; IsVoolInstall guards all three: the folder page and /DIR= let a person point {app} at a folder of their
; own (D:\Tools, their user folder), where a "python", "app" or "ollama" folder is theirs. A first install,
; or one into a folder that holds no VOOL install, deletes nothing.
Type: filesandordirs; Name: "{app}\python"; Check: IsVoolInstall
Type: filesandordirs; Name: "{app}\app"; Check: IsVoolInstall
Type: filesandordirs; Name: "{app}\ollama"; Check: IsVoolInstall

[Registry]
; User-facing VOOL deep link. HKCU matches PrivilegesRequired=lowest and never requires admin.
Root: HKCU; Subkey: "Software\Classes\vool"; ValueType: string; ValueName: ""; ValueData: "URL:VOOL Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\vool"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Flags: uninsdeletevalue
; The link's icon and the name a browser shows when it asks to open a vool:// link ("Open VOOL?").
; Without them both came from pythonw.exe, so the OpenRouter sign-in asked "Open Python?".
Root: HKCU; Subkey: "Software\Classes\vool\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\vool.ico,0"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\vool\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "VOOL"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\vool\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\python\pythonw.exe"" -X utf8 ""{app}\vool_protocol_handler.py"" ""%1"""; Flags: uninsdeletekeyifempty

[Icons]
; AppUserModelID: the same id vool_window.py claims for its process (installer/bundle/windows_chrome.py
; APP_USER_MODEL_ID), so the taskbar groups the VOOL window under these shortcuts and their icon.
; Launch via wscript so the VBS runs windowless (no console flash) with no dependency on the
; .vbs file association -- a .vbs cannot be executed directly (CreateProcess fails, code 193).
Name: "{group}\VOOL"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\vool.vbs"""; IconFilename: "{app}\vool.ico"; WorkingDir: "{app}"; AppUserModelID: "Parad0xLabs.VOOL"
Name: "{group}\Uninstall VOOL"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VOOL"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\vool.vbs"""; IconFilename: "{app}\vool.ico"; WorkingDir: "{app}"; AppUserModelID: "Parad0xLabs.VOOL"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\vool.vbs"""; Description: "Launch VOOL now"; Flags: nowait postinstall skipifsilent

; No [UninstallDelete] for the data dir: uninstall removes ONLY the program files. The user's
; models, wallet, keys, memory and receipts in {localappdata}\VOOL are preserved (a reinstall
; reuses them, no multi-GB re-download and no lost wallet). A full reset stays a manual choice.
; API keys in Windows Credential Manager (VOOL/credentials/*, core/windows_credential_manager.py) are
; user data too: uninstall never deletes them, and the post-uninstall note says where they are.

[Messages]
; Uninstall removes the program only (see above), so it must not promise to remove "all of its
; components". The post-uninstall note in [Code] names the real kept folder.
ConfirmUninstall=Remove the %1 program from this PC?%n%nYour models, chats, settings, logs, wallet and keys are kept, so a reinstall picks them up again. The next message says where they are.
UninstalledAll=The %1 program was removed. Your models, chats, settings, logs, wallet and keys were kept.

[UninstallDelete]
; Program files only. The bundle ships no bytecode, so the app writes __pycache__ under {app}\app
; as it imports modules; those files were never installed, and without this every package folder
; (and the install folder itself) stayed behind after uninstall.
Type: filesandordirs; Name: "{app}\app"
; The signed updater keeps its staging folder and previous-version backups inside the program
; folder (core/updater/windows.py). They are program files too, never user data.
Type: filesandordirs; Name: "{app}\.vool-update"
Type: filesandordirs; Name: "{app}\update_backups"
Type: files; Name: "{app}\.vool_updating.lock"

[Code]
var
  VoolInstallFound: Boolean;

{ True only when {app} already holds a VOOL install made by this installer: the bundle manifest with its
  schema, the supervisor, the bundled interpreter and the app source, the markers the runtime and the
  signed updater use (core/runtime_paths.py _is_installed_windows_bundle, core/updater/windows.py). Any
  marker missing means the folder is not ours, so [InstallDelete] removes nothing from it. }
function DetectVoolInstall(): Boolean;
var
  Root, Manifest: String;
  Text: AnsiString;
begin
  Result := False;
  Root := ExpandConstant('{app}');
  Manifest := Root + '\bundle_manifest.json';
  if not FileExists(Manifest) then Exit;
  if not FileExists(Root + '\bundle_supervisor.py') then Exit;
  if not FileExists(Root + '\python\python.exe') then Exit;
  if not DirExists(Root + '\app\core') then Exit;
  if not LoadStringFromFile(Manifest, Text) then Exit;
  Result := Pos('vool.bundle_manifest.v1', String(Text)) > 0;
end;

{ The Check for each [InstallDelete] runtime folder. Setup calls it once per entry, as it reaches that
  entry, and the python entry runs first: re-reading the markers then would find python\python.exe gone
  and leave app and ollama uncleared. So it returns the answer PrepareToInstall recorded before anything
  was deleted. }
function IsVoolInstall(): Boolean;
begin
  Result := VoolInstallFound;
end;

procedure StopNulla();
var
  ResultCode: Integer;
  InstallDir: String;
begin
  { Stop EVERY VOOL-owned python before touching files, so install/uninstall never hit a locked DLL
    (libcrypto-3.dll is loaded by BOTH the server vool_api_server.py AND the window host
    vool_window.py). Targets only the bundle's own python.exe/pythonw.exe by their exact paths, or
    a python/pythonw running one of VOOL's scripts under the install dir -- never a source checkout,
    the user's own Python (even one inside the install dir), or this powershell itself. Then
    waits for the OS to release the file handles before extraction begins. A listener on 11435 that
    is not one of those processes belongs to someone else and is never stopped. }
  { PowerShell also ends a single-quoted string at the curly quotes U+2018, U+2019, U+201A and
    U+201B, and reads any of them doubled as that one character. }
  InstallDir := ExpandConstant('{app}');
  StringChangeEx(InstallDir, '''', '''''', True);
  StringChangeEx(InstallDir, #$2018, #$2018#$2018, True);
  StringChangeEx(InstallDir, #$2019, #$2019#$2019, True);
  StringChangeEx(InstallDir, #$201A, #$201A#$201A, True);
  StringChangeEx(InstallDir, #$201B, #$201B#$201B, True);
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$r = ''' + InstallDir + '''; $rootPrefix = $r.TrimEnd([char[]]@(92,47)) + [char]92; $python = $rootPrefix + ''python\python.exe''; $pythonw = $rootPrefix + ''python\pythonw.exe''; $script = [regex]::Escape($rootPrefix) + ''(?:app\\apps\\vool_api_server\.py|vool_window\.py)''; $scriptArgument = ''^(?:\x22(?:[^\x22]*\\)?pythonw?\.exe\x22|(?:[^\s\x22]*\\)?pythonw?\.exe)(?:\s+-[bBdEhIiOPqRsSuvVx]+|\s+-W(?:\s+\x22[^\x22]*\x22|\s+\S+|[^\s\x22]+)|\s+-X(?:\s+\x22[^\x22]*\x22|\s+\S+|[^\s\x22]+)|\s+--check-hash-based-pycs\s+(?:\x22[^\x22]*\x22|\S+))*\s+\x22?'' + $script + ''(?:\x22|\s|$)''; Get-CimInstance Win32_Process -EA SilentlyContinue | Where-Object { ($_.Name -ieq ''python.exe'' -or $_.Name -ieq ''pythonw.exe'') -and ([string]::Equals([string]$_.ExecutablePath, $python, [System.StringComparison]::OrdinalIgnoreCase) -or [string]::Equals([string]$_.ExecutablePath, $pythonw, [System.StringComparison]::OrdinalIgnoreCase) -or ([string]$_.CommandLine -match $scriptArgument)) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }; Start-Sleep -Milliseconds 900"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

{ The one name install and uninstall call to stop VOOL before touching files (the Mac build's name);
  the Windows stop itself is StopNulla above. }
procedure StopVoolProcesses();
begin
  StopNulla();
end;

{ Setup sets the app constant only once the destination folder is known: expanding it from
  InitializeSetup aborts Setup before anything is installed (exit code 1). PrepareToInstall runs
  after the destination is chosen and before any file is touched, so the install-time stop runs
  there. In the uninstaller the app constant is the installed folder from the start. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopVoolProcesses();
  VoolInstallFound := DetectVoolInstall();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopVoolProcesses();
  Result := True;
end;

{ Name the kept data folder, expanded for this user. Wording only: nothing here deletes anything,
  and a silent uninstall (/SILENT, /VERYSILENT) is never stopped by a message box. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent()) then
    MsgBox('Your VOOL models, chats, settings, logs, wallet and keys were kept in:' + #13#10 +
      ExpandConstant('{localappdata}\VOOL') + #13#10#13#10 +
      'Reinstalling VOOL uses them again. The folder also holds your wallet and keys: back them up first, ' +
      'or deleting the folder loses them for good. If you no longer need any of it, delete that folder yourself.' + #13#10#13#10 +
      'API keys VOOL saved in Windows Credential Manager (entries named VOOL/credentials/...) were kept too. ' +
      'Deleting the folder does not remove them; remove them in Credential Manager if you no longer need them.',
      mbInformation, MB_OK);
end;
