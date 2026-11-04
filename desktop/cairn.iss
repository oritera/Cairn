; Inno Setup script for the Cairn desktop app. Build with desktop\build.ps1
; (it passes /DAppVersion=... and expects the PyInstaller output in build\dist\Cairn).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Cairn"
#define AppExe "Cairn.exe"

[Setup]
AppId={{6F1C3A52-8E4B-4C2E-9A7D-3B5E0C1D2F48}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Cairn
AppComments=Fact-graph problem-solving engine — Windows desktop app
; Per-user install by default (no admin); the user may choose "all users" in the dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Cairn-Setup-{#AppVersion}
SetupIconFile=..\cairn\src\cairn\desktop\assets\cairn.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardSmallImageFile=wizard-small.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
LicenseFile=..\LICENSE
; Refuse to install/uninstall while Cairn is running (the app holds this mutex).
AppMutex=CairnDesktopSingleInstance
CloseApplications=yes
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\dist\Cairn\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove the previous version's bundled libraries so stale files never linger after an upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Open Cairn"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Url = 'https://developer.microsoft.com/microsoft-edge/webview2/';

function HasWebView2: Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\' + WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'Software\' + WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function InitializeSetup: Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  if not HasWebView2 then
  begin
    if MsgBox('Cairn shows its interface with the Microsoft Edge WebView2 Runtime, which is not installed on this PC.' + #13#10#13#10 +
              'Windows 11 includes it; on Windows 10 you can install it free from Microsoft.' + #13#10#13#10 +
              'Open the download page now? (Setup will continue either way.)', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2Url, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\cairn');
    if DirExists(DataDir) and (UninstallSilent = False) then
    begin
      if MsgBox('Also delete Cairn''s data on this PC?' + #13#10#13#10 +
                'This removes the project database, settings, logs and the API keys saved in Windows Credential Manager.' + #13#10 +
                'Your projects folder (the agents'' working files) is never deleted.' + #13#10#13#10 +
                'Choose No to keep everything for a later reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        Exec(ExpandConstant('{app}\{#AppExe}'), '--purge-secrets', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
