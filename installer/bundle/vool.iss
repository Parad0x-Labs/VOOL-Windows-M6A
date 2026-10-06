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
#ifndef BuildMode
  #define BuildMode "Internal"
#endif
#if BuildMode == "Release"
  #define AppName "VOOL"
  #define AppIdValue "{{C503759E-5140-4AA7-AE0C-94A73D84C676}"
#else
  #define AppName "VOOL Internal Test"
  #define AppIdValue "{{0474D3B2-1B0C-4AE9-A7D9-F64A410EB434}"
#endif
#ifndef InstallerBaseName
  #define InstallerBaseName "VOOL-Setup-internal-unstamped"
#endif
#ifndef AppPublisher
  #error "Pass /DAppPublisher with the owner-authorized signing identity."
#endif
; Passed by the build as /DAppVersion=<core/app_version.py VOOL_VERSION>. Hardcoding it here
; let the installer report 0.4.0 while the runtime and the release channel had moved on, so an
; installed build could not be identified from Add/Remove Programs.
#ifndef AppVersion
  #define AppVersion "0.0.0-unstamped"
#endif

[Setup]
AppId={#AppIdValue}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\VOOL
DefaultGroupName=VOOL
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename={#InstallerBaseName}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequired=lowest
; The app's named mutex makes setup/uninstall ask the user to close VOOL first.
; Never terminate an arbitrary process merely because it owns a port or has a matching command line.
AppMutex=Local\VOOL_BUNDLE_SUPERVISOR,Local\VOOL_WINDOW_SINGLETON
SetupMutex=Local\VOOL_PYTHON_PAYLOAD_UPGRADE
RestartApplications=no
CloseApplications=no
SetupIconFile={#SourcePath}\..\assets\vool.ico
UninstallDisplayIcon={app}\native\VOOL.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Included in the one-download installer and extracted only when WebView2 is absent.
Source: "{#Stage}\webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy noencryption
; Setup-owned Python bootstrap has stdlib only and no import site. Never use old Python
; to authorize retirement, and never overwrite its ownership evidence during extraction.
Source: "{#Stage}\python-upgrade-bootstrap\*"; DestDir: "{tmp}\python-upgrade-bootstrap"; Flags: dontcopy noencryption
Source: "{#Stage}\python_payload.json"; Flags: dontcopy noencryption
; Enumerate the intended payload explicitly so compiler output and scratch directories under Stage
; cannot be copied into the installed bundle.
Source: "{#Stage}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
; Embedded models seed the persistent per-user store used by the launcher and are preserved on uninstall.
Source: "{#Stage}\models\*"; DestDir: "{localappdata}\VOOL\models"; Flags: recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist uninsneveruninstall
Source: "{#Stage}\ollama\*"; DestDir: "{app}\ollama"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\native\host\*"; DestDir: "{app}\native"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\native\broker\*"; DestDir: "{app}\native\broker"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Stage}\bundle_manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\bundle_supervisor.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\migrate_install_state.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\VOOL.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool_window.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\vool_protocol_handler.py"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; User-facing VOOL deep link. HKCU matches PrivilegesRequired=lowest and never requires admin.
Root: HKCU; Subkey: "Software\Classes\vool"; ValueType: string; ValueName: ""; ValueData: "URL:VOOL Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\vool"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\vool\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\python\pythonw.exe,0"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\vool\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\python\pythonw.exe"" -B ""{app}\vool_protocol_handler.py"" ""%1"""; Flags: uninsdeletekeyifempty

[Icons]
; The signed native host is the only installed user-facing entrypoint.
Name: "{group}\VOOL"; Filename: "{app}\native\VOOL.exe"; IconFilename: "{#SourcePath}\..\assets\vool.ico"; WorkingDir: "{app}"
Name: "{group}\Uninstall VOOL"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VOOL"; Filename: "{app}\native\VOOL.exe"; IconFilename: "{#SourcePath}\..\assets\vool.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\native\VOOL.exe"; Description: "Launch VOOL now"; Flags: nowait postinstall skipifsilent; Check: PythonUpgradeReady

; No [UninstallDelete] for the data dir: uninstall removes ONLY the program files. The user's
; models, wallet, keys, memory and receipts in {localappdata}\VOOL are preserved (a reinstall
; reuses them, no multi-GB re-download and no lost wallet). A full reset stays a manual choice.
; Python ownership/recovery archives intentionally remain for review after uninstall;
; only a verified closed archive may coexist with a subsequent reinstall. No automatic deletion.

[Code]
var
  PythonBootstrapReady: Boolean;
  PythonTransactionStarted: Boolean;
  PythonTransactionFinished: Boolean;

function PythonUpgradeReady(): Boolean;
begin
  Result := PythonTransactionFinished;
end;

function PythonPayloadAction(Action: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{tmp}\python-upgrade-bootstrap\python.exe'),
    '-I -S -B "' + ExpandConstant('{tmp}\python-upgrade-bootstrap\upgrade_python_payload.py') +
    '" ' + Action + ' --root "' + ExpandConstant('{app}') +
    '" --programs "' + ExpandConstant('{autopf}') +
    '" --home "' + ExpandConstant('{localappdata}\VOOL') +
    '" --manifest "' + ExpandConstant('{tmp}\python_payload.json') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function WebView2Present(): Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and
     (Version <> '') and (Version <> '0.0.0.0'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  Result := '';
  if CheckForMutexes('Local\VOOL_BUNDLE_SUPERVISOR,Local\VOOL_WINDOW_SINGLETON') then
  begin
    Result := 'Close VOOL completely before installing or repairing it, then retry setup.';
    Exit;
  end;
  try
    ExtractTemporaryFiles('{tmp}\python-upgrade-bootstrap\*');
    ExtractTemporaryFile('python_payload.json');
    PythonBootstrapReady := True;
  except
    Result := 'VOOL could not extract its Python upgrade verifier. No program payload was changed.';
    Exit;
  end;
  if not PythonPayloadAction('recover') or not PythonPayloadAction('check') then
  begin
    Result := 'Python ownership/recovery verification failed. Existing files are preserved; legacy or modified installs require review. No automatic cleanup is permitted.';
    Exit;
  end;
  if WebView2Present() then
    Exit;
  try
    ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
  except
    Result := 'VOOL could not extract its bundled WebView2 Runtime installer.';
    Exit;
  end;
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'VOOL could not start its bundled WebView2 Runtime installer.';
    Exit;
  end;
  for Attempt := 1 to 5 do
  begin
    if WebView2Present() then
      Exit;
    Sleep(500);
  end;
  Result := 'WebView2 Runtime installation did not complete. VOOL was not installed; retry setup or contact support.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    PythonTransactionStarted := True;
    if not PythonPayloadAction('begin') then
      RaiseException('Python payload retirement failed safely. Setup cannot proceed.');
    Exit;
  end;
  if CurStep <> ssPostInstall then
    Exit;
  if not PythonPayloadAction('verify') then
    RaiseException('Extracted Python payload failed verification. Setup cannot finish or launch VOOL.');
  if not Exec(ExpandConstant('{app}\python\python.exe'),
    '-B "' + ExpandConstant('{app}\migrate_install_state.py') + '" --root "' +
    ExpandConstant('{app}') + '" --home "' + ExpandConstant('{localappdata}\VOOL') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    RaiseException('VOOL could not safely migrate legacy program-folder state. Existing data was preserved; setup cannot finish.');
  if not PythonPayloadAction('finish') then
    RaiseException('Python payload finalization failed. Setup cannot finish or launch VOOL.');
  PythonTransactionFinished := True;
end;

procedure DeinitializeSetup();
begin
  // Attempt recovery at teardown. Actual cancellation ordering requires live qualification.
  // An interruption that prevents this callback leaves a journal for the next setup.
  if PythonBootstrapReady and PythonTransactionStarted and not PythonTransactionFinished then
    if not PythonPayloadAction('recover') then
      MsgBox('Python recovery could not be verified. Recovery material is preserved; do not launch VOOL until reviewed.', mbError, MB_OK);
end;
