"""Преодоление барьеров: задача proof-of-work и капча.

Детекторы называют барьер, а пробивается через него этот модуль — ему нужна сеть,
сессия и тот же самый адрес, с которого мы упёрлись. Поэтому он живёт отдельно от
detect: там чистые функции без сети, и такими они должны остаться.

Успехом считается не «решатель доволен», а свежая страница без барьера. Решатель может
получить справку у GeeTest, а Авито её не принять, и разницу видно только повторным
запросом — он всё равно нужен, так что стоит это ничего.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import time

from avito import detect, geetest

BASE = "https://www.avito.ru"
ПОПЫТОК = 2  # больше не имеет смысла: адрес, который не хочет пускать, дешевле сменить


@dataclasses.dataclass(slots=True)
class Исход:
    прошли: bool
    что: str                     # чем кончилось, человеческими словами
    страница: str | None = None  # свежая страница, если прошли
    код: int | None = None
    конечный: str | None = None  # куда в итоге привело: за барьером бывает и редирект
    запросов: int = 0
    секунд: float = 0.0


def вид(страница: str) -> str:
    """Какой перед нами барьер. Таймер — по остатку, своего признака у него нет."""
    if detect.задача(страница):
        return "задача"
    return "капча" if detect.капча(страница) else "таймер"


async def пройти(сессия, ссылка: str, страница: str, *, попыток: int = ПОПЫТОК) -> Исход:
    """Пробиться через барьер и вернуть свежую страницу. Таймер не пробивается."""
    начало, запросов = time.time(), 0

    for попытка in range(1, попыток + 1):
        какой = вид(страница)
        if какой == "таймер":
            return Исход(False, "таймер", запросов=запросов,
                         секунд=time.time() - начало)

        if какой == "задача":
            if not await решить_задачу(сессия):
                return Исход(False, "задача не решается", запросов=запросов,
                             секунд=time.time() - начало)
        else:
            try:
                принято, почему = await geetest.unblock(сессия, page_body=страница)
            except Exception as e:  # noqa: BLE001 — решатель тоже ходит в сеть
                return Исход(False, f"капча сорвалась: {type(e).__name__}",
                             запросов=запросов, секунд=time.time() - начало)
            if not принято:
                return Исход(False, f"капча не решилась: {почему}", запросов=запросов,
                             секунд=time.time() - начало)

        # Единственная настоящая проверка: что теперь отдаёт та же самая ссылка.
        ответ = await сессия.get(BASE + ссылка if ссылка.startswith("/") else ссылка)
        запросов += 1
        страница = ответ.text
        if not detect.барьер(страница):
            return Исход(True, f"{какой}: прошли", страница, ответ.status_code,
                         str(getattr(ответ, "url", "") or ссылка), запросов,
                         time.time() - начало)

    return Исход(False, f"барьер не пробит за {попыток} попытки", запросов=запросов,
                 секунд=time.time() - начало)


# ---------------------------------------------------------------------- задача

def _подобрать(ключ: str, сложность: int) -> int:
    начало, n = "0" * сложность, 0
    while not hashlib.sha256(f"{ключ}:{n}".encode()).hexdigest().startswith(начало):
        n += 1
    return n


async def решить_задачу(сессия) -> bool:
    """Перебор хеша по условию, которое Авито само же и отдаёт. Браузер не нужен.

    Перебор идёт в отдельном потоке: на большой сложности он занимает сотни тысяч
    итераций и иначе застопорил бы всех остальных работников.
    """
    вызов = сессия.cookies.get("pow_challenge")
    if not вызов:
        return False
    try:
        выдача = await сессия.post(f"{BASE}/web/3/firewallPow/get",
                                   json={"challenge": вызов},
                                   headers={"Content-Type": "application/json"})
        подпись = выдача.json()["success"]["result"]["challenge_jwt"]
        полезное = подпись.split(".")[1]
        полезное += "=" * (-len(полезное) % 4)
        условие = json.loads(base64.urlsafe_b64decode(полезное))
        ответ = await asyncio.get_running_loop().run_in_executor(
            None, _подобрать, условие["id"], условие["compl"])
        сдача = await сессия.post(f"{BASE}/web/3/firewallPow/verify",
                                  json={"challenge": подпись, "nonce": ответ},
                                  headers={"Content-Type": "application/json"})
        return сдача.json().get("success", {}).get("result", {}).get("verified") is True
    except Exception:  # noqa: BLE001
        return False
