Set shell = CreateObject("WScript.Shell")
baseDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = "C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
appPath = baseDir & "\app\ocr_app.py"

If CreateObject("Scripting.FileSystemObject").FileExists(pythonw) Then
  shell.CurrentDirectory = baseDir
  shell.Run """" & pythonw & """ """ & appPath & """", 0, False
Else
  shell.Run """" & baseDir & "\OCR Photos to Spreadsheet.bat" & """", 1, False
End If

