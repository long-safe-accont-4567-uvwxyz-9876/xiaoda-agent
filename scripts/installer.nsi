!define PRODUCT_NAME "Nahida Agent"
!define PRODUCT_VERSION "${VERSION}"
!define PRODUCT_PUBLISHER "Nahida Agent Team"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "nahida-agent-windows-x64-v${PRODUCT_VERSION}-setup.exe"
InstallDir "$PROGRAMFILES64\${PRODUCT_NAME}"
InstallDirRegKey HKLM "Software\${PRODUCT_NAME}" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!define MUI_ICON "dist\nahida-agent\nahida-icon.ico"
!define MUI_UNICON "dist\nahida-agent\nahida-icon.ico"

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
File /r "dist\nahida-agent\*.*"
; Explicitly include dotfiles (NSIS *.* may skip files starting with .)
File "dist\nahida-agent\.version"
File "dist\nahida-agent\.auto_update"
; 安装后清理可能残留的敏感文件（旧版升级时 .env 可能被保留）
Delete "$INSTDIR\_internal\config\webui_overrides.json"
Delete "$INSTDIR\config\webui_overrides.json"
CreateShortCut "$DESKTOP\纳西妲Agent.lnk" "$INSTDIR\start-windows.bat" "" "$INSTDIR\nahida-icon.ico" 0
CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\纳西妲Agent.lnk" "$INSTDIR\start-windows.bat" "" "$INSTDIR\nahida-icon.ico" 0
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\卸载.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Section -AdditionalIcons
WriteIniStr "$INSTDIR\${PRODUCT_NAME}.url" "InternetShortcut" "URL" "https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent"
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
RMDir /r "$INSTDIR"
Delete "$DESKTOP\纳西妲Agent.lnk"
RMDir /r "$SMPROGRAMS\${PRODUCT_NAME}"
DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
DeleteRegKey HKLM "Software\${PRODUCT_NAME}"
SectionEnd
