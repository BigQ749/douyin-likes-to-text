# Helper scripts

- `bootstrap.ps1`: installs the app dependencies and open-source downloader dependency for a clean clone.

From the project root in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
```

The script does not upload account logins, media files, audio, or transcription results.
