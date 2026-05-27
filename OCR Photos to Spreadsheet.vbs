Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = baseDir & "\.venv\Scripts\pythonw.exe"
appPath = baseDir & "\app\ocr_app.py"

If fso.FileExists(pythonw) Then
  shell.CurrentDirectory = baseDir
  shell.Run """" & pythonw & """ """ & appPath & """", 0, False
Else
  shell.Run """" & baseDir & "\OCR Photos to Spreadsheet.bat" & """", 1, False
End If
