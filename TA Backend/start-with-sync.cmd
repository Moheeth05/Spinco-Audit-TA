@echo off
rem Starts TA Backend on THIS computer (http://localhost:4000) AND copies every PDF the Spinco TA Audit desktop app has stored
rem into TA Backend\storage (sorted + de-duplicated). The app's folder is only READ, never changed.
rem
rem   Desktop app (EXE / installer):  %APPDATA%\Spinco TA Audit\documents        <- used below
rem   Dev servers (uvicorn + vite):   D:\Bills\storage
rem   Unit-test data folder of UAT:   D:\Bills\uat-data-test\documents
rem Change the next line if your app keeps its documents somewhere else.
cd /d "%~dp0"
set "SYNC_FOLDER=%APPDATA%\Spinco TA Audit\documents"
set "SYNC_INTERVAL_SECONDS=30"
where node >nul 2>nul || (echo Node.js 18 or newer is required: https://nodejs.org & pause & exit /b 1)
node index.js
pause
