@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Y-code 升级后一键验证脚本
echo ============================================
echo.

echo [1/4] 同步最新系统提示词到 %%USERPROFILE%%\.ycode ...
if not exist "%USERPROFILE%\.ycode" mkdir "%USERPROFILE%\.ycode"
if exist "%USERPROFILE%\.ycode\system_prompt.md" copy /y "%USERPROFILE%\.ycode\system_prompt.md" "%USERPROFILE%\.ycode\system_prompt.md.bak" >nul
copy /y "system_prompt.md" "%USERPROFILE%\.ycode\system_prompt.md" >nul
echo       完成（旧提示词已备份为 system_prompt.md.bak）
echo.

echo [2/4] 运行完整测试套件（含沙盒里跑不了的 8 个模块）...
venv\Scripts\python -m pytest -q
if errorlevel 1 (
    echo.
    echo       [X] 有测试失败：请把上面的输出复制给 Claude 排查
    pause
    exit /b 1
)
echo       全部测试通过
echo.

echo [3/4] 重新构建 GUI 前端（Vue）...
cd frontend
call npm run build
if errorlevel 1 (
    echo.
    echo       [X] 前端构建失败：请确认已安装 Node.js 20 或更高版本
    cd ..
    pause
    exit /b 1
)
cd ..
echo       前端构建完成
echo.

echo [4/4] 验证完毕！
echo.
echo   现在可以：
echo     体验 TUI：   venv\Scripts\python main.py
echo     体验 GUI：   venv\Scripts\python gui_main.py
echo     生成安装包： venv\Scripts\python build_release.py
echo.
pause
