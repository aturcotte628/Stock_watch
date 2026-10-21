' Marketscope - launch the Flask server with no console window.
' Double-click to start; set PYTHONW to override the interpreter (default: pythonw from PATH).
Dim sh, fso, dir, py
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
py = sh.ExpandEnvironmentStrings("%PYTHONW%")
If py = "%PYTHONW%" Then py = "pythonw"
sh.Run """" & py & """ """ & dir & "\app.py""", 0, False
