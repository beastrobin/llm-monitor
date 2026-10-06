' llm-monitor launcher, elevated.
'
' The power presets write nvidia-smi -pl, and nvidia-smi checks the caller's
' token before it will touch the limit -- so without elevation the card is
' read-only.  This launcher re-runs itself through UAC once and then hands off
' to start_monitor.vbs; the token, and nothing else, is the difference between
' the two entries.
'
' Usage: start_monitor_admin.vbs

Option Explicit

Dim fso, shell, root, app
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)

' The marker argument is what stops a relaunch loop: the elevated copy can
' only have been started by the branch below, which always passes it.
If Not WScript.Arguments.Named.Exists("elevated") Then
    Set app = CreateObject("Shell.Application")
    ' Window style 0, so the second wscript does not flash anything on screen.
    app.ShellExecute "wscript.exe", _
        """" & root & "\start_monitor_admin.vbs"" --elevated", root, "runas", 0
    WScript.Quit 0
End If

' Already elevated.  The shared launcher starts pythonw as a child of this
' process, so the server inherits the elevated token and the write path works.
shell.Run """wscript.exe"" """ & root & "\start_monitor.vbs""", 0, False
WScript.Quit 0
