@echo off
rem 打开三设备电量面板（不弹日志、不写文件）
cd /d "%~dp0"
start "" pythonw.exe "%~dp0my_devices.py"
