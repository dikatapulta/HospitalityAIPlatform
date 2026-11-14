# shellcheck shell=bash
# Свои авторы (#1059; подробности — приватная sec#18). Репозиторий публичный: PR и issue
# может открыть посторонний, а окно, взявшее его PR в ревью, исполнило бы его тесты на машине
# разработки. Пул ведёт только работу своих аккаунтов: владелец, аккаунт-ревьюер
# (docs/runbooks/review-process.md) и Dependabot — так их называет `gh` (`author.login`).
# PR из форка — посторонний при любом авторе. Подключается из ops/queue.sh (раздел 0,
# `--free`, «занято», новые issue) и ops/pool.sh (enqueue, merge-one, approve).
# Для тестов список переопределяется: POOL_TRUSTED_AUTHORS="…".

POOL_TRUSTED_AUTHORS="${POOL_TRUSTED_AUTHORS:-dikatapulta supertazick-oss app/dependabot}"

trusted_author() { case " $POOL_TRUSTED_AUTHORS " in *" $1 "*) [ -n "$1" ] ;; *) return 1 ;; esac; }
