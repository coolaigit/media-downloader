; MediaDownloader V3.0 安装包脚本（Inno Setup 6）
;
; 设计取舍（为什么这么配）：
; 1) PrivilegesRequired=lowest + {autopf}：不需要管理员权限。普通用户双击就能装，
;    装到 %LOCALAPPDATA%\Programs\MediaDownloaderV3。公司电脑 / 无管理员账号也能用，
;    这是"其他电脑也能装"最省事的选择。
; 2) 程序自身把配置、登录态、下载台账写在 %LOCALAPPDATA%\MediaDownloader3，
;    和安装目录无关 —— 覆盖安装 / 升级不会丢数据，卸载也不动它。
; 3) MinVersion=10.0：Python 3.14 运行时不支持 Win8 及更早，低于 Win10 直接拒绝安装，
;    比装完了打不开要好。
; 4) 内置 Chromium(约 428MB) + ffmpeg + bsk：目标电脑不需要额外装浏览器，也不需要
;    单独装 ffmpeg。VC++ 运行库(vcruntime140/msvcp140 等)已随包附带，无需另装 redist。
; 5) 安装包不含任何登录态/台账/下载文件 —— 那些是用户数据，不在程序目录里。

#define AppName "MediaDownloader"
#define AppVer "3.0.3"
#define AppVerShort "V3.0.3"
#define AppPublisher "Illumi Tools"
#define AppExe "MediaDownloader.exe"
#define SrcDir "..\dist\MediaDownloader"

[Setup]
AppId={{8F3C2A47-5D61-4B9E-9C2F-7A1D4E6B8C05}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVerShort}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVer}
VersionInfoDescription={#AppName} {#AppVerShort} 安装程序
DefaultDirName={autopf}\MediaDownloaderV3
DefaultGroupName={#AppName} {#AppVerShort}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=D://_md_installer_build
OutputBaseFilename=MediaDownloader_V3.0.3_Setup
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVerShort}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=yes
RestartApplications=no
DisableDirPage=auto
UsePreviousAppDir=yes

[Languages]
Name: "chs"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "quicklaunchicon"; Description: "创建开始菜单快捷方式"; GroupDescription: "附加任务："

[Files]
; 整个运行目录：exe + _internal(含 Chromium/ffmpeg/bsk/CRT 运行库)
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName} {#AppVerShort}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName} {#AppVerShort}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName} {#AppVerShort}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName} {#AppVerShort}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清安装目录里的残留；用户数据在 %LOCALAPPDATA%\MediaDownloader3，刻意不动
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\MediaDownloader3');
    if DirExists(DataDir) then
      MsgBox('卸载已完成，程序文件已删除。' + #13#10 + #13#10 +
             '你的数据（登录态、下载台账、设置）保留在：' + #13#10 + DataDir + #13#10 + #13#10 +
             '如果需要彻底清除，请手动删除该文件夹。',
             mbInformation, MB_OK);
  end;
end;
