@echo off
chcp 65001 > nul
title RP Lab
cd /d "%~dp0"

set PORT=7861

echo ============================
echo   RP Lab
echo ============================
echo   [1] Chi may nay (127.0.0.1)            - mac dinh
echo   [2] Tailscale (bind IP Tailscale)      - truy cap tu xa, an toan
echo   [3] Listen tat ca (0.0.0.0)            - LAN + Tailscale, KHONG an toan neu mang la
echo.
choice /c 123 /t 5 /d 1 /n /m "Chon che do (tu dong 1 sau 5s): "
set MODE=%errorlevel%

if "%MODE%"=="1" goto local
if "%MODE%"=="2" goto tailscale
if "%MODE%"=="3" goto listenall
goto local

:local
start "" http://127.0.0.1:%PORT%
python server.py --port %PORT%
goto end

:tailscale
set TSIP=
for /f "delims=" %%i in ('tailscale ip -4 2^>nul') do if not defined TSIP set TSIP=%%i
if not defined TSIP (
    echo [LOI] Khong lay duoc IP Tailscale. Hay mo Tailscale va dang nhap, hoac dung che do 3.
    goto end
)
echo Tailscale IP: %TSIP%
echo Truy cap tu xa: http://%TSIP%:%PORT%
start "" http://%TSIP%:%PORT%
python server.py --host %TSIP% --port %PORT%
goto end

:listenall
echo Truy cap tu xa: http://^<IP-Tailscale^>:%PORT%  (xem bang: tailscale ip -4)
start "" http://127.0.0.1:%PORT%
python server.py --listen --port %PORT%
goto end

:end
pause
