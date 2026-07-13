"""Канал Telegram (Task 0016): вебхук, нормализация, персистентность, ответы.

Публичная точка подключения — `router` (webhook), его импортирует composition
root (`hospitality/app.py`). Модели `Conversation`/`Message` регистрирует
`alembic/env.py`. Остальное — приватные детали адаптера канала.
"""
