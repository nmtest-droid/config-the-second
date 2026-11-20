@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Run from the project directory, regardless of the caller's directory.
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto directory_error
set "PYTHONPATH=%PROJECT_ROOT%src"
set "PYTHONUTF8=1"

rem Prefer the Python launcher; fall back to python on PATH.
where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_LAUNCHER=py"
    set "PYTHON_ARGS=-3"
) else (
    where python >nul 2>&1
    if errorlevel 1 goto python_error
    set "PYTHON_LAUNCHER=python"
    set "PYTHON_ARGS="
)

rem Preserve normal CLI usage when arguments are supplied.
if not "%~1"=="" goto run_with_arguments

rem No arguments: offer an interactive launch with a working demo by default.
echo.
echo Dependency Graph - interactive start
echo Press Enter to accept the value in square brackets.
echo.

set "PACKAGE=A"
set "INPUT_VALUE="
set /p "INPUT_VALUE=Package ID [A]: "
if defined INPUT_VALUE set "PACKAGE=%INPUT_VALUE%"

set "REPOSITORY=tests\fixtures\graphs\branching.json"
set "INPUT_VALUE="
set /p "INPUT_VALUE=Repository URL or file [tests\fixtures\graphs\branching.json]: "
if defined INPUT_VALUE set "REPOSITORY=%INPUT_VALUE%"

set "TEST_REPO_MODE=on"
set "INPUT_VALUE="
set /p "INPUT_VALUE=Test repository mode (on/off) [on]: "
if defined INPUT_VALUE set "TEST_REPO_MODE=%INPUT_VALUE%"

set "VERSION=1.0.0"
set "INPUT_VALUE="
set /p "INPUT_VALUE=Package version [1.0.0]: "
if defined INPUT_VALUE set "VERSION=%INPUT_VALUE%"

set "OUTPUT_FILE=dependency-graph.svg"
set "INPUT_VALUE="
set /p "INPUT_VALUE=Output SVG file [dependency-graph.svg]: "
if defined INPUT_VALUE set "OUTPUT_FILE=%INPUT_VALUE%"

set "LOAD_ORDER="
set /p "LOAD_ORDER=Show dependency load order? (y/N): "

echo.
echo Starting dependency graph application...
if /i "%LOAD_ORDER%"=="y" goto run_interactive_with_order
if /i "%LOAD_ORDER%"=="yes" goto run_interactive_with_order

"%PYTHON_LAUNCHER%" %PYTHON_ARGS% -m depgraph ^
    --package "%PACKAGE%" ^
    --repository "%REPOSITORY%" ^
    --test-repo-mode "%TEST_REPO_MODE%" ^
    --version "%VERSION%" ^
    --output "%OUTPUT_FILE%"
goto show_result

:run_interactive_with_order
"%PYTHON_LAUNCHER%" %PYTHON_ARGS% -m depgraph ^
    --package "%PACKAGE%" ^
    --repository "%REPOSITORY%" ^
    --test-repo-mode "%TEST_REPO_MODE%" ^
    --version "%VERSION%" ^
    --output "%OUTPUT_FILE%" --show-load-order

goto show_result

:run_with_arguments
"%PYTHON_LAUNCHER%" %PYTHON_ARGS% -m depgraph %*
exit /b %ERRORLEVEL%

:show_result
set "APP_EXIT_CODE=%ERRORLEVEL%"
echo.
if "%APP_EXIT_CODE%"=="0" (
    echo Application finished successfully.
) else (
    echo Application failed with exit code %APP_EXIT_CODE%.
)
echo.
pause
exit /b %APP_EXIT_CODE%

:python_error
echo ERROR: Python 3 was not found.
echo Install Python 3 and add it to PATH, then run this file again.
pause
exit /b 9009

:directory_error
echo ERROR: Could not switch to the project directory.
pause
exit /b 1
