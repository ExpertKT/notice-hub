' Runs a PowerShell script with no console window (Task Scheduler otherwise flashes a black box).
' Args: <script path> [extra powershell args...]
Option Explicit
Dim shell, args, i, cmd
Set shell = CreateObject("WScript.Shell")
If WScript.Arguments.Count < 1 Then WScript.Quit 2
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & WScript.Arguments(0) & """"
For i = 1 To WScript.Arguments.Count - 1
  cmd = cmd & " " & WScript.Arguments(i)
Next
shell.Run cmd, 0, False
