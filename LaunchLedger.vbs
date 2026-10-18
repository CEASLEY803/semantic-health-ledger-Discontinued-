Option Explicit

Dim oShell, oFSO, sRoot, sPython

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

sRoot   = oFSO.GetParentFolderName(WScript.ScriptFullName)
sPython = sRoot & "\.venv\Scripts\python.exe"

If Not oFSO.FileExists(sPython) Then
    MsgBox "Virtual environment not found." & vbCrLf & _
           "Run setup.bat first.", vbCritical, "LaunchLedger"
    WScript.Quit 1
End If

oShell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & sRoot & "\ledger.ps1"" dev", 1, False

Set oFSO   = Nothing
Set oShell = Nothing
