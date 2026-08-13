@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   RWKV6 模型自动导入 Ollama 脚本
echo ============================================================
echo.

set MODEL_DIR=models\rwkv6
set GGUF_FILE=%MODEL_DIR%\RWKV6-7B-v3-porn-chat-pro-IQ4_XS.gguf
set MODEL_NAME=rwkv6-novel:7b

rem ====== 检查 GGUF 文件 ======
if not exist "%GGUF_FILE%" (
    echo [错误] 找不到 GGUF 文件: %GGUF_FILE%
    echo.
    echo 请先手动下载文件，放到本目录下的 %MODEL_DIR% 文件夹：
    echo.
    echo   浏览器打开以下任意一个链接：
    echo   (1) https://hf-mirror.com/btaskel/RWKV6-7B-v3-porn-chat-pro-GGUF/resolve/main/RWKV6-7B-v3-porn-chat-pro-IQ4_XS.gguf
    echo   (2) https://huggingface.co/btaskel/RWKV6-7B-v3-porn-chat-pro-GGUF/resolve/main/RWKV6-7B-v3-porn-chat-pro-IQ4_XS.gguf
    echo.
    echo   下载完成后，将此 .gguf 文件放到:
    echo     %CD%\%MODEL_DIR%\
    echo.
    echo   然后重新运行本脚本！
    pause
    exit /b 1
)

echo [OK] GGUF 文件已找到: %GGUF_FILE%
echo.

rem ====== 创建 Modelfile ======
echo [1/3] 创建 Modelfile...
set MF=%MODEL_DIR%\Modelfile
(
echo FROM %GGUF_FILE%
echo.
echo TEMPLATE """{{- if .System }}System: {{ .System }}
echo {{ end }}
echo {{- range $i, $_ := .Messages }}
echo {{- $last := eq (len (slice $.Messages $i)) 1}}
echo {{- if eq .Role "user" }}User: {{ .Content }}
echo {{- else if eq .Role "assistant" }}{{ if $last }}Assistant: {{ .Content }}{{ else }}Assistant: {{ .Content }}
echo {{ end }}
echo {{- end }}
echo {{- end }}
echo {{- if .Response }}Assistant: {{ .Response }}{{ end }}"""
echo PARAMETER stop "User:"
echo PARAMETER stop "Assistant:"
echo PARAMETER temperature 0.8
echo PARAMETER top_p 0.9
echo PARAMETER num_ctx 8192
) > "%MF%"
echo [OK] Modelfile 已创建
echo.

rem ====== 导入到 Ollama ======
echo [2/3] 导入模型到 Ollama（约需 1-3 分钟）...
ollama create %MODEL_NAME% -f "%MF%"
if %ERRORLEVEL% NEQ 0 (
    echo [失败] 模型导入失败，请检查 Ollama 是否正在运行！
    echo   尝试运行: ollama serve
    pause
    exit /b 1
)
echo [OK] 模型导入成功！
echo.

rem ====== 验证 ======
echo [3/3] 验证模型...
ollama list | findstr "rwkv6-novel"
if %ERRORLEVEL% NEQ 0 (
    echo [警告] 模型列表中未找到 rwkv6-novel:7b，但导入可能已成功
) else (
    echo [OK] 模型已就绪！
)
echo.
echo ============================================================
echo   完成！现在可以这样使用模型：
echo.
echo     ollama run rwkv6-novel:7b
echo.
echo   或者在小鼠修改工具的"设置 - AI连接"中选择此模型
echo ============================================================
pause
