; Grain Search: extra uninstall steps (bundle.windows.nsis.installerHooks in tauri.conf.json).
;
; Always (a real uninstall, not an automatic update):
;   delete %LOCALAPPDATA%\GrainSearch\runtime - Python, libraries and PyTorch (3-7 GB, re-downloadable).
; If "Delete the application data" is ticked - remove every trace:
;   the AI model, also from a folder the user picked (only its own ...\embeddinggemma-2 folder, never
;   the folder around it), then %LOCALAPPDATA%\GrainSearch (index, models, logs) and
;   %APPDATA%\GrainSearch (settings). The Hugging Face cache is shared with other tools: left alone.
;
; Automatic updates pass /UPDATE ($UpdateMode = 1): everything is kept, so nothing is downloaded again.

!macro NSIS_HOOK_PREINSTALL
!macroend

!macro NSIS_HOOK_POSTINSTALL
!macroend

!macro NSIS_HOOK_PREUNINSTALL
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    SetShellVarContext current
    RMDir /r "$LOCALAPPDATA\GrainSearch\runtime"

    ${If} $DeleteAppDataCheckboxState = 1
      ; the model folder, wherever it is: the engine wrote its full path here (UTF-16LE, one line)
      ClearErrors
      FileOpen $0 "$LOCALAPPDATA\GrainSearch\model-location.txt" r
      ${IfNot} ${Errors}
        FileReadUTF16LE $0 $1
        FileClose $0
        StrLen $2 $1
        StrCpy $3 $1 "" -17
        ; only a full path that ends in the model's own folder name (a damaged file deletes nothing)
        ${If} $3 == "\embeddinggemma-2"
        ${AndIf} $2 > 20
          RMDir /r "$1"
        ${EndIf}
      ${EndIf}

      RMDir /r "$LOCALAPPDATA\GrainSearch"
      RMDir /r "$APPDATA\GrainSearch"
    ${EndIf}
  ${EndIf}
!macroend
