Option Explicit

Dim shell, fileSystem, appDir, url, http, pythonw, candidates, candidate, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
appDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
url = "http://127.0.0.1:8765"

On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", url & "/api/state", False
http.Send
If Err.Number = 0 Then
    If http.Status = 200 Then
        http.Open "POST", url & "/api/shutdown", False
        http.setRequestHeader "Content-Type", "application/json"
        http.Send "{}"
        WScript.Sleep 800
    End If
End If
Err.Clear
On Error GoTo 0

candidates = Array( _
    appDir & "\.venv\Scripts\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe" _
)

pythonw = ""
For Each candidate In candidates
    If fileSystem.FileExists(candidate) Then
        pythonw = candidate
        Exit For
    End If
Next

If pythonw = "" Then
    MsgBox "Python 3 was not found. Please reinstall Python or open README.md.", 16, "LeyLineBook"
    WScript.Quit 1
End If

command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & appDir & "\app.py" & Chr(34)
shell.Run command, 0, False
