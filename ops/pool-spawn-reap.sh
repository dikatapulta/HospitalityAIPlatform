# shellcheck shell=bash
# Разбор закончившегося безголового окна и простой пула. Вынесено из ops/pool-spawn.sh (#1167),
# там же записи окон, сторож и его ход. Подключается из ops/pool.sh после него. Тесты —
# tests/test_pool_spawn.py, tests/test_pool_spawn_limit.py и tests/test_pool_spawn_break.py.
#
# Обрыв с кодом 0 (#1167, 27.11). `claude -p` снимал фоновые команды через 10 минут после
# последнего хода и выходил так, будто окно закончило. Сон Mac растягивает эти 10 минут
# на любое ожидание. Теперь потолок снят переменной в launch, но выход посреди работы остаётся
# возможным. Закончившее окно отпускает свои замки (release, шаг 5 «Рабочего окна»). Если
# окно оставило за собой номер, турникет или заявку, работа брошена, и окно возобновляется
# как оборвавшееся.

STALL_SEC="${POOL_STALL_SEC:-600}"
LAST_TICK="${POOL_LAST_TICK:-0}" # конец прошлого хода; тесты задают его, чтобы изобразить сон Mac
STALL_FROM=""
STALL_TILL=""

# Слова лимита в выводе последнего запуска (docs: errors, «Usage limits»): печатает model —
# кончилась модель окна (CLI 12.11: «You've reached your Fable limit. Switch to another
# model…»), all — подписка целиком; 1 — лимита нет.
limit_hit() {
    local log="$WORKERS/$1.log" pos tail_text
    pos="$(rec_get "$1" logpos)"
    tail_text="$(tail -c +$((${pos:-0} + 1)) "$log" 2>/dev/null || true)"
    if grep -qiE '(fable|opus|sonnet|haiku) ([a-z]+ )?limit' <<<"$tail_text"; then
        echo model
    elif grep -qiE '(hit|reached) your [a-z ]*limit|usage limit' <<<"$tail_text"; then
        echo all
    else
        return 1
    fi
}

spawn_journal() { # работа, сделано, от основателя — запись сторожа в журнал основателя
    local file
    file="$(journal_path)" || return 0
    printf '%s\n' "$(date '+%d.%m %H:%M') · Сторож · ${1}" "Сделано: ${2}" "От тебя: ${3}" "" >>"$file"
}

# Замки, которые окно оставило за собой (номер, турникет, заявки): их владелец — его claude.
left_locks() { # pid
    local d out=""
    case "$1" in '' | *[!0-9]*) return 0 ;; esac
    for d in "$POOL_DIR"/*/; do
        d="${d%/}"
        if [ "$(lock_owner "$d")" = "$1" ]; then out="${out} ${d##*/}"; fi
    done
    echo "${out# }"
}

# Окно закончилось — разобрать: остановлено паузой, ушло, упёрлось в лимит или оборвалось.
reap() {
    local id="$1" r="$WORKERS/$1" code kind started tries lim model left
    code="$(rec_get "$id" exit)" kind="$(rec_get "$id" kind)" started="$(rec_get "$id" started)"
    left="$(left_locks "$(rec_get "$id" pid)")"
    rm -f "$r/exit" "$r/pid" "$r/wrap"
    # Остановленное паузой продолжается всегда: после SIGINT claude может выйти и с кодом 0.
    if [ -f "$r/stop" ]; then
        rm -f "$r/stop"
        rec_put "$id" want "пауза"
        say "окно ${id}: остановлено паузой — продолжит после ops/pool.sh resume"
        return 0
    fi
    if [ "$code" = 0 ] && [ -n "$left" ]; then code="0, замки не отпущены: ${left}"; fi
    if [ "$code" = 0 ]; then
        # Короткий запуск без метки — тоже «брать нечего»: иначе окно, забывшее idle,
        # перезапускалось бы каждую минуту. Снимок — с запуска окна, до того как оно прочло
        # пул: перемены за его жизнь (PR вышел из очереди слияния, снята заявка) сторож иначе
        # принял бы за «ничего не менялось» (#1109). Снимка нет — нынешний; не снялся — метки нет.
        if [ -f "$r/idle" ] || [ $(($(now) - ${started:-0})) -lt "$SHORT_RUN_SEC" ]; then
            if [ -s "$r/snap" ]; then
                cp "$r/snap" "$WORKERS/.idle-$kind"
            else
                snapshot idle >"$WORKERS/.idle-$kind" 2>/dev/null || rm -f "$WORKERS/.idle-$kind"
            fi
        fi
        rm -rf "$r"
        say "окно ${id}: закончило"
        return 0
    fi
    if lim="$(limit_hit "$id")"; then
        if [ "$lim" = model ]; then
            model="$(model_of "$id")"
            hold "$model"
            rec_put "$id" want "лимит модели ${model}"
            say "окно ${id}: лимит модели — окна ${model} стоят до $(clock "$(held_till "$model")" '%H:%M'), остальные идут; потом продолжит это окно"
        else
            hold
            rec_put "$id" want "лимит подписки"
            say "окно ${id}: лимит подписки — сторож стоит до $(clock "$(held_till)" '%H:%M'), потом продолжит это окно"
        fi
        return 0
    fi
    tries=$(($(rec_get "$id" tries) + 1))
    if [ "$tries" -gt "$RESUME_MAX" ]; then
        hold
        spawn_journal "безголовое окно ${id} оборвалось $((tries - 1)) раза подряд — снято" \
            "сторож перестал его поднимать и стоит $((LIMIT_HOLD_SEC / 60)) мин, чтобы не жечь подписку на сломанном запуске" \
            "посмотреть лог ${WORKERS}/${id}.log; если окно держало PR — его продолжит следующее окно по комментарию «Передача» или заявке"
        rm -rf "$r"
        say "окно ${id}: оборвалось ${RESUME_MAX} раза подряд — снято, запись в журнале"
        return 0
    fi
    rec_put "$id" tries "$tries"
    rec_put "$id" want "обрыв, код ${code:-нет}, попытка ${tries} из ${RESUME_MAX}"
    say "окно ${id}: оборвалось (код ${code:-нет}) — возобновлю"
}

# Простой пула (#1167). Сторож ходит раз в минуту, и разрыв между ходами дольше STALL_SEC
# бывает, только когда Mac спал: сторож и окна стояли вместе с ним. 27.11 пул так стоял 1 ч 50 мин,
# а в журнале не было ни строки. Короткие служебные пробуждения Mac сна не прерывают: разрывы
# подряд считаются одним простоем. Запись в журнал — на первом обычном ходу после него.
pool_stall() {
    local t
    t="$(now)"
    if [ "$LAST_TICK" -gt 0 ] && [ $((t - LAST_TICK)) -gt "$STALL_SEC" ]; then
        STALL_FROM="${STALL_FROM:-$LAST_TICK}"
        STALL_TILL="$t"
    elif [ -n "$STALL_FROM" ]; then
        local span
        span="$(clock "$STALL_FROM" '%H:%M') по $(clock "$STALL_TILL" '%H:%M')"
        spawn_journal "пул стоял $(((STALL_TILL - STALL_FROM) / 60)) мин, с ${span}: Mac спал" \
            "сторож и окна стояли вместе с Mac; окно, оборванное сном, сторож возобновит сам" \
            "ничего, если Mac усыпили нарочно; иначе — не закрывать крышку Mac, пока работает пул"
        say "пул стоял с ${span} — Mac спал, запись в журнале"
        STALL_FROM=""
        STALL_TILL=""
    fi
}
