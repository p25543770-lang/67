@echo off
setlocal
cd /d "%~dp0"
where node >nul 2>nul || (echo Install Node.js 20 or newer from https://nodejs.org/ and retry. & pause & exit /b 1)
if not exist node_modules (
  echo Installing application dependencies...
  call npm ci || (echo npm install failed. & pause & exit /b 1)
)
start "Depth Studio" cmd /k npm run dev
 timeout /t 3 /nobreak >nul
start http://localhost:5173
endlocal
