@echo off
chcp 65001 > nul
title RP Lab
cd /d "%~dp0"
start "" http://127.0.0.1:7861
python server.py --port 7861
pause
