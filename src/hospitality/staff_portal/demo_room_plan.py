"""Румпланы жилых этажей отеля для демо «Номера» (issue #674).

Переписаны со снимков «Room Plan» из Opera (снимки — вне репозитория): какие
номера есть на этаже, с какой стороны коридора и какого типа. Статусов здесь
нет — статусы на демо-доске выдуманы (`demo_room_board.py`).

Румплан — коридор, вдоль него два ряда по 12 клеток: угловая, десять обычных,
угловая. Строка ряда — клетки по порядку румплана сверху вниз через пробел:
- `1303:STDT` — номер и тип кодом из Opera;
- `+` — та же комната, что слева: люкс на две-три клетки;
- `-` — пустая клетка: на румплане там номера нет.
Пропуски в нумерации (нет 1510, 2006–2007) — как на румплане; этажей 6 и 10–12
среди румпланов нет.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final, Literal

SideKey = Literal["mountains", "city"]

SLOTS_PER_SIDE: Final = 12


@dataclass(frozen=True)
class PlanRoom:
    number: str
    room_type: str  # код типа из Opera: STDT, PRMD, EXEC…
    span: int = 1  # сколько клеток ряда занимает


@dataclass(frozen=True)
class Side:
    key: SideKey
    label: str  # подпись ряда — как на румплане
    cells: tuple[PlanRoom | None, ...]  # None — пустая клетка


# Ряд «Вид на горы» на румплане справа, «Вид на город» — слева.
SIDE_LABELS: Final[dict[SideKey, str]] = {"mountains": "Вид на горы", "city": "Вид на город"}

_PLANS: Final[dict[int, tuple[str, str]]] = {
    5: (
        "- 501:PRMD 502:PRMD 503:PRMT 504:PRMD 505:PRMT 506:PRMT 507:PRMD 508:PRMT 509:PRMD"
        " 510:EXEC +",
        "- 520:PRMT 519:PRMD 518:PRMD 517:PRMD 516:PRMD 515:PRMD 514:PRMD 513:PRMD 512:PRMD"
        " 511:EXEC +",
    ),
    7: (
        "- 701:PRMD 702:PRMD 703:PRMD 704:PRMD 705:PRMD 706:PRMD 707:PRMD 708:PRMD 709:PRMD"
        " 710:PRMD 711:PRMT",
        "- 721:PRMD 720:PRMD 719:PRMD 718:PRMD 717:PRMD 716:PRMD 715:PRMD 714:PRMD 713:PRMD - -",
    ),
    8: (
        "801:PRMT 802:PRMT 803:PRMD 804:PRMT 805:PRMD 806:PRMT 807:PRMT 808:PRMD 809:PRMT"
        " 811:EXEC + 812:PRMT",
        "824:PRMT 823:PRMT 822:PRMD 821:PRMT 820:PRMD 819:PRMT 818:PRMT 817:PRMD 816:PRMT"
        " 815:PRMD 814:PRMT 813:PRMT",
    ),
    9: (
        "901:PRMT 902:PRMT 903:PRMT 904:PRMT 905:PRMD 906:PRMT 907:PRMT 908:PRMD 909:PRMT"
        " 911:EXEC + 912:PRMT",
        "924:PRMT 923:PRMT 922:PRMT 921:PRMT 920:PRMD 919:PRMT 918:PRMT 917:PRMD 916:PRMT"
        " 915:PRMT 914:PRMT 913:PRMT",
    ),
    13: (
        "- 1303:STDT 1304:STDT 1305:STDT 1306:SUPD 1307:STDT 1308:STDT 1309:STDT 1310:STDT"
        " 1311:STDT 1312:STDT 1313:STDT",
        "- 1324:STDT 1323:STDT 1322:STDT 1321:STDT 1320:STDT 1319:STDT 1318:STDT 1317:STDT"
        " 1316:STDT 1315:STDT 1314:STDT",
    ),
    14: (
        "1401:FAMR 1402:PRMT 1403:PRMT 1404:PRMD 1405:PRMT 1406:PRMT 1407:PRMD 1408:PRMD"
        " 1409:PRMD 1410:PRMD 1411:PRMT -",
        "1422:FAMR 1421:PRMD 1420:PRMD 1419:PRMD 1418:PRMD 1417:PRMD 1416:PRMD 1415:PRMD"
        " 1414:PRMD 1413:PRMD 1412:PRMT -",
    ),
    15: (
        "1501:PRMT 1502:PRMD 1503:PRMD 1504:PRMD 1505:PRMD 1506:PRMD 1507:PRMD 1508:PRMD"
        " 1509:PRMD 1511:EXEC + 1512:PRMT",
        "- 1523:PRMD 1522:PRMD 1521:PRMD 1520:PRMD 1519:PRMD 1518:PRMD 1517:PRMD 1516:PRMD"
        " 1515:PRMD 1514:PRMD 1513:PRMT",
    ),
    16: (
        "1601:PRMT 1602:PRMD 1603:PRMD 1604:PRMD 1605:PRMD 1606:PRMD 1607:PRMD 1608:PRMD"
        " 1609:PRMD 1611:EXEC + 1612:PRMT",
        "1624:PRMT 1623:PRMD 1622:PRMD 1621:PRMD 1620:PRMD 1619:PRMD 1618:PRMD 1617:PRMD"
        " 1616:PRMD 1615:PRMD 1614:PRMD 1613:PRMT",
    ),
    17: (
        "1701:PRMT 1702:EXEC + 1703:SUPD 1704:SUPD 1705:SUPD 1706:SUPD 1707:SUPD 1708:SUPD"
        " 1709:EXEC + 1711:PRMT",
        "1721:PRMT 1720:EXEC + 1719:PRMD 1718:PRMD 1717:PRMD 1716:SUPD 1715:SUPD 1714:SUPD"
        " 1713:EXEC + 1712:PRMT",
    ),
    18: (
        "1801:PRMT 1802:PRMD 1803:PRMD 1804:PRMD 1805:PRMD 1806:PRMD 1807:PRMD 1808:PRMD"
        " 1809:PRMD 1810:PRMD 1811:PRMD 1812:PRMT",
        "1824:PRMT 1823:PRMD 1822:PRMD 1821:PRMD 1820:PRMD 1819:PRMD 1818:PRMD 1817:PRMD"
        " 1816:PRMD 1815:PRMD 1814:PRMD 1813:PRMT",
    ),
    19: (
        "1901:STDT 1902:DLXR + 1903:SUPD 1904:SUPD 1905:SUPD 1906:SUPD 1907:SUPD 1908:SUPD"
        " 1909:DLXR + 1910:STDT",
        "1920:STDT 1919:EXEC + 1918:PRMD 1917:STDT 1916:SUPD 1915:STDT 1914:SUPD 1913:SUPD"
        " 1912:DLXR + 1911:STDT",
    ),
    20: (
        "2001:STDT 2002:DLXR + 2003:SUPD 2004:SUPD 2005:SUPD 2008:GRND + + 2009:DLXR + 2010:STDT",
        "2020:STDT 2019:DLXR + 2018:SUPD 2017:SUPD 2016:PRMD 2015:STDT 2014:SUPD 2013:STDT"
        " 2012:DLXR + 2011:STDT",
    ),
    21: (
        "2101:STDT 2102:DLXR + 2103:STDT 2104:STDD 2105:STDT 2106:STDT - 2108:STDT 2109:DLXR +"
        " 2110:STDT",
        "2120:STDT - - 2118:STDT 2117:STDD 2116:STDT 2115:STDT 2114:STDT 2113:STDT 2112:DLXR +"
        " 2111:STDT",
    ),
    22: (
        "2201:STDT 2202:DLXR + 2203:STDD 2204:STDD 2205:STDD 2206:STDD 2207:SUPD 2208:STDD"
        " 2209:DLXR + 2210:STDT",
        "2220:STDT 2219:DLXR + 2218:STDD 2217:STDD 2216:STDD 2215:STDD 2214:STDD 2213:STDD"
        " 2212:DLXR + 2211:STDT",
    ),
    23: (
        "2301:STDD 2302:DLXR + 2303:STDD 2304:STDD 2305:STDD 2306:SUPD 2307:SUPD 2308:SUPD"
        " 2309:DLXR + 2310:STDT",
        "2320:STDT 2319:DLXR + 2318:STDD 2317:STDD 2316:STDD 2315:SUPD 2314:SUPD 2313:SUPD"
        " 2312:DLXR + 2311:STDT",
    ),
    24: (
        "2401:STDT 2402:DLXR + 2403:STDD 2404:STDD 2405:STDD 2406:STDD 2407:STDD 2408:GRND + +"
        " 2409:STDT",
        "2418:STDT 2417:STDT + 2416:STDD 2415:STDD 2414:STDD 2413:STDD 2412:STDD 2411:GRND + +"
        " 2410:STDT",
    ),
    25: (
        "2501:STDT 2502:DLXR + 2503:STDD 2504:STDD 2505:STDD 2506:STDD 2507:STDD 2508:GRND + +"
        " 2509:STDT",
        "2518:STDT 2517:DLXR + 2516:STDD 2515:STDD 2514:STDD 2513:STDD 2512:STDD 2511:GRND + +"
        " 2510:STDT",
    ),
}


def _cells(row: str) -> tuple[PlanRoom | None, ...]:
    cells: list[PlanRoom | None] = []
    for token in row.split():
        if token == "-":
            cells.append(None)
        elif token == "+":
            room = cells[-1]
            if room is None:
                raise ValueError(f"«+» после пустой клетки: {row}")
            cells[-1] = replace(room, span=room.span + 1)
        else:
            number, room_type = token.split(":")
            cells.append(PlanRoom(number, room_type))
    return tuple(cells)


FLOORS: Final = tuple(sorted(_PLANS))
# Этаж → ряды в порядке доски: «Вид на горы» сверху, «Вид на город» под ним.
PLAN: Final[dict[int, tuple[Side, Side]]] = {
    floor: (
        Side("mountains", SIDE_LABELS["mountains"], _cells(mountains)),
        Side("city", SIDE_LABELS["city"], _cells(city)),
    )
    for floor, (mountains, city) in _PLANS.items()
}
