@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src"
set "PYTHON="

if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if defined PYTHON goto havepython
where python >nul 2>nul
if not errorlevel 1 set "PYTHON=python"
if defined PYTHON goto havepython
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if defined PYTHON goto havepython

echo [ERROR] Python not found.
echo         Install Python 3.10+ or create .venv here (see README.md).
echo.
pause
exit /b 9009

:havepython
rem A stale WindowsApps alias or broken install can answer "where python" and then fail.
%PYTHON% -V >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON=py -3"
)
%PYTHON% -V >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python cannot start: %PYTHON%
  echo         Repair the install or create .venv in this folder.
  echo.
  pause
  exit /b 9009
)

if "%~1"=="" goto web

%PYTHON% -m llm_stress %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [FAILED] exit code %CODE% - press any key to close.
  pause >nul
)
exit /b %CODE%

:web
echo LLM Stress Testing - Web UI
echo   URL   http://127.0.0.1:8021
echo   Close this window to stop the server.
echo.
%PYTHON% -m llm_stress serve --open
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [FAILED] exit code %CODE% - press any key to close.
  pause >nul
)
exit /b %CODE%
