@echo off
chcp 65001 >nul
setlocal
if not exist "%~dp0AC8翻译工具.exe" (
    echo 请将 AC8翻译工具.cmd 与 AC8翻译工具.exe 放在同一目录。
    pause
    exit /b 1
)
start "" "%~dp0AC8翻译工具.exe" %*
exit /b %errorlevel%
