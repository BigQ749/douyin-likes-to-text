Option Explicit

Dim shell, fso, appRoot, depsRoot, legacyVendorRoot, venvConfig, baseHome, basePythonw, fallbackPythonw, launcher, app
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appRoot = fso.GetParentFolderName(WScript.ScriptFullName)
depsRoot = fso.BuildPath(appRoot, ".deps\douyin-downloader")
legacyVendorRoot = fso.BuildPath(fso.GetParentFolderName(appRoot), "vendor\douyin-downloader")
launcher = fso.BuildPath(appRoot, "desktop_app_launcher.pyw")
app = fso.BuildPath(appRoot, "desktop_app.py")

If Not fso.FileExists(app) Or Not fso.FileExists(launcher) Then
    MsgBox "The application files are incomplete.", 16, "Douyin Video Text Tool"
    WScript.Quit 1
End If

' Prefer the app environment created by scripts\bootstrap.ps1.
venvConfig = fso.BuildPath(appRoot, ".venv\pyvenv.cfg")
fallbackPythonw = fso.BuildPath(appRoot, ".venv\Scripts\pythonw.exe")
If Not fso.FileExists(venvConfig) Then
    venvConfig = fso.BuildPath(depsRoot, ".venv\pyvenv.cfg")
    fallbackPythonw = fso.BuildPath(depsRoot, ".venv\Scripts\pythonw.exe")
End If
If Not fso.FileExists(venvConfig) Then
    venvConfig = fso.BuildPath(legacyVendorRoot, ".venv\pyvenv.cfg")
    fallbackPythonw = fso.BuildPath(legacyVendorRoot, ".venv\Scripts\pythonw.exe")
End If

baseHome = ""
If fso.FileExists(venvConfig) Then
    Dim configFile, configLine
    Set configFile = fso.OpenTextFile(venvConfig, 1, False, 0)
    Do Until configFile.AtEndOfStream
        configLine = Trim(configFile.ReadLine)
        If LCase(Left(configLine, 7)) = "home = " Then
            baseHome = Trim(Mid(configLine, 8))
            Exit Do
        End If
    Loop
    configFile.Close
End If

If baseHome <> "" Then
    basePythonw = fso.BuildPath(baseHome, "pythonw.exe")
End If
If baseHome = "" Or Not fso.FileExists(basePythonw) Then
    basePythonw = fallbackPythonw
End If
If Not fso.FileExists(basePythonw) Then
    MsgBox "The local runtime is missing. Run scripts\bootstrap.ps1 once, then open this shortcut again.", 16, "Douyin Video Text Tool"
    WScript.Quit 1
End If

shell.CurrentDirectory = appRoot
shell.Run Chr(34) & basePythonw & Chr(34) & " " & Chr(34) & launcher & Chr(34), 0, False
