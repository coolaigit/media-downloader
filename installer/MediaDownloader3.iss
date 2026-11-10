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
; 4) 内置 Chromium(约 428MB) + WebView2 Fixed Runtime(约 383MB) + ffmpeg + bsk：
;    目标电脑不需要额外装浏览器，也不需要单独装 ffmpeg；桌面壳自带 WebView2
;    运行时（_internal\webview2\<ver>\），系统 WebView2 被清理工具破坏的机器
;    也能正常出 Chromium 窗口（探测优先级见 app/desktop_shell.py）。
;    VC++ 运行库(vcruntime140/msvcp140 等)已随包附带，无需另装 redist。
; 5) 安装包不含任何登录态/台账/下载文件 —— 那些是用户数据，不在程序目录里。

#define AppName "MediaDownloader"
#define AppVer "3.0.4"
#define AppVerShort "V3.0.4"
#define AppPublisher "Illumi Tools"
#define AppExe "MediaDownloader.exe"
#define SrcDir "..\dist_ful2\MediaDownloader"
; 必须指向构建产物（2026-11-10 本轮为 dist_ful2：全新 PyInstaller 产物，含随包
; Chromium 与 WebView2 Fixed Runtime）。原先是 ..\dist\MediaDownloader（本地在跑
; 的绿色版），两者脱节会打出「版本号是新的、文件是旧的」的假包 —— 2026-11-03
; 每次换构建目录务必同步改这里。2026-11-10 精简本地工作区后 dist_ful2 已归档到
; E:\_md_cleanup_20261110\dist_ful2，重新打包前先跑 PyInstaller 生成该目录：
;   python -m PyInstaller --noconfirm --distpath dist_ful2 --workpath _build_full _spec_onedir_release.spec

; 打包命令（本机 ISCC 位于 F:\Apps\Inno Setup 6\ISCC.exe）：
;   "F:\Apps\Inno Setup 6\ISCC.exe" installer\MediaDownloader3.iss

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
; 输出目录：D:\_md_installer_build 曾被杀软实时扫描锁住写入中的 exe（error 32，
; 2026-11-10 两次复现），换独立输出目录规避。
OutputDir=D:\_md_build_v304
OutputBaseFilename=MediaDownloader_V3.0.4
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVerShort}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=yes
RestartApplications=no
; 目录页必须**始终**显示（2026-11-10 实测事故）：原为 DisableDirPage=auto，
; 检测到已有安装记录时 Inno 会隐藏「选择目标位置」页，再叠加
; UsePreviousAppDir=yes 直接沿用上次目录 —— 用户点下一步就被带到上一次的
; 测试路径，没有任何选择机会。改成 no + 不沿用旧目录，每次都可选。
DisableDirPage=no
UsePreviousAppDir=no
AlwaysShowDirOnReadyPage=yes

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
