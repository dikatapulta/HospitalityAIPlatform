# Образ приложения — один файл, две цели (P-12: один канонический способ).
#   base       — общий слой: зависимости рантайма + установленный пакет.
#   dev        — + dev-инструменты и --reload; используется docker-compose.yml (Task 0004).
#   production — самодостаточный образ без volume/reload; собирается в CI и катится
#                на staging (Task 0006). Это цель по умолчанию: `docker build` без
#                --target собирает production.
#
# Сборка dev: docker compose ... build app        (compose задаёт target: dev)
# Сборка prod: docker build -f ops/Dockerfile --target production .

FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
COPY pyproject.toml ./
COPY src ./src
# Миграции — часть образа: деплой запускает `alembic upgrade head` в контейнере
# приложения (ops/deploy/deploy.sh, Task 0009) — БД staging не открыта наружу.
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --upgrade pip && pip install .

# --- dev: hot-reload для локальной среды ------------------------------------
# Установка editable + dev-зависимостей поверх base; исходники монтируются
# volume'ом (docker-compose.yml), поэтому --reload видит правки без пересборки.
FROM base AS dev
RUN pip install -e ".[dev]"
CMD ["uvicorn", "hospitality.app:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]

# --- production: то, что бежит на staging/проде -----------------------------
# Без volume и без --reload: код уже внутри образа (COPY + pip install в base).
# Непривилегированный пользователь — контейнер не работает от root.
FROM base AS production
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "hospitality.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
