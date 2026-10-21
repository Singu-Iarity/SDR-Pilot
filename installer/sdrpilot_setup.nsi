; SDR Pilot - Windows installer (per-user, no admin rights needed)
; Build: makensis sdrpilot_setup.nsi   (on Linux: apt install nsis)
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WordFunc.nsh"
!include "TextFunc.nsh"
!include "nsDialogs.nsh"

!ifndef VERSION
  !define VERSION "0.9.0"
!endif
!ifndef SRC
  !define SRC ".."
!endif
; the build stamp shipped in the files (sdrpilot/BUILD, e.g. 0.9.0.20261021.1315) - the same stamp the running app and
; its dashboard compare, so updates by this installer, by copying files in, or by the dashboard's restart button agree
!ifndef FULLVER
  !define /file FULLVER "${SRC}\sdrpilot\BUILD"
!endif
Name "SDR Pilot ${VERSION}"
OutFile "SDR-Pilot-Setup-${VERSION}.exe"
InstallDir "$PROFILE\SDR Pilot"
InstallDirRegKey HKCU "Software\SDR Pilot" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "SDR Pilot ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

!define MUI_ICON "${SRC}\sdrpilot\sdrpilot.ico"
!define MUI_UNICON "${SRC}\sdrpilot\sdrpilot.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "SDR Pilot ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "SDR Pilot lets an AI drive your SDR (through SDRangel) and GNU Radio: find signals, decode them, and build GNU Radio decoders - with a live dashboard you can open on any device.$\r$\n$\r$\nThis installer sets up everything it needs:$\r$\n  - SDR Pilot itself$\r$\n  - radioconda (GNU Radio for Windows), if you don't have it$\r$\n  - SDRangel (the SDR program), optional$\r$\n  - a desktop shortcut and an always-on background service$\r$\n$\r$\nNo administrator rights are needed. Downloads (radioconda is about 1 GB) need an internet connection."
!define MUI_COMPONENTSPAGE_SMALLDESC
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open SDR Pilot now"
!define MUI_FINISHPAGE_RUN_FUNCTION OpenApp
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\README.md"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show the README"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
Page custom MaintPage MaintLeave
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfMaint
!insertmacro MUI_PAGE_LICENSE "${SRC}\LICENSE"
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipComponents
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfMaint
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var PY      ; radioconda's python.exe
Var PYW     ; radioconda's pythonw.exe
Var OLDVER  ; version already installed ("" = fresh install)
Var OLDDIR  ; where it is installed
Var MODE    ; fresh | update | modify
Var WINOPEN ; 1 = the SDR Pilot desktop window was open when the update started (reopened afterwards)
Var RB_UPD
Var RB_MOD
Var RB_UNI

Function FindPython
  StrCpy $PY ""
  ${If} ${FileExists} "$PROFILE\radioconda\python.exe"
    StrCpy $PY "$PROFILE\radioconda\python.exe"
    StrCpy $PYW "$PROFILE\radioconda\pythonw.exe"
  ${ElseIf} ${FileExists} "$APPDATA\..\..\radioconda\python.exe"
    StrCpy $PY "$APPDATA\..\..\radioconda\python.exe"
    StrCpy $PYW "$APPDATA\..\..\radioconda\pythonw.exe"
  ${Else}
    ReadEnvStr $0 "ProgramData"
    ${If} ${FileExists} "$0\radioconda\python.exe"
      StrCpy $PY "$0\radioconda\python.exe"
      StrCpy $PYW "$0\radioconda\pythonw.exe"
    ${EndIf}
  ${EndIf}
FunctionEnd

