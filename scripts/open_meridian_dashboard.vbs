Option Explicit
Dim shell, fso, profile, desk, scripts, python, refresh, url, pingUrl, deadline, result
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
profile = shell.ExpandEnvironmentStrings("%USERPROFILE%")
desk = profile & "\GreekSoup-Meridian"
scripts = profile & "\Meridian\data\scripts"
python = desk & "\.venv\Scripts\pythonw.exe"
refresh = scripts & "\refresh_prices_manual.py"
url = "http://localhost:8765/plugins/meridian-overview/screen.html"
pingUrl = "http://localhost:8765/api/ping"

' Validate deployment without starting the desk, requesting quotes or opening a browser.
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "--check-only" Then
    If fso.FileExists(python) And fso.FileExists(refresh) And fso.FileExists(scripts & "\risk_refresh.py") Then
      WScript.Echo "Meridian launcher dependencies found; no refresh or launch performed."
      WScript.Quit 0
    End If
    WScript.Echo "Meridian launcher dependencies are missing."
    WScript.Quit 1
  End If
End If

If Not DeskIsRunning(pingUrl) Then
  shell.Run "schtasks.exe /Run /TN ""Research Desk""", 0, True
  deadline = DateAdd("s", 30, Now)
  Do While Not DeskIsRunning(pingUrl) And Now < deadline
    WScript.Sleep 500
  Loop
End If

If Not DeskIsRunning(pingUrl) Then
  MsgBox "Meridian could not start the research desk. Check the Research Desk task and its service log.", 16, "Meridian dashboard"
  WScript.Quit 1
End If

' Existing free quote refresh owns its mutex, dated marks and backups. Never pass --pause.
result = 1
If fso.FileExists(python) And fso.FileExists(refresh) And fso.FileExists(scripts & "\risk_refresh.py") Then
  On Error Resume Next
  result = shell.Run(Quote(python) & " " & Quote(refresh) & " --desk " & Quote(desk), 0, True)
  If Err.Number <> 0 Then result = 1
  Err.Clear
  On Error GoTo 0
End If
If result <> 0 Then
  MsgBox "Prices did not refresh. Meridian will show saved values; check their source dates. No trade or account change was made.", 48, "Meridian prices"
End If
shell.Run url, 1, False

Function Quote(value)
  Quote = Chr(34) & value & Chr(34)
End Function

Function DeskIsRunning(address)
  On Error Resume Next
  Dim request
  Set request = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  request.Open "GET", address, False
  request.setTimeouts 1000, 1000, 1000, 2000
  request.Send
  DeskIsRunning = (Err.Number = 0 And request.Status = 200)
  Err.Clear
  On Error GoTo 0
End Function
