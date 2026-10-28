"""«Наставник» персонала — LLM-часть (spec 0048 §5, ADR-023).

Композиционный слой над модулем `mentor`: ищет статьи кодекса, собирает промпт из
`ai/prompts/mentor_*`, зовёт шлюз с назначением `mentor` (ADR-024) и пишет результат
через `modules/mentor/api.py`. Модель — только через `ai/gateway` (контракт 4).

Вход — `ask.ask`, `quiz.new_quiz_question` / `quiz.check_quiz_answer` (потоки событий
`common.MentorEvent`) и `codex_upload.upload_codex`; все — внутри `tenant_context`.
"""
