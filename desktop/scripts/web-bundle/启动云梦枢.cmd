@echo off
rem ===========================================================================
rem  YunMeng Hub - Web (portable) launcher
rem ---------------------------------------------------------------------------
rem  This .cmd exists for one reason: double-clickable. The real logic lives in
rem  start.ps1 (Chinese messages, health check, port picking, browser opening).
rem
rem  * Keep this file ASCII-only. A .cmd is read in the OEM code page (936 on a
rem    Chinese Windows), so Chinese text here would show up as mojibake on a
rem    machine with a different locale. All human-facing text is in start.ps1
rem    (UTF-8 BOM), which PowerShell reads correctly.
rem
rem  * cd to the extracted folder first (`%~dp0`), so start.ps1 never receives a
rem    Chinese path as an argument -- passing one through cmd.exe has bitten us
rem    before on non-936 code pages.
rem ===========================================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "start.ps1"
rem Keep the window open when something goes wrong, so the error stays readable.
if errorlevel 1 (
  echo.
  echo Failed to start. The message above says why.
  pause
)
