Option Explicit
Dim shell, files, folder, command, argument
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File """ & folder & "\start.ps1"""
For Each argument In WScript.Arguments
  If argument = "--no-browser" Then command = command & " -NoBrowser"
Next
shell.Run command, 0, False
