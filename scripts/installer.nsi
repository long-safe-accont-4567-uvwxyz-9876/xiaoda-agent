!define PRODUCT_NAME "Xiaoda Agent"
; VERSION 通过 makensis /DVERSION=x.y.z 命令行参数注入
!ifndef VERSION
  !define VERSION "0.0.0-dev"
!endif
!define PRODUCT_VERSION "${VERSION}"
!define PRODUCT_PUBLISHER "Xiaoda Agent Team"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
; OUTFILE 通过 makensis -DOUTFILE=path 注入，默认值用于本地测试
!ifndef OUTFILE
  !define OUTFILE "xiaoda-agent-windows-x64-v${VERSION}-setup.exe"
!endif
OutFile "${OUTFILE}"
; ── 绿化核心改动：per-user 安装到 LOCALAPPDATA，无需 UAC 管理员权限 ──
;   1. InstallDir 改为 $LOCALAPPDATA（用户可写，无需 admin）
;   2. RequestExecutionLevel user（不弹 UAC）
;   3. InstallDirRegKey 改为 HKCU（当前用户注册表）
;   4. SetShellVarContext current（快捷方式放当前用户目录）
;   这样双击安装包不再弹 UAC，且卸载无需管理员权限。
;   如果用户需要全机器安装，仍可通过右键"以管理员身份运行"升级到 per-machine。
InstallDir "$LOCALAPPDATA\${PRODUCT_NAME}"
InstallDirRegKey HKCU "Software\${PRODUCT_NAME}" "InstallDir"
RequestExecutionLevel user
SetShellVarContext current
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!define MUI_ICON "dist\xiaoda-agent\xiaoda-icon.ico"
!define MUI_UNICON "dist\xiaoda-agent\xiaoda-icon.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Section "MainSection" SEC01
; ── 旧版 per-machine 安装迁移（CodeRabbit 审查发现）──
;   检测 HKLM 下的旧版（ProgramFiles 安装），调用其 uninstaller 卸载，
;   避免新旧版本并存于不同目录。旧版 uninstaller 需要 admin 权限，
;   这里用 ExecWait 同步等待卸载完成；若用户未以 admin 运行，跳过迁移。
ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString"
${If} $0 != ""
  ; 检测到旧版 per-machine 安装，提示并调用其 uninstaller
  MessageBox MB_YESNO|MB_ICONQUESTION "检测到旧版（per-machine）安装于 $0。$\n$\n是否先卸载旧版再继续安装新版（per-user）？$\n（卸载旧版需要管理员权限，若取消请右键本安装包"以管理员身份运行"）" IDYES +2 IDNO skip_legacy_uninstall
  ExecWait '"$0" /S' ; /S 静默卸载（NSIS 默认 uninstaller 支持）
skip_legacy_uninstall:
${EndIf}

