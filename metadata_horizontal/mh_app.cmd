@echo off
rem Metadata Horizontal (PowerShell version): double-click to start. Needs psql.exe; Excel upload needs ImportExcel.
start "" powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0mh_app.ps1" %*
