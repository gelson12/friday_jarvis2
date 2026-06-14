' Launches run.bat with NO visible console window (hidden background bridge).
Dim fso, here, sh
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run """" & here & "\run.bat""", 0, False
