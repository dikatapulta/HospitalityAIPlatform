"""Узор подложки гостевого чата (#746): контуры отеля и Алматы, как обои Telegram.

Решение основателя 03.11 — «Небо и золото», узор свой (рисунок Telegram — их
собственность): ключ, звонок на стойке, горы, яблоко, тюльпан, домбра, шанырак…
Мотивы — из образца консультанта «Обои гостевого чата». Плитка 400×400 собирается
один раз при импорте и уходит в CSS маской (`--wall-tile`): цвет и заметность линий
задают токены `chat.css`, поэтому одна плитка служит дню и ночи. Раскладка —
детерминированная (свой ГПСЧ с постоянным зерном): страница не меняется от запуска
к запуску, а тест видит ровно то, что увидит гость.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

# Мотив — контур в поле 48×48, только линии.
_MOTIFS: Final = (
    ("k", '<circle cx="13" cy="24" r="7"/><path d="M20 24h20M34 24v5M39 24v4"/>'),
    ("b", '<path d="M10 33a14 14 0 0 1 28 0zM7 37h34M24 19v-3M21 16h6"/>'),
    ("c", '<rect x="9" y="16" width="30" height="22" rx="4"/><path d="M19 16v-4h10v4M9 25h30"/>'),
    ("q", '<path d="M12 22h20v8a8 8 0 0 1-8 8h-4a8 8 0 0 1-8-8zM32 25h3a4 4 0 0 1 0 8h-4"/>'),
    ("m", '<path d="M4 38l12-18 8 10 6-10 14 18z"/><path d="M12 26l4-2 3 2M27 25l3-2 3 2"/>'),
    (
        "a",
        '<path d="M24 16c-4-3-12-2-13 7-1 8 5 16 9 16 2 0 3-1 4-1s2 1 4 1c4 0 10-8 9-16'
        '-1-9-9-10-13-7zM24 16c0-4 2-7 5-8"/>',
    ),
    (
        "t",
        '<path d="M24 41V25M24 34c-4-1-8-4-9-8M17 11l3.5 4 3.5-6 3.5 6 3.5-4v8a7 7 0 0 1-14 0z"/>',
    ),
    (
        "w",
        '<path d="M18 42V17h12v25M15 42h18M20 17l-1-6 2.5 3L24 8l2.5 6 2.5-3-1 6M18'
        ' 25h12M18 33h12"/>',
    ),
    (
        "d",
        '<path d="M12 41c-6-4-4-13 2-15 4-1 7 1 8 4 1 4-1 9-4 11-2 1-4 1-6 0z'
        'M21 28L38 11M35 8l5 5"/>',
    ),
    (
        "y",
        '<path d="M7 37h34M9 37v-9M39 37v-9M7 28c5-8 29-8 34 0M16 23c3-7 13-7 16 0M21 37v-7h6v7"/>',
    ),
    (
        "s",
        '<circle cx="24" cy="24" r="14"/><path d="M13 16c7 4 15 4 22 0M13 32c7-4 15-4 22 0'
        'M16 13c4 7 4 15 0 22M32 13c-4 7-4 15 0 22"/>',
    ),
    (
        "o",
        '<path d="M24 36v-9M24 27c-2-8-12-10-14-3-1 4 3 7 6 5 2-1 1-4-1-4M24 27c2-8 12-10 14-3'
        '1 4-3 7-6 5-2-1-1-4 1-4"/>',
    ),
    ("p", '<path d="M10 26h28a14 10 0 0 1-28 0zM18 37h12M19 21c-2-2 2-3 0-6M27 21c-2-2 2-3 0-6"/>'),
    ("g", '<path d="M4 11l40 8M24 15v7M14 22h20v12a3 3 0 0 1-3 3H17a3 3 0 0 1-3-3zM14 28h20"/>'),
    ("f", '<path d="M24 9v30M11 16.5l26 15M11 31.5l26-15M20 11l4 4 4-4M20 37l4-4 4 4"/>'),
    ("h", '<path d="M24 36s-12-7-12-15a6 6 0 0 1 12-2 6 6 0 0 1 12 2c0 8-12 15-12 15z"/>'),
    ("x", '<path d="M24 10c1 8 4 11 12 12-8 1-11 4-12 12-1-8-4-11-12-12 8-1 11-4 12-12z"/>'),
    (
        "l",
        '<path d="M24 41s-11-11-11-19a11 11 0 0 1 22 0c0 8-11 19-11 19z"/><circle'
        ' cx="24" cy="22" r="4"/>',
    ),
)
_TILE: Final = 400
_GRID: Final = 5
_SEED: Final = 20261103


def _rng(seed: int) -> list[float]:
    """Поток чисел [0, 1) линейного конгруэнтного генератора — хватает на одну плитку."""
    out: list[float] = []
    state = seed
    for _ in range(_GRID * _GRID * 5):
        state = (1103515245 * state + 12345) % 2**31
        out.append(state / 2**31)
    return out


def tile_svg() -> str:
    """Плитка узора: сетка 5×5, в клетке мотив с небольшим поворотом и сдвигом."""
    numbers = iter(_rng(_SEED))
    cell = _TILE // _GRID
    uses = []
    for index in range(_GRID * _GRID):
        row, col = divmod(index, _GRID)
        # Шаг 7 взаимно прост с числом мотивов: соседние клетки не повторяют друг друга.
        key = _MOTIFS[index * 7 % len(_MOTIFS)][0]
        scale = 0.74 + next(numbers) * 0.2
        rotate = round((next(numbers) - 0.5) * 50)
        x = col * cell + (cell - 48 * scale) / 2 + (next(numbers) - 0.5) * 12
        y = row * cell + (cell - 48 * scale) / 2 + (next(numbers) - 0.5) * 12
        next(numbers)
        uses.append(
            f'<use href="#{key}" transform="translate({x:.0f} {y:.0f}) scale({scale:.2f}) '
            f'rotate({rotate} 24 24)"/>'
        )
    defs = "".join(f'<g id="{key}">{body}</g>' for key, body in _MOTIFS)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_TILE} {_TILE}">'
        f"<defs>{defs}</defs>"
        '<g fill="none" stroke="#000" stroke-width="1.9" stroke-linecap="round" '
        f'stroke-linejoin="round">{"".join(uses)}</g></svg>'
    )


def wallpaper_css() -> str:
    """CSS-переменная `--wall-tile` с плиткой — data-URI для `mask-image` в `chat.css`."""
    uri = "data:image/svg+xml," + quote(tile_svg(), safe=" =:/,.-\"'()")
    return f':root {{ --wall-tile: url("{uri.replace(chr(34), chr(39))}"); }}'
