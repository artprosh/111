"""Telegram Mini App (Web App) для GasRelax_bot.

Состав пакета:
- auth.py    — проверка Telegram WebApp initData (только стандартная библиотека);
- core.py    — вся логика API поверх того же db.Database, что и у бота
               (без aiogram/aiohttp, поэтому тестируется и запускается локально);
- bridge.py  — «мост» к функциям уведомлений из main.py (рабочий режим, aiogram);
- server.py  — aiohttp-сервер (рабочий режим, запускается из main.py);
- static/    — фронтенд: index.html, app.js, style.css (без сборки).
"""
