#!/bin/sh
# Установка зависимостей строго по uv.lock (#305, ADR-026) — единственный способ
# ставить зависимости проекта: make venv, CI, smoke и образ зовут этот скрипт,
# а не `pip install` сами (щит — tests/test_dependency_lock.py).
#
#   ops/install-deps.sh runtime  — рантайм-зависимости и пакет (не editable), uv убирается:
#                                  образ (ops/Dockerfile), миграции и алерт в CI
#   ops/install-deps.sh dev      — то же плюс dev-инструменты, пакет editable, uv остаётся
#                                  (им `make lock` пересобирает лок): make venv, check в CI
#
# Ставит в Python из переменной PYTHON (по умолчанию python3 из PATH): make venv
# передаёт PYTHON=.venv/bin/python. Порядок: точная версия uv → список версий
# с хэшами из uv.lock → pip ставит ровно его → пакет без зависимостей → pip check.
set -eu

# Версия uv — осознанным PR, как и любая зависимость (ADR-026, «Последствия»).
UV_VERSION=0.13.7

mode=${1:-}
case "$mode" in
  runtime) extras="" ;;
  dev) extras="--extra dev" ;;
  *)
    echo "использование: ops/install-deps.sh runtime|dev" >&2
    exit 2
    ;;
esac

PYTHON=${PYTHON:-python3}
cd "$(dirname "$0")/.."

"$PYTHON" -m pip install --quiet --disable-pip-version-check "uv==$UV_VERSION"

requirements=$(mktemp)
trap 'rm -f "$requirements"' EXIT

# --locked: pyproject.toml поменяли, а uv.lock не пересобрали — ошибка, а не тихая
# установка непроверенного (без флага uv молча пересобрал бы лок здесь же). Хэши
# uv export пишет сам; --no-emit-project — пакет ставится ниже отдельно. $extras
# без кавычек: пустой — ни одного аргумента. Команда — одной строкой: её флаги
# сверяет tests/test_dependency_lock.py.
# shellcheck disable=SC2086
if ! "$PYTHON" -m uv export --locked --no-emit-project --format requirements-txt $extras --output-file "$requirements" --quiet; then
  echo "install-deps: uv export не прошёл (причина — строками выше); обычно это uv.lock, не пересобранный после правки pyproject.toml: make lock и закоммить uv.lock (ADR-026)" >&2
  exit 1
fi

# --require-hashes: каждый пакет — точная версия из лока и проверенный хэш файла;
# пакета, которого в списке нет, pip не поставит, а упадёт.
"$PYTHON" -m pip install --quiet --disable-pip-version-check --require-hashes -r "$requirements"

if [ "$mode" = dev ]; then
  "$PYTHON" -m pip install --quiet --disable-pip-version-check --no-deps -e .
else
  "$PYTHON" -m pip install --quiet --disable-pip-version-check --no-deps .
  "$PYTHON" -m pip uninstall --quiet --yes uv
fi

# Требования пакета удовлетворены тем, что встало, — иначе красный шаг здесь,
# а не ImportError в рантайме.
"$PYTHON" -m pip check
