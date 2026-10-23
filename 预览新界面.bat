@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "D:\Python312\pythonw.exe" (
  start "" /D "%~dp0" "D:\Python312\pythonw.exe" "%~dp0ui_preview.py" --material mica
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" /D "%~dp0" pythonw "%~dp0ui_preview.py" --material mica
  exit /b 0
)
echo 未找到 pythonw，请用已安装的 Windows Python 运行 ui_preview.py。
pause
exit /b 1
