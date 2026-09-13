Option Explicit

Dim shell, fileSystem, appDir, pythonw, candidates, candidate, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
appDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
' app.py owns authenticated instance discovery and shutdown.

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
