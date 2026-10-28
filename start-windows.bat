@echo off
setlocal
REM Development helper: browser UI in demo mode only.
REM The installed Depth Studio prepares its own Python environment in
REM %LOCALAPPDATA%\DepthStudio on first launch - no global Python, no admin rights.
cd /d "%~dp0"
where node >nul 2>nul || (echo Install Node.js 20 or newer from https://nodejs.org/ and retry. & pause & exit /b 1)
if not exist node_modules (
  echo Installing application dependencies...
  call npm ci || (echo npm install failed. & pause & exit /b 1)
)
start "Depth Studio (dev)" cmd /k npm run dev
timeout /t 3 /nobreak >nul
start http://localhost:5173
endlocal
