' llm-monitor launcher -- no console from end to end.
'
' The batch file this replaces hid its python child with
' WScript.Shell.Run(cmd, 0, false).  That window style is honoured by conhost
' but ignored by Windows Terminal, which Windows 11 now delegates every new
' console to -- so a double-click left a black window that would not close.
' Starting from wscript.exe (GUI subsystem, no console of its own) and running
' the server under pythonw.exe (no console either) leaves Windows Terminal
' nothing to take over.
'
' Usage:
'   start_monitor.vbs        read-only power card
'   start_monitor_admin.vbs  elevated, power presets writable

Option Explicit

Const DASHBOARD_URL = "http://127.0.0.1:8090"

Dim fso, shell, root, pythonw, attempt, extra, arg_index
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)

' Anything after the script name goes on to monitor.py unchanged, so the
' command line the batch file documented -- "--port 8091", "--interval 5" --
' still works.  Each argument is quoted on the way through because VBScript
' hands them over with their original quoting already stripped.
extra = ""
For arg_index = 0 To WScript.Arguments.Count - 1
    extra = extra & " " & Quote(WScript.Arguments(arg_index))
Next

pythonw = ResolvePythonw()
If pythonw = "" Then
    MsgBox "pythonw.exe was not found." & vbCrLf & vbCrLf & _
           "Install Python, or point the PYTHONW environment variable at an " & _
           "interpreter, then try again.", vbExclamation, "llm-monitor"
    WScript.Quit 1
End If

' Already serving?  Then this is a second click rather than a restart: just
' bring the page up.  Starting another copy would only lose the race for the
' port and leave a process that reports "port busy" and exits.
If DashboardIsUp() Then
    shell.Run DASHBOARD_URL, 1, False
    WScript.Quit 0
End If

' The router mints a fresh api-key on every start, so a config.json left over
' from the last run answers 401 and the dashboard shows no models at all.
' Best effort by design: if the helper cannot find the router's log it leaves
' the config alone, and the panel reports the 401 exactly as it always did.
If fso.FileExists(root & "\tools\sync_hermes_key.py") Then
    shell.Run Quote(pythonw) & " " & Quote(root & "\tools\sync_hermes_key.py"), 0, True
End If

' Window style 0, and do not wait: monitor.py serves until it is killed, so
' waiting here would hang the launcher for the whole life of the dashboard.
shell.Run Quote(pythonw) & " " & Quote(root & "\monitor.py") & extra, 0, False

' Wait for the port to answer before handing the browser a url that may still
' refuse the connection.  Fifteen tries at 200 ms covers a cold start; a
' slower one falls through to the same url anyway and a refresh picks it up.
For attempt = 1 To 15
    WScript.Sleep 200
    If DashboardIsUp() Then Exit For
Next

shell.Run DASHBOARD_URL, 1, False
WScript.Quit 0


Function Quote(value)
    Quote = """" & value & """"
End Function


Function ResolvePythonw()
    ' Same order the batch file used -- the managed interpreter first, then the
    ' usual per-user install -- except pythonw.exe is probed directly, because
    ' shelling out to `where` would need a console, which is the whole thing
    ' this launcher exists to avoid.
    Dim candidates, candidate, versions_dir, sub_folder

    candidates = Array( _
        shell.ExpandEnvironmentStrings("%PYTHONW%"), _
        shell.ExpandEnvironmentStrings("%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"), _
        shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"))

    For Each candidate In candidates
        If fso.FileExists(candidate) Then
            ResolvePythonw = candidate
            Exit Function
        End If
    Next

    ' Any managed runtime, in case the pinned version above was upgraded away.
    versions_dir = shell.ExpandEnvironmentStrings("%USERPROFILE%\.workbuddy\binaries\python\versions")
    If fso.FolderExists(versions_dir) Then
        For Each sub_folder In fso.GetFolder(versions_dir).SubFolders
            candidate = sub_folder.Path & "\pythonw.exe"
            If fso.FileExists(candidate) Then
                ResolvePythonw = candidate
                Exit Function
            End If
        Next
    End If

    ResolvePythonw = ""
End Function


Function DashboardIsUp()
    ' A direct localhost probe.  WinHttp is used rather than the W3C object on
    ' purpose: it does not travel through the system proxy, so a proxy setting
    ' cannot turn a live dashboard into a false negative and start a second
    ' copy that then loses the port.
    Dim http
    DashboardIsUp = False
    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 1000, 1000, 1000, 2000
    http.Open "GET", DASHBOARD_URL & "/api/snapshot", False
    http.Send
    If Err.Number = 0 Then
        If http.Status = 200 Then DashboardIsUp = True
    End If
    On Error GoTo 0
End Function
