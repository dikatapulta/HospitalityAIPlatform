# Образ приложения для локальной разработки (Task 0004).
# Прод-образ появится отдельно при первом деплое (Task 0006) — здесь только dev-контур.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev]"

# Composition root — src/hospitality/app.py (Task 0005). Volume с исходниками
# смонтирован (docker-compose.yml), поэтому --reload видит правки без пересборки.
CMD ["uvicorn", "hospitality.app:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]
