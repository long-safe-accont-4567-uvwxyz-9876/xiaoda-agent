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
; ── Per-user 安装优化：无需 UAC，降低权限并隔离用户安装状态 ──
;   1. InstallDir 使用 $LOCALAPPDATA（当前用户可写）
;   2. RequestExecutionLevel user（不请求管理员权限）
;   3. InstallDirRegKey 使用 HKCU（当前用户注册表）
;   4. SetShellVarContext current（快捷方式写入当前用户目录）
;   安装与卸载均无需管理员权限，不影响用户数据目录。
InstallDir "$LOCALAPPDATA\${PRODUCT_NAME}"
InstallDirRegKey HKCU "Software\${PRODUCT_NAME}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WordFunc.nsh"
!include "WinMessages.nsh"
!define MUI_ICON "dist\xiaoda-agent\xiaoda-icon.ico"
!define MUI_UNICON "dist\xiaoda-agent\xiaoda-icon.ico"

; 安装页面自定义文本
!define MUI_WELCOMEPAGE_TITLE "欢迎使用小妲 Agent 安装程序"
!define MUI_WELCOMEPAGE_TEXT "本程序将安装小妲 Agent 到你的用户目录（无需管理员权限）。$\n$\n点击「下一步」继续。"
!define MUI_INSTFILESPAGE_FINISH_HEADER_TEXT "安装完成"
!define MUI_INSTFILESPAGE_FINISH_HEADER_SUBTEXT "小妲 Agent 已成功安装到你的计算机"
; 安装完成后可选运行自检
!define MUI_FINISHPAGE_RUN "$INSTDIR\xiaoda-agent.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "doctor"
!define MUI_FINISHPAGE_RUN_TEXT "运行自检（推荐）"

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
; per-user 安装: 快捷方式放当前用户目录（必须在 Section 内调用）
SetShellVarContext current
; ── 旧版 per-machine 安装迁移（CodeRabbit 审查发现）──
;   检测 HKLM 下的旧版（ProgramFiles 安装），调用其 uninstaller 卸载，
;   避免新旧版本并存于不同目录。旧版 uninstaller 需要 admin 权限，
;   这里用 ExecWait 同步等待卸载完成；若用户未以 admin 运行，跳过迁移。
;   修复（v0.5.40）：原 MessageBox 每次安装都弹框要求 admin，用户体验差。
;   改为静默检测：仅在旧版 uninstaller 可访问时静默卸载，失败则跳过继续安装，
;   不阻塞 per-user 安装流程。用户可事后手动清理旧版。
ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString"
${If} $0 != ""
  ; 检测到旧版 per-machine 安装，尝试静默卸载（不弹框，不强制 admin）
  ClearErrors
  ExecWait '"$0" /S' ; /S 静默卸载（NSIS 默认 uninstaller 支持）
  ${If} ${Errors}
    ; 旧版卸载失败（需 admin 或卸载器异常）：不阻塞，继续 per-user 安装
    ClearErrors
    MessageBox MB_OK|MB_ICONINFORMATION "检测到旧版（per-machine）残留于 $0。$\n$\n新版将安装到用户目录（无需管理员权限）。$\n旧版可在控制面板手动卸载，不影响新版使用。"
  ${EndIf}
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
; .auto_update 使用 /nonfatal：CI 不再默认创建此文件，用户需手动创建以启用自动更新
File /nonfatal "dist\xiaoda-agent\.auto_update"
File /nonfatal "dist\xiaoda-agent\.env.example"
; 安装后清理可能残留的敏感文件（旧版升级时 .env 可能被保留）
Delete "$INSTDIR\_internal\config\webui_overrides.json"
Delete "$INSTDIR\config\webui_overrides.json"
; 清理旧版 agent 配置文件（IP 风险名称迁移）
; per-user 安装下 $COMMONAPPDATA (C:\ProgramData) 可能无写权限，Delete 失败不影响安装
ClearErrors
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
ClearErrors
; 快捷方式必须指向 start-windows.bat（唯一启动入口）：
;   - 执行更新检查（auto-update.bat）防止用户运行旧版
;   - 启动看门狗，崩溃时自动重启
;   - 直接运行 xiaoda-agent.exe 会绕过上述保护，更新后可能崩溃
CreateShortCut "$DESKTOP\小妲Agent.lnk" "$INSTDIR\start-windows.bat" "--desktop" "$INSTDIR\xiaoda-icon.ico" 0
CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\小妲Agent.lnk" "$INSTDIR\start-windows.bat" "--desktop" "$INSTDIR\xiaoda-icon.ico" 0
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
; UninstallString 必须用引号包裹路径，否则路径含空格时
; "C:\Users\foo\LocalAppData\Xiaoda Agent\uninstall.exe" 会被拆成多个参数
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString" '"$INSTDIR\uninstall.exe"'
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "DisplayVersion" "${PRODUCT_VERSION}"
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "Publisher" "${PRODUCT_PUBLISHER}"
; per-user 安装的卸载入口也放当前用户（SetShellVarContext current 已设置）
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "InstallLocation" "$INSTDIR"
; 添加安装目录到用户 PATH（per-user，避免重复添加）
ReadRegStr $R0 HKCU "Environment" "PATH"
ClearErrors
${WordFind} "$R0" "$INSTDIR" "E+1{" $R1
IfErrors 0 path_already_exists
  ${If} $R0 == ""
    WriteRegStr HKCU "Environment" "PATH" "$INSTDIR"
  ${Else}
    WriteRegStr HKCU "Environment" "PATH" "$R0;$INSTDIR"
  ${EndIf}
  SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment"
path_already_exists:
SectionEnd

Section Uninstall
; per-user 卸载: 清理当前用户快捷方式（必须在 Section 内调用）
SetShellVarContext current
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
Delete "$INSTDIR\auto-update.ps1"
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
; 从用户 PATH 移除安装目录
; 处理三种情况：PATH 恰为 $INSTDIR / $INSTDIR;前缀 / ;$INSTDIR 后缀
ReadRegStr $R0 HKCU "Environment" "PATH"
ClearErrors
${If} "$R0" == "$INSTDIR"
  ; PATH 恰好只有安装目录：清空
  WriteRegStr HKCU "Environment" "PATH" ""
${Else}
  ${WordReplace} "$R0" "$INSTDIR;" "" "+" $R1
  ${WordReplace} "$R1" ";$INSTDIR" "" "+" $R2
  WriteRegStr HKCU "Environment" "PATH" "$R2"
${EndIf}
SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment"
SectionEnd
