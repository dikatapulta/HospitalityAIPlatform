"""Заменитель flock(1) для тестов деплоя на macOS — запускается как программа.

`ops/deploy/deploy.sh` держит замок `flock` (issue #286). На сервере и в CI это
настоящий flock из util-linux, на Mac основателя его нет — без заменителя тесты
деплоя здесь не прошли бы до первого шага. Заменитель понимает ровно то, что
зовёт deploy.sh, — `flock -n FD` и `flock -w СЕКУНДЫ FD` — и берёт тот же
flock(2) на унаследованный дескриптор: замок живёт на открытом файле
родительского bash и после выхода программы, как у настоящего.
"""

from __future__ import annotations

import fcntl
import shutil
import sys
import time
from pathlib import Path

_POLL_SECONDS = 0.05


def install_flock(bin_dir: Path) -> None:
    """Кладёт заменитель в bin_dir, если настоящего flock в PATH нет."""
    if shutil.which("flock") is not None:
        return
    wrapper = bin_dir / "flock"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n')
    wrapper.chmod(0o755)


def main(argv: list[str]) -> int:
    wait: float | None = None  # без ключей flock ждёт бесконечно
    args = list(argv)
    if args[:1] == ["-n"]:
        wait, args = 0.0, args[1:]
    elif args[:1] == ["-w"] and len(args) >= 2:
        wait, args = float(args[1]), args[2:]
    if len(args) != 1 or not args[0].isdigit():
        print(f"fake flock: не понимаю аргументы {argv}", file=sys.stderr)
        return 64
    deadline = None if wait is None else time.monotonic() + wait
    while True:
        try:
            fcntl.flock(int(args[0]), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                return 1
            time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