Function .onInit
  Call FindPython
  ${If} $PY != ""
    ; radioconda is already there: don't offer to download it again
    SectionSetFlags 1 0
    SectionSetText 1 "radioconda (already installed)"
  ${EndIf}
  StrCpy $MODE "fresh"
  ReadRegStr $OLDVER HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SDR Pilot" "DisplayVersion"
  ReadRegStr $OLDDIR HKCU "Software\SDR Pilot" "InstallDir"
  ; the files themselves say which build is installed (they may have been updated without this installer)
  ${If} ${FileExists} "$OLDDIR\sdrpilot\BUILD"
    ClearErrors
    FileOpen $0 "$OLDDIR\sdrpilot\BUILD" r
    ${IfNot} ${Errors}
      FileRead $0 $1
      FileClose $0
      ${TrimNewLines} "$1" $1
      ${If} $1 != ""
        StrCpy $OLDVER $1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $OLDVER != ""
  ${AndIf} ${FileExists} "$OLDDIR\sdrpilot_app.py"
    StrCpy $INSTDIR $OLDDIR
    StrCpy $MODE "update"
    ; keep the user's earlier choice about the Claude desktop app
    ReadRegStr $0 HKCU "Software\SDR Pilot" "ClaudeRegistered"
    ${If} $0 == "1"
      SectionSetFlags 5 1
    ${EndIf}
  ${Else}
    StrCpy $OLDVER ""
  ${EndIf}
  ; "Download & install" in the dashboard runs this installer as  /S /UPDATE : update in place, no questions
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/UPDATE" $1
  ${IfNot} ${Errors}
  ${AndIf} $MODE == "fresh"
    MessageBox MB_ICONEXCLAMATION "No installed SDR Pilot was found to update." /SD IDOK
    Abort
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- existing installation: update / modify / uninstall
Function MaintPage
  ${If} $OLDVER == ""
    Abort  ; fresh install: no such page
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "SDR Pilot is already installed" "Version $OLDVER in $OLDDIR"
  nsDialogs::Create 1018
  Pop $0
  ${VersionCompare} "$OLDVER" "${FULLVER}" $1  ; 0 same, 1 installed is newer, 2 installed is older
  ${If} $1 == 2
    StrCpy $2 "Update SDR Pilot $OLDVER to ${FULLVER}"
    StrCpy $3 "Your settings, captures, flowgraphs and chats are kept. SDR Pilot is stopped during the update and started again afterwards (an open dashboard reloads by itself)."
  ${ElseIf} $1 == 0
    StrCpy $2 "Repair SDR Pilot ${FULLVER} (already up to date - reinstall its files)"
    StrCpy $3 "This build is already installed (perhaps updated from the dashboard). Repair puts back every program file and the shortcuts / background service. Your settings and data are kept."
  ${Else}
    StrCpy $2 "Replace the newer SDR Pilot $OLDVER with this older ${FULLVER}"
    StrCpy $3 "Not recommended: the installed files are newer than this installer (they were updated after it was made). Your data is kept."
  ${EndIf}
  ${NSD_CreateLabel} 0 0 100% 20u "This installer found SDR Pilot $OLDVER. What would you like to do?"
  Pop $0
  ${NSD_CreateRadioButton} 10u 26u 95% 12u "$2"
  Pop $RB_UPD
  ${NSD_CreateLabel} 24u 39u 90% 20u "$3"
  Pop $0
  ${NSD_CreateRadioButton} 10u 64u 95% 12u "Modify - choose which parts to add (radioconda, SDRangel, shortcuts, Claude app)"
  Pop $RB_MOD
  ${NSD_CreateRadioButton} 10u 82u 95% 12u "Uninstall SDR Pilot"
  Pop $RB_UNI
  ${If} $1 == 1
    ${NSD_Check} $RB_MOD
  ${Else}
    ${NSD_Check} $RB_UPD
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function MaintLeave
  ${NSD_GetState} $RB_UNI $0
  ${If} $0 == ${BST_CHECKED}
    ExecWait '"$OLDDIR\Uninstall SDR Pilot.exe" _?=$OLDDIR'
    ${IfNot} ${FileExists} "$OLDDIR\sdrpilot_app.py"
      Delete "$OLDDIR\Uninstall SDR Pilot.exe"
      RMDir "$OLDDIR"
    ${EndIf}
    Quit
  ${EndIf}
  ${NSD_GetState} $RB_MOD $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $MODE "modify"
  ${Else}
    StrCpy $MODE "update"
  ${EndIf}
FunctionEnd

Function SkipIfMaint
  ${If} $MODE != "fresh"
    Abort  ; same folder, license already accepted
  ${EndIf}
FunctionEnd

Function SkipComponents
  ${If} $MODE == "update"
    Abort  ; update / repair: same parts as before (radioconda, if present, is left alone)
  ${EndIf}
FunctionEnd

