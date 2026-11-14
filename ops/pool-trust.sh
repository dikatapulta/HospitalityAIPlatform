# shellcheck shell=bash
# Свои авторы (sec#18, #1059). Репозиторий публичный, а чужой код на машине разработки
# не исполняется. Пул ведёт только работу своих аккаунтов: владелец, аккаунт-ревьюер
# (docs/runbooks/review-process.md) и Dependabot — так их называет `gh` (`author.login`).
# PR из форка — посторонний при любом авторе. Подключается из ops/queue.sh (раздел 0,
# `--free`, «занято», новые issue) и ops/pool.sh (enqueue, merge-one, approve).
# Для тестов список переопределяется: POOL_TRUSTED_AUTHORS="…".

POOL_TRUSTED_AUTHORS="${POOL_TRUSTED_AUTHORS:-dikatapulta supertazick-oss app/dependabot}"

trusted_author() { case " $POOL_TRUSTED_AUTHORS " in *" $1 "*) [ -n "$1" ] ;; *) return 1 ;; esac; }
