!define PRODUCT_NAME "Xiaoda Agent"
!define PRODUCT_VERSION "${VERSION}"
!define PRODUCT_PUBLISHER "Xiaoda Agent Team"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "xiaoda-agent-windows-x64-v${PRODUCT_VERSION}-setup.exe"
InstallDir "$PROGRAMFILES64\${PRODUCT_NAME}"
InstallDirRegKey HKLM "Software\${PRODUCT_NAME}" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"
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
SetOutPath "$INSTDIR"
SetOverwrite on
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
SectionEnd

Section -AdditionalIcons
WriteIniStr "$INSTDIR\${PRODUCT_NAME}.url" "InternetShortcut" "URL" "https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent"
SectionEnd

Section -Post
WriteUninstaller "$INSTDIR\uninstall.exe"
WriteRegStr HKLM "Software\${PRODUCT_NAME}" "InstallDir" "$INSTDIR"
WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "DisplayName" "${PRODUCT_NAME}"
WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString" "$INSTDIR\uninstall.exe"
WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "DisplayVersion" "${PRODUCT_VERSION}"
WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "Publisher" "${PRODUCT_PUBLISHER}"
SectionEnd

Section Uninstall
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
Delete "$INSTDIR\${PRODUCT_NAME}.url"
Delete "$INSTDIR\uninstall.exe"
; 尝试移除空目录（如果用户数据仍在则不会删除）
RMDir "$INSTDIR"
Delete "$DESKTOP\小妲Agent.lnk"
RMDir /r "$SMPROGRAMS\${PRODUCT_NAME}"
DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
DeleteRegKey HKLM "Software\${PRODUCT_NAME}"
SectionEnd