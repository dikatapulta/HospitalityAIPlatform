# Образ приложения — один файл, две цели (P-12: один канонический способ).
#   base       — общий слой: зависимости рантайма + установленный пакет.
#   dev        — + dev-инструменты и --reload; используется docker-compose.yml (Task 0004).
#   production — самодостаточный образ без volume/reload; собирается в CI и катится
#                на staging (Task 0006). Это цель по умолчанию: `docker build` без
#                --target собирает production.
#
# Сборка dev: docker compose ... build app        (compose задаёт target: dev)
# Сборка prod: docker build -f ops/Dockerfile --target production .
#
# Оба CMD запускают uvicorn с --no-proxy-headers (issue #207): у uvicorn эта
# прослойка включена ПО УМОЛЧАНИЮ и подменяет адрес клиента значением из
# X-Forwarded-For, которое присылает кто угодно; настоящий адрес приложение
# берёт само из CF-Connecting-IP с проверкой соседа по сокету — почему именно
# так, написано в src/hospitality/shared/clientip.py. Флаг стоит в образе, а не
# только в ops/deploy/docker-compose.staging.yml, потому что command в compose
# переопределяет CMD целиком: любой запуск без такого override (локальный
# ops/docker-compose.yml, будущий прод) иначе поднялся бы с прослойкой.

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
# Юридические тексты — часть образа: страница /legal/privacy отдаёт политику
# из docs/legal/privacy-policy.md в рантайме (spec 0029 §2), а на неё ссылается
# обязательный текст согласия гостя. Без этого COPY ссылка ведёт в 500.
COPY docs/legal ./docs/legal
# Профили онбординга — часть образа: команда `hospitality.tools.onboard_tenant`
# (issue #123) запускается внутри контейнера на staging/проде и читает состав
# служб отеля из файла. Без этого COPY онбординг требовал бы заливки файла на
# сервер руками.
COPY ops/onboarding ./ops/onboarding
RUN pip install --upgrade pip && pip install .
# Проверки конфигурации — ДО запуска любой команды образа (issue #267):
# ENTRYPOINT прогоняет src/hospitality/preflight.py и лишь потом exec'ает CMD
# (или command из compose, который CMD переопределяет целиком). Без этого шага
# fail-fast ронял только рабочий процесс, а супервизоры uvicorn (--workers у
# production, --reload у dev) переживали смерть ребёнка и перезапускали его в
# цикле — контейнер числился живым. Подробности — в самом entrypoint.sh.
# ENTRYPOINT стоит в base: обе цели ниже наследуют его вместе со своим CMD.
COPY ops/entrypoint.sh ./ops/entrypoint.sh
RUN chmod 755 ./ops/entrypoint.sh
ENTRYPOINT ["/app/ops/entrypoint.sh"]

# --- dev: hot-reload для локальной среды ------------------------------------
# Установка editable + dev-зависимостей поверх base; исходники монтируются
# volume'ом (docker-compose.yml), поэтому --reload видит правки без пересборки.
FROM base AS dev
RUN pip install -e ".[dev]"
CMD ["uvicorn", "hospitality.app:app", "--reload", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

# --- production: то, что бежит на staging/проде -----------------------------
# Без volume и без --reload: код уже внутри образа (COPY + pip install в base).
# Непривилегированный пользователь — контейнер не работает от root.
FROM base AS production
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "hospitality.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-proxy-headers"]
