Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
' Hidden window (0): the app runs in the background, log goes to app.log
sh.Run "cmd /c set PYTHONUTF8=1&& python app.py > app.log 2>&1", 0, False
