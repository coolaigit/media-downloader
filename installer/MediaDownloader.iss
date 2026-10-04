; -*- coding: utf-8 -*-
; MediaDownloader 安装器定义（阶段 1F，Installer RC）
;
; 原则：
; - 输入源只有 ..\dist\MediaDownloader\*（RC3 onedir 本身就是完整应用目录），
;   Inno 只负责安装，不弥补 RC 缺文件。
; - 运行数据（config/logs/cookies/profile/downloads/history）全部在
;   %LOCALAPPDATA%\MediaDownloader 与 %USERPROFILE%\Downloads\MediaDownloader，
;   {app}（Program Files）在安装后保持只读使用 —— 因此 [InstallDelete] 只清
;   {app}\* 旧程序文件是安全的，卸载默认只删 {app}，绝不动用户数据。
; - AppId 永久固定：以后升级版本必须继续使用同一个 AppId（升级覆盖安装
;   才能命中同一个卸载项）。不要重新生成。
; - AppPublisher 故意留空（Publisher 待用户定案，不得代填）。
; - 不设项目自身 LicenseFile（项目 License 待用户定案，见 LICENSE-TODO.md）；
;   第三方声明随 RC3（THIRD_PARTY_NOTICES.txt / licenses\ /
;   FFMPEG_DISTRIBUTION_NOTES.md）一起安装。
; - Code signing: pending（本安装器未签名，SmartScreen 提示 Unknown Publisher
;   属当前预期）。

#define MyAppName "MediaDownloader"
#define MyAppVersion "2.0.0"
; 固定 AppId（2026-10-01 生成一次后固化，禁止随构建变化）
#define MyAppId "{{3A40E79A-76F4-4EEA-B328-5D122EFE7F5F}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; AppPublisher 留空：正式公开发布前由用户最终确认
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\MediaDownloader.exe
; 机器级正常安装（不为躲 UAC 装到奇怪目录）
PrivilegesRequired=admin
; 显式 64 位模式：否则 {autopf} 解析到 Program Files (x86)
; （1F 首次实测：Inno 6.7.3 不声明时按 32 位安装，装进了 (x86)，已卸载重装）
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; 运行中升级/卸载：用标准 Restart Manager 检测文件占用并提示关闭，
; 不粗暴 taskkill
CloseApplications=yes
; 输出
OutputDir=output
OutputBaseFilename=MediaDownloader-Setup-2.0.0-RC3
SetupIconFile=..\app_icon.ico
; 压缩：稳定性优先，禁 UPX / 裁剪资源
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 单应用，不需要程序组页
DisableProgramGroupPage=yes

[Tasks]
; 桌面快捷方式 = 可选任务，默认不勾选
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 唯一输入源：RC3 onedir 整体复制（含 _internal\ + THIRD_PARTY_NOTICES.txt
; + licenses\ + FFMPEG_DISTRIBUTION_NOTES.md）。禁止指向项目根 *。
Source: "..\dist\MediaDownloader\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; 升级前清理旧程序文件（仅 {app} 内，防 onedir 升级残留旧 _internal 文件）。
; 安全性：1A 之后运行数据已完全不在 {app}（LOCALAPPDATA / Downloads 承接），
; 因此删除 {app}\* 不会触碰任何用户数据。
Type: filesandordirs; Name: "{app}\*"

[Icons]
; 开始菜单快捷方式（始终创建）
Name: "{group}\{#MyAppName}"; Filename: "{app}\MediaDownloader.exe"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; 桌面快捷方式（可选任务，默认不勾选）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\MediaDownloader.exe"; \
    Tasks: desktopicon

[Run]
; 安装完成页可选启动（默认勾选）。静默安装时跳过。
; runasoriginaluser：避免提权安装器把应用以管理员令牌拉起。
; 端口仍由 EXE 自己决定；默认打开桌面窗口，桌面壳失败时才回退浏览器，不在此固定 8848。
Filename: "{app}\MediaDownloader.exe"; \
    Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: postinstall nowait skipifsilent runasoriginaluser
