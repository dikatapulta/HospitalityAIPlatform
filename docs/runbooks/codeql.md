# Находки CodeQL

> **Роль:** шаги разбора находки CodeQL (#1056 п. 2). Как устроен прогон — шапка
> [`.github/workflows/codeql.yml`](../../.github/workflows/codeql.yml); куда уходит
> уязвимость — правило 9 [PROJECT_EXECUTION_PLAN.md](../../PROJECT_EXECUTION_PLAN.md),
> абзац «Уязвимость».

CodeQL — анализатор кода от GitHub, без модели: ищет известные классы дыр (SQL из ввода,
путь к файлу из ввода, XSS, опасные шаги workflow) в Python, JavaScript и `.github/workflows`.
Разбор идёт после каждого слияния в `main` с кодом и по понедельникам; merge он не держит.

**Где видно.** Список находок — Security → Code scanning, его видят только участники
с правом записи. Пометок в PR нет намеренно: их видит любой читатель публичного
репозитория. Число открытых находок печатает `make queue` — строка «Находки CodeQL».

**Кто разбирает** — окно с человеком: настоящая находка уходит в приватный репозиторий.

1. Список — в своём терминале, не в issue, PR или комментарии:
   ```
   gh api --paginate 'repos/{owner}/{repo}/code-scanning/alerts?state=open' --jq '.[] | "\(.number) \(.rule.id) \(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'
   ```
   Описание правила — `gh api 'repos/{owner}/{repo}/code-scanning/alerts/<N>' --jq .rule.full_description`.
2. Прочитать код по адресу и решить, доходит ли до этого места ввод постороннего.
3. **Ложное срабатывание** — закрыть с причиной одной фразой, без пути эксплуатации:
   ```
   gh api -X PATCH 'repos/{owner}/{repo}/code-scanning/alerts/<N>' -f state=dismissed -f dismissed_reason='false positive' -f dismissed_comment='<почему>'
   ```
   В тестах — `dismissed_reason='used in tests'`.
4. **Настоящая** — issue в приватном репозитории `HospitalityAIPlatform-security`
   (где, как воспроизвести, номер находки CodeQL) и здесь issue-ссылка «Безопасность: sec#N»
   по правилу 9. Находку не закрывать: её снимет сам разбор `main` после merge починки.

**Разбор упал** (красный прогон CodeQL в Actions) — алерт ERR-OPS-013 его не ловит, он
только про `CI` и `Smoke`. Лог прогона публичен, как всё в Actions: шагов, печатающих
результаты разбора в лог или в artifact, в workflow не добавлять. Повтор —
`gh run rerun <номер прогона> --failed`; падает снова — issue с логом шага.
