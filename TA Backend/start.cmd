@echo off
rem Starts TA Backend on THIS computer only: http://localhost:4000  (stop with Ctrl+C)
cd /d "%~dp0"
where node >nul 2>nul || (echo Node.js 18 or newer is required: https://nodejs.org & pause & exit /b 1)
node index.js
pause
