#!/usr/bin/env bash
# Образ для отеля (#1048): production-образ коммита в GHCR — собрать, только если его там нет.
#
# Зовёт job release-image в .github/workflows/ci.yml на ветке починок `release/*` и при
# ручном запуске CI на метке кандидата `pilot-*` (docs/runbooks/pilot-copy.md, «Выпуск
# в отель»). Образ коммита из main обычно уже собрал deploy-staging — тогда он не
# пересобирается: на копию отеля едет ровно тот артефакт, что основатель проверил на
# staging, а не новая сборка того же кода с другим дайджестом.
#
# Использование: ops/release-image.sh ghcr.io/<owner>/hospitality-app:<sha>
# Код 0 — образ в GHCR есть (был или собран); 1 — GHCR не ответил внятно или сборка
# упала; 2 — не указан образ.
set -euo pipefail

image="${1:-}"
if [ -z "$image" ]; then
    echo "ОШИБКА: не указан образ (ghcr.io/<owner>/hospitality-app:<sha>)." >&2
    exit 2
fi

# «Образа нет» отличаем от сбоя сети или входа: на сбое пересборка перезаписала бы
# проверенный на staging образ — лучше красный прогон и повтор руками.
if answer="$(docker manifest inspect "$image" 2>&1)"; then
    echo "==> Образ уже есть в GHCR, не пересобираю: $image"
    exit 0
fi
case "$answer" in
    *"manifest unknown"* | *"no such manifest"*) ;;
    *)
        echo "ОШИБКА: GHCR не ответил, есть ли образ $image:" >&2
        printf '%s\n' "$answer" >&2
        exit 1
        ;;
esac

echo "==> Образа нет — собираю: $image"
docker build -f ops/Dockerfile --target production -t "$image" .
docker push "$image"
echo "==> OK: образ собран и лежит в GHCR: $image"
