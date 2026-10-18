#ifndef AppVersion
  #error AppVersion must be supplied by the build command.
#endif
#ifndef PackageDir
  #error PackageDir must point to the packaged Windows application.
#endif

[Setup]
AppId={{58B35E4E-E68F-4D61-92DB-1591968EC711}
AppName=夸克网盘客户端
AppVersion={#AppVersion}
AppPublisher=Quark Drive Client
DefaultDirName={localappdata}\Programs\QuarkDriveClient
DefaultGroupName=夸克网盘客户端
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
SetupIconFile=..\assets\app-icon.ico
UninstallDisplayIcon={app}\QuarkDriveClient.exe
OutputBaseFilename=QuarkDriveClient-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/normal
SolidCompression=yes
DiskSpanning=no
CloseApplications=no
RestartApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\夸克网盘客户端"; Filename: "{app}\QuarkDriveClient.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\夸克网盘客户端"; Filename: "{app}\QuarkDriveClient.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\QuarkDriveClient.exe"; Description: "启动夸克网盘客户端"; Flags: nowait postinstall skipifsilent
