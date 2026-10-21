@echo off
rem 启动三设备电量托盘（静默驻留，不弹黑窗）
cd /d "%~dp0"
start "" pythonw.exe "%~dp0tray_app.py"
