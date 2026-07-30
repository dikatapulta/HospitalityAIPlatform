"""Кабинет персонала — server-rendered веб-интерфейс (spec 0033, ADR-014).

Композиционный слой (FOUNDATION §5.1 «Web (персонал/админ)»): импортирует
`platform/` и `api.py` доменных модулей, сам не импортируется никем, кроме
composition root (контракт import-linter). Публичный вход — `router.router`.
"""
