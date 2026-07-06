"""Temporary file: intentional type error to verify CI blocks the PR (Task 0002 DoD)."""


def add(a: int, b: int) -> int:
    return a + b


result: str = add(1, 2)
