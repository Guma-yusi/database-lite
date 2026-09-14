@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo [诊断] 工作目录: %cd%
echo [诊断] 服务会以前台模式运行；成功启动后终端持续占用是正常现象。
if not exist "server.py" (
    echo [错误] 当前目录未找到 server.py，请确认 start.bat 与 server.py 位于同一项目根目录。
    goto :done
)

echo [诊断] 正在检查 py Launcher...
where py >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('where py 2^>nul') do (
        echo [诊断] 命中解释器: %%I
        goto :check_py_version
    )
)
echo [诊断] 未在 PATH 中找到 py
goto :check_python

:check_py_version
py -3 --version
if errorlevel 1 (
    echo [诊断] py 已找到，但无法解析可用的 Python 3，继续尝试 python...
) else (
    echo [诊断] 启动命令: py -3 -u server.py
    py -3 -u server.py
    goto :report_exit
)

:check_python
echo [诊断] 正在检查 python...
where python >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        echo [诊断] 命中解释器: %%I
        goto :check_python_version
    )
)
echo [诊断] 未在 PATH 中找到 python
goto :check_python3

:check_python_version
python --version
if errorlevel 1 (
    echo [诊断] python 已找到，但执行失败，继续尝试 python3...
) else (
    echo [诊断] 启动命令: python -u server.py
    python -u server.py
    goto :report_exit
)

:check_python3
echo [诊断] 正在检查 python3...
where python3 >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('where python3 2^>nul') do (
        echo [诊断] 命中解释器: %%I
        goto :check_python3_version
    )
)
echo [诊断] 未在 PATH 中找到 python3
goto :check_bundled

:check_python3_version
python3 --version
if errorlevel 1 (
    echo [诊断] python3 已找到，但执行失败，继续尝试内置 Python...
) else (
    echo [诊断] 启动命令: python3 -u server.py
    python3 -u server.py
    goto :report_exit
)

:check_bundled
set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
echo [诊断] 正在检查内置 Python: "%BUNDLED_PY%"
if exist "%BUNDLED_PY%" (
    "%BUNDLED_PY%" --version
    if errorlevel 1 (
        echo [诊断] 内置 Python 存在，但无法正常执行。
    ) else (
        echo [诊断] 启动命令: "%BUNDLED_PY%" -u server.py
        "%BUNDLED_PY%" -u server.py
        goto :report_exit
    )
) else (
    echo [诊断] 未找到内置 Python
)

echo [错误] 未找到可用的 Python 3 解释器，请先安装 Python 3.11 或更高版本，并确保 py 或 python 可用。
goto :done

:report_exit
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" (
    echo [诊断] server.py 已正常退出。
) else (
    echo [错误] server.py 启动失败或异常退出，退出码: %EXIT_CODE%
    echo [提示] 请查看上方的 Traceback、端口占用或依赖缺失信息。
)

:done
pause
