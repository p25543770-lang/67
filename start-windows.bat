@echo off
cd /d "%~dp0"
where node >nul 2>nul || (echo Install Node.js from https://nodejs.org/ && pause && exit /b 1)
where python >nul 2>nul || (echo Install Python and add it to PATH. && pause && exit /b 1)
python -c "import freenect, numpy, open3d" || (echo Missing Python dependencies. See README.md for Kinect setup. && pause && exit /b 1)
if not exist node_modules call npm install
start "Kinect bridge" cmd /k python server.py
start "Depth Studio" cmd /k npm run dev
 timeout /t 3 /nobreak >nul
start http://localhost:5173
