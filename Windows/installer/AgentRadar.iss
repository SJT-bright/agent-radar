#ifndef AppVersion
#define AppVersion "1.9.0"
#endif
#ifndef PublishDir
#define PublishDir "..\..\build\windows\publish"
#endif
[Setup]
AppId={{9E03C9AC-8B4A-48F0-8F77-BE3212D09ADA}
AppName=任务雷达
AppVersion={#AppVersion}
AppPublisher=SJT-bright
AppPublisherURL=https://github.com/SJT-bright/agent-radar
DefaultDirName={localappdata}\Programs\AgentRadar
DefaultGroupName=任务雷达
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\build\windows\release
OutputBaseFilename=AgentRadar-{#AppVersion}-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\AgentRadar.Windows\AppIcon.ico
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\AgentRadar.exe
[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked
[Files]
Source: "{#PublishDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\任务雷达"; Filename: "{app}\AgentRadar.exe"
Name: "{autodesktop}\任务雷达"; Filename: "{app}\AgentRadar.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\AgentRadar.exe"; Description: "打开任务雷达"; Flags: nowait postinstall skipifsilent
; User settings, queues, journals and sessions live outside {app}; uninstall never deletes them.
