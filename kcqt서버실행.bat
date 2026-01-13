@echo off
title Django Server - KCQT CSI

:: 1. 한글 경로 문제를 피하기 위해 이 파일이 있는 폴더로 작업 경로 고정
cd /d "%~dp0"

echo ==========================================
echo    권한 설정 적용 및 가상환경 활성화 중...
echo ==========================================

:: 2. 요청하신 권한 설정(RemoteSigned) 후 가상환경 활성화 및 서버 실행
:: & { ... } 구문을 사용해 권한을 먼저 풀고 가상환경을 켭니다.
powershell -Command "& {Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process; . .\venv\Scripts\Activate.ps1; python manage.py runserver 0.0.0.0:8000}"

pause