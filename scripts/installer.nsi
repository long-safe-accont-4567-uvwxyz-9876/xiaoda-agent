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
; 安装完成后可选运行自检：用 doctor.bat（详细输出 + chcp 65001 防中文乱码），
; 加 --launch：自检结束自动启动主程序（v0.5.60 修复：原直接跑 exe doctor，
; 无 pause 导致窗口一闪而过，也不会自动启动）
!define MUI_FINISHPAGE_RUN "$INSTDIR\doctor.bat"
!define MUI_FINISHPAGE_RUN_PARAMETERS "--launch"
!define MUI_FINISHPAGE_RUN_TEXT "运行自检并启动（推荐）"

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
; ── v0.5.60 修复：RMDir 删除旧 dist 前，先备份旧版壁纸到用户媒体目录 ──
; 旧架构（v0.5.5x）壁纸存在 exe 目录 web/dist/assets/wallpapers/，升级时若
; 直接 RMDir 删除，用户自定义壁纸会永久丢失（config.py 启动迁移也找不到源）。
; 这里复制到 $PROFILE\.ai-agent\media\wallpapers（与 MEDIA_DIR 一致），
; 只复制目标不存在的文件，不覆盖用户已有壁纸。
StrCpy $R2 "$INSTDIR\_internal\web\dist\assets\wallpapers"
StrCpy $R3 "$PROFILE\.ai-agent\media\wallpapers"
${If} ${FileExists} "$R2\*.*"
  CreateDirectory "$R3"
  FindFirst $R0 $R1 "$R2\*.*"
  ${DoWhile} $R1 != ""
    ${If} $R1 != "."
    ${AndIf} $R1 != ".."
      ${IfNot} ${FileExists} "$R3\$R1"
        CopyFiles /SILENT "$R2\$R1" "$R3"
      ${EndIf}
    ${EndIf}
    FindNext $R0 $R1
  ${Loop}
  FindClose $R0
${EndIf}
; 兼容旧版直接把 dist 放在安装根目录的结构
StrCpy $R2 "$INSTDIR\web\dist\assets\wallpapers"
${If} ${FileExists} "$R2\*.*"
  CreateDirectory "$R3"
  FindFirst $R0 $R1 "$R2\*.*"
  ${DoWhile} $R1 != ""
    ${If} $R1 != "."
    ${AndIf} $R1 != ".."
      ${IfNot} ${FileExists} "$R3\$R1"
        CopyFiles /SILENT "$R2\$R1" "$R3"
      ${EndIf}
    ${EndIf}
    FindNext $R0 $R1
  ${Loop}
  FindClose $R0
${EndIf}
RMDir /r "$INSTDIR\_internal\web\dist"
RMDir /r "$INSTDIR\web\dist"
File /r "dist\xiaoda-agent\*.*"
; Explicitly include dotfiles (NSIS *.* may skip files starting with .)
File "dist\xiaoda-agent\.version"
; .auto_update 标志已废弃：更新改为手动触发（auto-update.bat 独立运行）
; 保留 /nonfatal 以兼容旧构建目录，CI 不再生成此文件
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
; 主快捷方式直接指向 xiaoda-agent.exe（软件窗口入口）：
;   - exe 双击默认启动桌面原生窗口，内部已带看门狗，崩溃/卡死时自动重启
;   - WebView2 缺失时自动回退到浏览器
;   - 更新检查已分离到独立的「检查更新」快捷方式，启动时不再自动检查
CreateShortCut "$DESKTOP\小妲Agent.lnk" "$INSTDIR\xiaoda-agent.exe" "" "$INSTDIR\xiaoda-icon.ico" 0
CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\小妲Agent.lnk" "$INSTDIR\xiaoda-agent.exe" "" "$INSTDIR\xiaoda-icon.ico" 0
; CLI 命令行入口：双击进入 CLI 界面（安装目录已加入 PATH，cmd 输入 `xiaoda` 亦可进入）
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\CLI命令行.lnk" "$INSTDIR\xiaoda.bat" "" "$INSTDIR\xiaoda-icon.ico" 0
; 「检查更新」独立快捷方式 —— 启动主程序不再自动检查更新，用户需手动点此快捷方式
CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\检查更新.lnk" "$INSTDIR\auto-update.bat" "" "$INSTDIR\xiaoda-icon.ico" 0
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
; ── 创建卸载程序 ──
; 卡顿根因（v0.5.60 修复）：本安装包解压后 600MB+ / 数千个文件（PyInstaller 依赖
; + 前端 assets + 95MB 本地模型 + onnxruntime），WriteUninstaller 需把全部已装
; 文件清单写入 uninstall.exe，再被 Windows Defender 实时扫描，可能耗时 1-2 分钟，
; 界面看起来像"卡死在最后一步"。处理：
;   1) 先删旧 uninstall.exe：升级安装时旧文件可能被占用/杀软锁定 → WriteUninstaller
;      会一直等待，先删除可避免真卡死
;   2) 失败重试一次
;   3) DetailPrint 明确提示，让用户知道这一步在做什么而不是"假死"
ClearErrors
Delete "$INSTDIR\uninstall.exe"
${If} ${Errors}
  DetailPrint "[i] 旧卸载程序被占用，等待后重试..."
  Sleep 2000
  Delete "$INSTDIR\uninstall.exe"
${EndIf}
DetailPrint "[i] 正在创建卸载程序（需扫描已装文件，首次约 1-2 分钟，请稍候）..."
WriteUninstaller "$INSTDIR\uninstall.exe"
${If} ${Errors}
  DetailPrint "[!] 卸载程序创建失败，3 秒后重试..."
  Sleep 3000
  WriteUninstaller "$INSTDIR\uninstall.exe"
${EndIf}
DetailPrint "[i] 卸载程序创建完成"
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
  ; 广播环境变量变更：SendMessageTimeoutW + SMTO_ABORTIFHUNG(=2) + 2000ms 超时。
  ; 原 SendMessage HWND_BROADCAST 同步广播，若有窗口无响应会永久阻塞卡死。
  System::Call 'user32::SendMessageTimeoutW(i ${HWND_BROADCAST}, i ${WM_SETTINGCHANGE}, i 0, w "Environment", i 2, i 2000, *i .r0) .r0'
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
Delete "$INSTDIR\xiaoda.bat"
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
; 广播环境变量变更：SendMessageTimeoutW + SMTO_ABORTIFHUNG(=2) + 2000ms 超时。
; 原 SendMessage HWND_BROADCAST 同步广播，若有窗口无响应会永久阻塞，
; 导致卸载卡死在最后一步无法完成。改用超时版，跳过挂起窗口。
System::Call 'user32::SendMessageTimeoutW(i ${HWND_BROADCAST}, i ${WM_SETTINGCHANGE}, i 0, w "Environment", i 2, i 2000, *i .r0) .r0'
SectionEnd
