# shellcheck shell=bash
# «Занять номер» (#568, решение основателя 30.10): номер миграции, спеки и версия политики
# гостя не достаются двум окнам. Подключается из ops/pool.sh — замки и владелец там.
# Шаг окна — docs/QUEUE.md, «Пул окон», шаг «Работа». Тесты — tests/test_pool_reserve.py.
#
# Номер — следующий за наибольшим занятым: в свежем origin/main, в голове каждого
# открытого PR (черновики тоже) и в живых замках reserved-<вид>-<номер> других окон.
# Открытые PR нужны потому, что окно уходит после ready и его замок снимается, а номер
# живёт в PR до merge; замок закрывает только промежуток между reserve и push.
# Два окна, посчитавшие одно и то же, разводит mkdir: проигравшее берёт следующий.

reserve_kind() { case "$1" in migration | spec | policy) ;; *) return 1 ;; esac; }

reserve_label() {
    case "$1" in
    migration) echo "миграция" ;;
    spec) echo "спека" ;;
    policy) echo "политика гостя" ;;
    esac
}

reserve_locks() { # замки вида $1
    local d
    for d in "$POOL_DIR/reserved-$1-"*; do
        [ -d "$d" ] || continue
        case "$d" in *.break) continue ;; esac
        echo "$d"
    done
}

# Номера вида $1 в commit $2, по одному на строку: у миграции и спеки — четырёхзначное
# начало имени файла, у политики — «X.Y» из строки «Версия X.Y,» русского текста.
numbers_in() {
    case "$1" in
    migration)
        git ls-tree --full-tree --name-only "$2" alembic/versions/ |
            sed -nE 's#^alembic/versions/([0-9]{4})_[^/]*\.py$#\1#p'
        ;;
    spec)
        git ls-tree --full-tree --name-only "$2" docs/specs/ |
            sed -nE 's#^docs/specs/([0-9]{4})-[^/]*\.md$#\1#p'
        ;;
    policy)
        { git show "$2:docs/legal/privacy-policy.md" 2>/dev/null || true; } |
            sed -nE 's/^Версия ([0-9]+\.[0-9]+),.*/\1/p'
        ;;
    esac
}

# Свежий main первой строкой (FETCH_HEAD хранит порядок ссылок командной строки), за ним
# головы открытых PR — commit по одному на строку. Сбой gh или git — код 1: номер без них
# мог бы совпасть с номером из PR.
open_heads() {
    local prs n refs=""
    prs="$(gh pr list --state open --limit 200 --json number --jq '.[].number')" || return 1
    for n in $prs; do
        case "$n" in *[!0-9]*) return 1 ;; esac
        refs="$refs refs/pull/$n/head"
    done
    # shellcheck disable=SC2086 # refs — номера PR, проверены выше
    git fetch -q origin main $refs 2>/dev/null || return 1
    cut -f1 "$(git rev-parse --git-path FETCH_HEAD)"
}

top_of() { sed '/^$/d' | sort -t. -k1,1n -k2,2n | tail -n 1; }

above() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | top_of)" = "$1" ]; }

reserve_next() { # вид, наибольший занятый (пусто — ничего не занято) → следующий
    case "$1" in
    policy)
        [ -n "$2" ] || return 1
        echo "${2%%.*}.$((10#${2#*.} + 1))"
        ;;
    *) printf '%04d\n' $((10#${2:-0} + 1)) ;;
    esac
}

# Свой номер окно получает снова, пока main его не догнал; догнал (влился свой PR серии
# или чужой с бо́льшим номером) — замок снимается и номер выдаётся заново.
cmd_reserve() {
    if [ $# -ne 1 ] || ! reserve_kind "$1"; then
        die "использование: ops/pool.sh reserve migration | spec | policy"
    fi
    need_owner
    local kind="$1" heads main_top d n taken next
    heads="$(open_heads)" ||
        die "не получил открытые PR или свежий main (gh, git fetch) — номер не выдан, повтори"
    main_top="$(numbers_in "$kind" "${heads%%$'\n'*}" | top_of)"
    for d in $(reserve_locks "$kind"); do
        n="${d##*/reserved-"$kind"-}"
        if is_mine "$d"; then
            if above "$n" "$main_top"; then echo "$n"; return 0; fi
            rm -rf "$d"
        else
            break_if_stale "$d" || true # окно закрыто — номер держит его PR, если он есть
        fi
    done
    taken="$(
        for n in $heads; do numbers_in "$kind" "$n"; done
        for d in $(reserve_locks "$kind"); do lock_stale "$d" || echo "${d##*/reserved-"$kind"-}"; done
    )"
    next="$(reserve_next "$kind" "$(printf '%s\n' "$taken" | top_of)")" ||
        die "в main нет строки «Версия X.Y,» в docs/legal/privacy-policy.md — номер не выдан"
    until acquire "reserved-$kind-$next"; do
        [ -d "$POOL_DIR/reserved-$kind-$next" ] || die "замок номера не создаётся в ${POOL_DIR} — повтори"
        next="$(reserve_next "$kind" "$next")"
    done
    echo "$next"
}

reserve_status() {
    local kind d
    for kind in migration spec policy; do
        for d in $(reserve_locks "$kind"); do
            echo "    занят номер: $(reserve_label "$kind") ${d##*/reserved-"$kind"-} — окно $(window_of "$(lock_owner "$d")"), $(lock_state "$d")"
        done
    done
}
