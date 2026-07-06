# Образ приложения для локальной разработки (Task 0004).
# Прод-образ появится отдельно при первом деплое (Task 0006) — здесь только dev-контур.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev]"

# Плейсхолдер: приложения ещё нет (появится в Task 0005 — FastAPI + composition root).
# Volume с исходниками уже смонтирован (docker-compose.yml) — когда app.py появится,
# здесь достаточно заменить команду на `uvicorn hospitality.app:app --reload --host 0.0.0.0`.
CMD ["sleep", "infinity"]