SetOutPath "$INSTDIR"
SetOverwrite on
; 关闭正在运行的实例，避免文件被锁定导致安装失败
nsExec::ExecToStack 'powershell -NoProfile -Command "Stop-Process -Name xiaoda-agent -Force -ErrorAction SilentlyContinue"'
; 安装前清理旧的前端文件，避免 vite hash 文件名导致的缓存问题
RMDir /r "$INSTDIR\_internal\web\dist"
RMDir /r "$INSTDIR\web\dist"
File /r "dist\xiaoda-agent\*.*"
; Explicitly include dotfiles (NSIS *.* may skip files starting with .)
File "dist\xiaoda-agent\.version"
File "dist\xiaoda-agent\.auto_update"
File /nonfatal "dist\xiaoda-agent\.env.example"
; 安装后清理可能残留的敏感文件（旧版升级时 .env 可能被保留）
Delete "$INSTDIR\_internal\config\webui_overrides.json"
Delete "$INSTDIR\config\webui_overrides.json"
; 清理旧版 agent 配置文件（IP 风险名称迁移）
Delete "$COMMONAPPDATA\Xiaoda Agent\config\agents\nahida.json"
Delete "$COMMONAPPDATA\Xiaoda Agent\config\agents\keli.json"
Delete "$COMMONAPPDATA\Xiaoda Agent\config\agents\yinlang.json"
Delete "$COMMONAPPDATA\Xiaoda Agent\config\agents\xilian.json"
Delete "$COMMONAPPDATA\Xiaoda Agent\config\agents\nike.json"
Delete "$APPDATA\Xiaoda Agent\config\agents\nahida.json"
Delete "$APPDATA\Xiaoda Agent\config\agents\keli.json"
Delete "$APPDATA\Xiaoda Agent\config\agents\yinlang.json"
Delete "$APPDATA\Xiaoda Agent\config\agents\xilian.json"
Delete "$APPDATA\Xiaoda Agent\config\agents\nike.json"
CreateShortCut "$DESKTOP\小妲Agent.lnk" "$INSTDIR\xiaoda-agent.exe" "--desktop" "$INSTDIR\xiaoda-icon.ico" 0
CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\小妲Agent.lnk" "$INSTDIR\xiaoda-agent.exe" "--desktop" "$INSTDIR\xiaoda-icon.ico" 0
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\卸载.lnk" "$INSTDIR\uninstall.exe"
; 创建用户数据目录结构（供用户上传参考音频、表情包等）
CreateDirectory "$PROFILE\.ai-agent\data\voice_refs"
CreateDirectory "$PROFILE\.ai-agent\data\stickers"
CreateDirectory "$PROFILE\.ai-agent\data\xiaoli-stickers"
CreateDirectory "$PROFILE\.ai-agent\data\agent-stickers"
CreateDirectory "$PROFILE\.ai-agent\data\media"
CreateDirectory "$PROFILE\.ai-agent\data\files"
SectionEnd

Section -AdditionalIcons
WriteIniStr "$INSTDIR\${PRODUCT_NAME}.url" "InternetShortcut" "URL" "https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent"
SectionEnd

Section -Post
WriteUninstaller "$INSTDIR\uninstall.exe"
; per-user 安装：注册表写 HKCU 而非 HKLM（无需管理员权限）
WriteRegStr HKCU "Software\${PRODUCT_NAME}" "InstallDir" "$INSTDIR"
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "DisplayName" "${PRODUCT_NAME}"
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString" "$INSTDIR\uninstall.exe"
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "DisplayVersion" "${PRODUCT_VERSION}"
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "Publisher" "${PRODUCT_PUBLISHER}"
; per-user 安装的卸载入口也放当前用户（SetShellVarContext current 已设置）
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "InstallLocation" "$INSTDIR"
SectionEnd

Section Uninstall
; 关闭正在运行的实例
nsExec::ExecToStack 'powershell -NoProfile -Command "Stop-Process -Name xiaoda-agent -Force -ErrorAction SilentlyContinue"'
; 卸载时保留用户数据（记忆数据库、配置、凭证等）
; 仅删除程序文件，不删除用户数据目录
RMDir /r "$INSTDIR\_internal"
Delete "$INSTDIR\xiaoda-agent.exe"
Delete "$INSTDIR\xiaoda-icon.ico"
Delete "$INSTDIR\.version"
Delete "$INSTDIR\.auto_update"
Delete "$INSTDIR\.env.example"
Delete "$INSTDIR\start-windows.bat"
Delete "$INSTDIR\auto-update.bat"
Delete "$INSTDIR\open-browser.ps1"
Delete "$INSTDIR\doctor.bat"
Delete "$INSTDIR\${PRODUCT_NAME}.url"
Delete "$INSTDIR\uninstall.exe"
; 尝试移除空目录（如果用户数据仍在则不会删除）
RMDir "$INSTDIR"
Delete "$DESKTOP\小妲Agent.lnk"
RMDir /r "$SMPROGRAMS\${PRODUCT_NAME}"
; per-user 卸载：清理 HKCU 注册表（与安装段对应）
DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
DeleteRegKey HKCU "Software\${PRODUCT_NAME}"
SectionEnd