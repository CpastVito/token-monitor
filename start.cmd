@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem =====================================================================
rem  解释器选择
rem  桌宠需要 tkinter + Pillow：优先使用系统 Python（自带），
rem  服务端对解释器无特殊要求。
rem =====================================================================
set "PY_SYS=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
set "PYW_SYS=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
set "PY_MAN="
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY_MAN=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

if exist "%PY_SYS%" (
    set "PY=%PY_SYS%"
    set "PYW=%PYW_SYS%"
) else if defined PY_MAN (
    set "PY=%PY_MAN%"
    set "PYW=%PY_MAN%"
) else (
    set "PY=python"
    set "PYW=pythonw"
)

echo ===============================================
echo   WorkBuddy Token 监测
echo ===============================================
echo   Python : %PY%
echo.

echo [1/3] 启动本地服务（后台，无窗口）...
start "" "%PYW%" "%~dp0server.py" --no-browser
ping -n 3 127.0.0.1 >nul

echo [2/3] 启动桌宠（鲸鱼娘）...
start "" "%PYW%" "%~dp0overlay.py"
ping -n 2 127.0.0.1 >nul

rem 首选端口被占时 server.py 会顺延，实际端口写在 .cache\server-port 里
set "PORT=8971"
if exist ".cache\server-port" set /p PORT=<".cache\server-port"

echo [3/3] 打开仪表盘 ...
start "" http://127.0.0.1:%PORT%/

echo.
echo  完成。
echo    · 桌宠：悬停展开面板 / 单击钉住 / 双击仪表盘 / 拖动移动 / 右键菜单
echo    · 全局热键 Ctrl+Alt+W 可唤出面板
echo    · 设置：仪表盘右上角「设置」，或 http://127.0.0.1:%PORT%/#view=settings
echo    · 停止：运行 stop.cmd，或右键桌宠选「退出」
echo.
ping -n 5 127.0.0.1 >nul
endlocal