Section "SDR Pilot (required)" SecMain
  SectionIn RO
  ${If} $MODE != "fresh"
    Call FindPython
    ${If} $PY != ""
      DetailPrint "Stopping the running SDR Pilot (it is started again at the end)..."
      nsExec::ExecToLog '"$PY" "$INSTDIR\sdrpilot_app.py" stop'
      Pop $0
      Sleep 1500
    ${EndIf}
    ; close the desktop window so its exe can be replaced (and remember to reopen it)
    StrCpy $WINOPEN "0"
    nsExec::ExecToStack 'tasklist /NH /FI "IMAGENAME eq SDR Pilot.exe"'
    Pop $0
    Pop $1
    ClearErrors
    ${WordFind} "$1" "SDR Pilot.exe" "E#" $2
    ${IfNot} ${Errors}
      StrCpy $WINOPEN "1"
    ${EndIf}
    nsExec::Exec 'taskkill /F /IM "SDR Pilot.exe"'
    Pop $0
    ; program files only - workspace (your data) and sdrpilot_config.json (your settings) are never touched
    DetailPrint "Removing the old program files..."
    RMDir /r "$INSTDIR\sdrpilot"
    RMDir /r "$INSTDIR\blocks"
    RMDir /r "$INSTDIR\recipes"
  ${EndIf}
  SetOutPath "$INSTDIR"
  ; the program - never the user's data (workspace) or logs
  File /r /x workspace /x workspace_selftest /x logs /x __pycache__ /x *.pyc /x installer /x tests /x dashboard_new.html \
       "${SRC}\sdrpilot" "${SRC}\blocks" "${SRC}\recipes"
  File "${SRC}\*.py" "${SRC}\*.bat" "${SRC}\*.md" "${SRC}\LICENSE" "${SRC}\requirements.txt"
  ; the desktop app window (native, WebView2) + Microsoft's WebView2 loader
  File "${SRC}\SDR Pilot.exe" "${SRC}\WebView2Loader.dll"
  ; keep an existing config (the user's settings) - install the default one only on a fresh install
  ${IfNot} ${FileExists} "$INSTDIR\sdrpilot_config.json"
    File "${SRC}\sdrpilot_config.json"
  ${EndIf}
  CreateDirectory "$INSTDIR\logs"
  WriteUninstaller "$INSTDIR\Uninstall SDR Pilot.exe"
  WriteRegStr HKCU "Software\SDR Pilot" "InstallDir" "$INSTDIR"
  !define UNINST "Software\Microsoft\Windows\CurrentVersion\Uninstall\SDR Pilot"
  WriteRegStr HKCU "${UNINST}" "DisplayName" "SDR Pilot"
  WriteRegStr HKCU "${UNINST}" "DisplayVersion" "${FULLVER}"
  WriteRegStr HKCU "${UNINST}" "Publisher" "SDR Pilot"
  WriteRegStr HKCU "${UNINST}" "DisplayIcon" "$INSTDIR\SDR Pilot.exe"
  WriteRegStr HKCU "${UNINST}" "UninstallString" '"$INSTDIR\Uninstall SDR Pilot.exe"'
  WriteRegDWORD HKCU "${UNINST}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST}" "NoRepair" 1
SectionEnd

Section "radioconda - GNU Radio for Windows (download, ~1 GB)" SecConda
  InitPluginsDir
  File "/oname=$PLUGINSDIR\get_release.ps1" "get_release.ps1"
  DetailPrint "Looking up the newest radioconda release..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\get_release.ps1" -Repo ryanvolz/radioconda -Pattern "^radioconda-.*-Windows-x86_64\.exe$$" -Out "$PLUGINSDIR\radioconda-setup.exe"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "radioconda couldn't be downloaded. Install it yourself from https://github.com/ryanvolz/radioconda/releases (into your user folder), then run this installer again." /SD IDOK
    Abort
  ${EndIf}
  DetailPrint "Installing radioconda into $PROFILE\radioconda (several minutes, please wait)..."
  ExecWait '"$PLUGINSDIR\radioconda-setup.exe" /S /InstallationType=JustMe /AddToPath=0 /RegisterPython=0 /D=$PROFILE\radioconda' $0
  Call FindPython
  ${If} $PY == ""
    MessageBox MB_ICONEXCLAMATION "radioconda didn't install (exit code $0). Install it from https://github.com/ryanvolz/radioconda/releases, then run this installer again." /SD IDOK
    Abort
  ${EndIf}
SectionEnd

Section /o "SDRangel - the SDR program (download + its own installer)" SecSdrangel
  InitPluginsDir
  File "/oname=$PLUGINSDIR\get_release.ps1" "get_release.ps1"
  DetailPrint "Looking up the newest SDRangel release..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\get_release.ps1" -Repo f4exb/sdrangel -Pattern "(?i)win.*64.*\.exe$$" -Out "$PLUGINSDIR\sdrangel-setup.exe"'
  Pop $0
  ${If} $0 == 0
    DetailPrint "Starting the SDRangel installer - follow its steps..."
    ExecWait '"$PLUGINSDIR\sdrangel-setup.exe"'
  ${Else}
    MessageBox MB_ICONINFORMATION "SDRangel couldn't be downloaded automatically. Get it from https://github.com/f4exb/sdrangel/releases (the win64 installer)." /SD IDOK
  ${EndIf}
SectionEnd

Section "Python packages + self-test" SecSetup
  SectionIn RO
  Call FindPython
  ${If} $PY == ""
    MessageBox MB_ICONEXCLAMATION "radioconda (GNU Radio for Windows) wasn't found. Run this installer again with the radioconda part ticked, or install it from https://github.com/ryanvolz/radioconda/releases." /SD IDOK
    Abort
  ${EndIf}
  DetailPrint "Using $PY"
  DetailPrint "Installing the MCP Python package..."
  ${If} ${Silent}
    nsExec::ExecToLog '"$PY" -m pip install --disable-pip-version-check "mcp>=1.2"'
  ${Else}
    nsExec::ExecToLog '"$PY" -m pip install --upgrade --disable-pip-version-check "mcp>=1.2"'
  ${EndIf}
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "Installing the Python package 'mcp' failed (see the details above). SDR Pilot needs it - check your internet connection and run the installer again." /SD IDOK
    Abort
  ${EndIf}
  ${IfNot} ${Silent}   ; a dashboard-started update skips the minute-long self-test
    DetailPrint "Running SDR Pilot's self-test (simulator, no radio needed - about a minute)..."
    SetOutPath "$INSTDIR"
    nsExec::ExecToLog '"$PY" "$INSTDIR\selftest.py"'
    Pop $0
    ${If} $0 != 0
      MessageBox MB_ICONEXCLAMATION "The self-test reported a problem (details above, and in $INSTDIR\logs). SDR Pilot is installed anyway - send that log if something doesn't work." /SD IDOK
    ${EndIf}
  ${EndIf}
SectionEnd

Section "Desktop + Start menu shortcuts, start with Windows" SecApp
  Call FindPython
  ${If} $PY != ""
    SetOutPath "$INSTDIR"
    DetailPrint "Adding shortcuts and the background service (starts at logon, restarts itself)..."
    nsExec::ExecToLog '"$PY" "$INSTDIR\sdrpilot_app.py" install'
    Pop $0
    ; the Start-menu entry is what Windows Search and "All apps" list - made here directly as well, with the icon
    ; tells SDR Pilot.exe which python starts the host
    FileOpen $1 "$INSTDIR\launcher.json" w
    ${WordReplace} "$PY" "\" "\\" "+" $2
    FileWrite $1 '{"python": "$2"}'
    FileClose $1
  ${EndIf}
  ; the Start-menu entry is what Windows Search and "All apps" list; pointing at SDR Pilot.exe makes the taskbar
  ; button (and "Pin to taskbar") SDR Pilot's own
  SetShellVarContext current
  CreateShortCut "$SMPROGRAMS\SDR Pilot.lnk" "$INSTDIR\SDR Pilot.exe" "" "$INSTDIR\SDR Pilot.exe" 0 SW_SHOWNORMAL "" "SDR Pilot - AI-operated SDR dashboard"
  CreateShortCut "$DESKTOP\SDR Pilot.lnk" "$INSTDIR\SDR Pilot.exe" "" "$INSTDIR\SDR Pilot.exe" 0 SW_SHOWNORMAL "" "SDR Pilot - AI-operated SDR dashboard"
SectionEnd

Section /o "Also use it from the Claude desktop app" SecClaude
  Call FindPython
  ${If} $PY != ""
    DetailPrint "Registering SDR Pilot with the Claude desktop app..."
    nsExec::ExecToLog '"$PY" "$INSTDIR\install_claude_config.py" --force'
    Pop $0
    WriteRegStr HKCU "Software\SDR Pilot" "ClaudeRegistered" "1"
    DetailPrint "Restart Claude (tray icon > Quit, then open it) to use SDR Pilot there."
  ${EndIf}
SectionEnd

Section "-Start SDR Pilot again"
  Call FindPython
  ${If} $PY != ""
    SetOutPath "$INSTDIR"
    DetailPrint "Starting SDR Pilot..."
    nsExec::ExecToLog '"$PY" "$INSTDIR\sdrpilot_app.py" start'
    Pop $0
  ${EndIf}
  ${If} ${Silent}
  ${AndIf} $WINOPEN == "1"
  ${AndIf} ${FileExists} "$INSTDIR\SDR Pilot.exe"
    Exec '"$INSTDIR\SDR Pilot.exe"'
  ${EndIf}
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "The SDR Pilot program: MCP server, dashboard, dashboard agent, GNU Radio block library and recipes."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecConda} "GNU Radio for Windows (radioconda). Needed. Downloaded from GitHub and installed into your user folder."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecSdrangel} "SDRangel talks to your SDR (HackRF, RTL-SDR, Airspy, Pluto...). Skip if you already have it."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecSetup} "Installs the Python package SDR Pilot needs and checks everything works with the built-in simulator."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecApp} "Runs SDR Pilot on its own - no Claude app needed: an 'SDR Pilot' shortcut, and a background service that starts at logon."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecClaude} "Lets the Claude desktop app use SDR Pilot's tools too (adds it to Claude's MCP config)."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function OpenApp
  ${If} ${FileExists} "$INSTDIR\SDR Pilot.exe"
    Exec '"$INSTDIR\SDR Pilot.exe"'
  ${Else}
    Call FindPython
    ${If} $PYW != ""
      Exec '"$PYW" "$INSTDIR\sdrpilot_app.py" open'
    ${EndIf}
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.FindPython
  ${If} $PY != ""
    nsExec::ExecToLog '"$PY" "$INSTDIR\sdrpilot_app.py" stop'
    Pop $0
    nsExec::ExecToLog '"$PY" "$INSTDIR\sdrpilot_app.py" uninstall'
    Pop $0
  ${EndIf}
  nsExec::Exec 'taskkill /F /IM "SDR Pilot.exe"'
  Pop $0
  SetShellVarContext current
  Delete "$SMPROGRAMS\SDR Pilot.lnk"
  Delete "$DESKTOP\SDR Pilot.lnk"
  RMDir /r "$INSTDIR\sdrpilot"
  RMDir /r "$INSTDIR\blocks"
  RMDir /r "$INSTDIR\recipes"
  RMDir /r "$INSTDIR\logs"
  Delete "$INSTDIR\*.py"
  Delete "$INSTDIR\*.bat"
  Delete "$INSTDIR\*.md"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\requirements.txt"
  Delete "$INSTDIR\SDR Pilot.exe"
  Delete "$INSTDIR\WebView2Loader.dll"
  Delete "$INSTDIR\launcher.json"
  ${If} ${FileExists} "$INSTDIR\workspace\*.*"
    MessageBox MB_YESNO|MB_ICONQUESTION "Also delete your SDR Pilot data (captures, flowgraphs, chats, settings in $INSTDIR\workspace)?" /SD IDNO IDNO keep
      RMDir /r "$INSTDIR\workspace"
      Delete "$INSTDIR\sdrpilot_config.json"
    keep:
  ${Else}
    Delete "$INSTDIR\sdrpilot_config.json"
  ${EndIf}
  Delete "$INSTDIR\Uninstall SDR Pilot.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SDR Pilot"
  DeleteRegKey HKCU "Software\SDR Pilot"
SectionEnd

Function un.FindPython
  StrCpy $PY ""
  ${If} ${FileExists} "$PROFILE\radioconda\python.exe"
    StrCpy $PY "$PROFILE\radioconda\python.exe"
  ${Else}
    ReadEnvStr $0 "ProgramData"
    ${If} ${FileExists} "$0\radioconda\python.exe"
      StrCpy $PY "$0\radioconda\python.exe"
    ${EndIf}
  ${EndIf}
FunctionEnd
