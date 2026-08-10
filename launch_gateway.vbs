' Gateway hidden launcher.
' Runs gateway_run.bat in a HIDDEN console window: no black box on screen,
' but the gateway still has a real console so ttyd/ConPTY keep working.
' Logs still go to gateway.log (redirect inside gateway_run.bat).
' Used by start.bat and the gateway restart helper.
' NOTE: use the ABSOLUTE batch path - WScript.Shell.Run does not reliably
' resolve a relative .bat against CurrentDirectory.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
cmdline = "cmd.exe /c """ & dir & "\gateway_run.bat"""
sh.Run cmdline, 0, False
