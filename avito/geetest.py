"""Решатель GeeTest v4 (слайдер) для файрвола Авито — чистый HTTP, без браузера.

Авито закрывает страницы капчей GeeTest v4. Ключевой факт: браузер никогда не
передаёт GeeTest данные о мыши — он сообщает только *число*, куда встала деталь.
Поэтому весь обмен сводится к трём HTTP-запросам и одному расчёту по картинкам.

    1. GET  gcaptcha4.geetest.com/load    -> lot_number, две картинки, ypos
    2.      считаем смещение по картинкам                       (~6 мс)
    3. GET  gcaptcha4.geetest.com/verify  -> pass_token          («справка»)
    4. POST avito.ru/web/3/firewallCaptcha/verify + справка      -> разблокировано

Жёсткие ограничения, все измерены (подробности в docs/geetest-solver.md):
  * Шаги 1-3 должны идти с ТОГО ЖЕ IP, с которого пойдёт шаг 4. Справка, добытая
    на другом адресе, отвергается (verified=false).
  * Шаг 4 разблокирует только ту СЕССИЮ (банку кук), которая его отправила.
    Остальные сессии на том же IP остаются в блоке.
  * Справка одноразовая и живёт не меньше ~6 минут.
  * HTTP 403 — таймерный блок, капчей не снимается. Распознать и ждать/сменить прокси.

Зависимости: httpx, opencv-python, numpy, pycryptodome.
"""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import random
import re
import string
import time
import uuid

import cv2
import numpy as np
try:                                     # pip-овский pycryptodome
    from Crypto.Cipher import AES, PKCS1_v1_5
    from Crypto.PublicKey import RSA
except ImportError:                      # системный пакет Debian/Ubuntu
    from Cryptodome.Cipher import AES, PKCS1_v1_5
    from Cryptodome.PublicKey import RSA

__all__ = [
    "AVITO_CAPTCHA_ID", "BLOCK_CAPTCHA", "BLOCK_TIMER", "NO_BLOCK",
    "classify_block", "extract_captcha_id", "find_gap", "solve_geetest",
    "AVITO_VERIFY_URL", "build_avito_verify", "submit_to_avito", "unblock",
]

GEETEST = "https://gcaptcha4.geetest.com"
STATIC = "https://static.geetest.com/"
AVITO_VERIFY_URL = "https://www.avito.ru/web/3/firewallCaptcha/verify"

# Захардкожен в странице блокировки Авито; extract_captcha_id() предпочитает живое значение.
AVITO_CAPTCHA_ID = "2d9c743cf7d63dbc9db578a608196bcd"

# Публичный RSA-ключ GeeTest. Свой AES-ключ мы придумываем на каждый запрос и
# запечатываем этим — GeeTest вскроет его приватным. Их собственная генерация
# ключа нам не нужна вообще.
_RSA = RSA.construct((int(
    "00C1E3934D1614465B33053E7F48EE4EC87B14B95EF88947713D25EECBFF7E74C7977D02DC1D9451F79DD"
    "5D1C10C29ACB6A9B4D6FB7D0A0279B6719E1772565F09AF627715919221AEF91899CAE08C0D686D748B20"
    "A3603BE2318CA6BC2B59706592A9219D0BF05C9F65023A21D2330807252AE0066D59CEEFA5F2748EA80BAB81",
    16), 0x10001))
_AES_IV = b"0000000000000000"

BLOCK_CAPTCHA = "captcha"   # HTTP 429 — решаемо
BLOCK_TIMER = "timer"       # HTTP 403 — не решаемо, ждать или менять прокси
NO_BLOCK = "none"

# Различает страницы наличие формы, а не текст советов: "Перезагрузить роутер"
# есть на обеих и как маркер бесполезен (проверено на 11 сохранённых страницах).
_CAPTCHA_MARKERS = ("js-firewall-form", "firewallCaptcha", "initGeetest4")
_TIMER_MARKERS = ("подождите немного", "самолёт")


def classify_block(status_code: int, body: str) -> str:
    """Какая перед нами стена. Решают маркеры в теле: код ответа сам по себе врёт,
    и /firewallCaptcha/get тоже врёт — он отвечает "geeTest" даже на таймерной."""
    if status_code not in (403, 429):
        return NO_BLOCK
    if any(m in body for m in _CAPTCHA_MARKERS):
        return BLOCK_CAPTCHA
    if any(m in body for m in _TIMER_MARKERS):
        return BLOCK_TIMER
    return BLOCK_CAPTCHA if status_code == 429 else BLOCK_TIMER


def extract_captcha_id(body: str) -> str:
    """Достаёт captchaId из страницы блокировки, иначе отдаёт известную константу."""
    m = re.search(r"captchaId\s*[:=]\s*['\"]([0-9a-f]{32})['\"]", body)
    return m.group(1) if m else AVITO_CAPTCHA_ID


# ------------------------------------------------------------------ расчёт по CV

def find_gap(bg_bytes: bytes, slice_bytes: bytes, ypos: float | None = None) -> dict:
    """Находит вырез и возвращает, насколько надо сдвинуть деталь.

    У картинки детали есть альфа-канал, поэтому её точный силуэт достаётся даром —
    одинаково работает для пазла, треугольника, сердца и шестиугольника. Вырез —
    единственное место, где силуэт ложится на пиксели одновременно ТЁМНЫЕ и
    ОДНОТОННЫЕ. Именно однотонность отличает настоящий вырез от тёмной листвы и теней.

    margin — отрыв от лучшего неперекрывающегося конкурента. Низкий margin означает,
    что картинка спорная и ответу можно верить меньше.
    """
    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    piece = cv2.imdecode(np.frombuffer(slice_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if piece is None or bg is None:
        raise ValueError("не удалось декодировать картинки капчи")
    if piece.ndim < 3 or piece.shape[2] < 4:
        raise ValueError("у картинки детали нет альфа-канала")

    alpha = piece[:, :, 3]
    ys, xs = np.where(alpha > 128)
    if not len(xs):
        raise ValueError("силуэт детали пустой")
    x0, y0 = int(xs.min()), int(ys.min())
    h, w = int(ys.max()) - y0 + 1, int(xs.max()) - x0 + 1

    shape = alpha[y0:y0 + h, x0:x0 + w] > 128
    core = cv2.erode(shape.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    if core.sum() < 20:                      # силуэт крошечный: обойдёмся без эрозии
        core = shape
    core_f, outer_f = core.astype(np.float32), (~shape).astype(np.float32)
    n_core, n_outer = float(core_f.sum()), float(outer_f.sum())

    # Суммы по окну через корреляцию — без питоновского цикла по позициям (~150x быстрее).
    gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sum_in = cv2.matchTemplate(gray, core_f, cv2.TM_CCORR)
    sum_in_sq = cv2.matchTemplate(gray * gray, core_f, cv2.TM_CCORR)
    sum_out = cv2.matchTemplate(gray, outer_f, cv2.TM_CCORR)

    mean_in = sum_in / n_core
    std_in = np.sqrt(np.maximum(sum_in_sq / n_core - mean_in * mean_in, 0.0))
    score = (sum_out / n_outer - mean_in) - 0.35 * std_in   # внутри темно и однотонно

    if ypos is not None:
        # Верх выреза равен ровно ypos + y0, поэтому ищем в одной полосе, а не по
        # всей картинке. Это убирает промахи «не та строка» на малоконтрастных фонах.
        row = int(round(ypos + y0))
        lo, hi = max(0, row - 3), min(score.shape[0], row + 4)
        if hi > lo:
            band = np.full(score.shape, -1e9, np.float32)
            band[lo:hi, :] = score[lo:hi, :]
            score = band

    _, best, _, loc = cv2.minMaxLoc(score)
    best_x = loc[0]
    rivals = score.copy()
    rivals[:, max(0, best_x - w // 2):best_x + w // 2 + 1] = -1e9
    _, runner_up, _, _ = cv2.minMaxLoc(rivals)

    return {"dist": best_x - x0, "margin": float(best - runner_up)}


# --------------------------------------------------------------- половина GeeTest

def _seal(payload: dict) -> str:
    """Шифруем payload одноразовым AES-ключом, сам ключ запечатываем RSA."""
    key = "".join(random.choices(string.digits + string.ascii_lowercase, k=16))
    body = json.dumps(payload, separators=(",", ":"))
    pad = 16 - len(body) % 16
    ciphertext = AES.new(key.encode(), AES.MODE_CBC, _AES_IV).encrypt((body + chr(pad) * pad).encode())
    sealed_key = PKCS1_v1_5.new(_RSA).encrypt(key.encode())
    return binascii.hexlify(ciphertext).decode() + binascii.hexlify(sealed_key).decode()


def _unwrap(text: str):
    """GeeTest отвечает в формате JSONP."""
    return json.loads(text[text.index("(") + 1:text.rindex(")")]) if "(" in text else json.loads(text)


def _callback() -> str:
    return "geetest_" + str(int(time.time() * 1000))


async def solve_geetest(client, captcha_id: str = AVITO_CAPTCHA_ID, attempts: int = 3) -> dict | None:
    """Добывает справку у GeeTest. client — httpx.AsyncClient, привязанный к тому же
    прокси/IP, с которого потом пойдёт сдача. Возвращает seccode либо None."""
    for _ in range(attempts):
        loaded = await client.get(f"{GEETEST}/load", params={
            "captcha_id": captcha_id, "challenge": str(uuid.uuid4()), "client_type": "web",
            "risk_type": "slide", "lang": "rus", "callback": _callback()})
        task = _unwrap(loaded.text)["data"]

        bg, piece = await asyncio.gather(client.get(STATIC + task["bg"]),
                                         client.get(STATIC + task["slice"]))
        gap = find_gap(bg.content, piece.content, ypos=task.get("ypos"))

        pow_detail = task["pow_detail"]
        pow_msg = "|".join([str(pow_detail["version"]), str(pow_detail["bits"]),
                            pow_detail["hashfunc"], pow_detail["datetime"], captcha_id,
                            task["lot_number"], "",
                            "".join(random.choices("0123456789abcdef", k=32))])
        answer = await client.get(f"{GEETEST}/verify", params={
            "captcha_id": captcha_id, "client_type": "web", "lot_number": task["lot_number"],
            "payload": task["payload"], "process_token": task["process_token"],
            "payload_protocol": task["payload_protocol"], "pt": task["pt"],
            "callback": _callback(),
            "w": _seal({
                "setLeft": gap["dist"],
                "passtime": random.randint(700, 1600),
                # Обязательно ЧИСЛО. То же значение строкой сервер отвергает.
                "userresponse": gap["dist"] + 2,
                "device_id": "", "lot_number": task["lot_number"],
                "pow_msg": pow_msg, "pow_sign": hashlib.md5(pow_msg.encode()).hexdigest(),
                "geetest": "captcha", "lang": "rus", "ep": "123", "biht": "1426265548",
                "em": {"ph": 0, "cp": 0, "ek": "11", "wd": 1, "nt": 0, "si": 0, "sc": 0},
            })})

        result = _unwrap(answer.text)
        if result.get("status") == "success" and result.get("data", {}).get("result") == "success":
            seccode = dict(result["data"]["seccode"])
            seccode["_dist"], seccode["_margin"] = gap["dist"], gap["margin"]
            return seccode
    return None


# ----------------------------------------------------------------- половина Авито

def build_avito_verify(seccode: dict) -> tuple[str, dict]:
    """Возвращает (url, тело) для запроса к Авито. Вынесено отдельно, чтобы вызывающий
    мог отправить его тем, кто владеет заблокированной банкой кук: httpx или
    page.request.post() у Playwright — второе нужно, когда сессией владеет браузер.

    Заголовок X-Cube не нужен: Авито принимает запрос и без него, и с мусором в нём."""
    return AVITO_VERIFY_URL, {
        "captcha": "", "hCaptchaResponse": "",
        "captcha_id": seccode["captcha_id"], "lot_number": seccode["lot_number"],
        "pass_token": seccode["pass_token"], "gen_time": seccode["gen_time"],
        "captcha_output": seccode["captcha_output"],
    }


async def submit_to_avito(client, seccode: dict) -> bool:
    """Тратит справку на сессию client. Разблокируется только эта сессия."""
    url, body = build_avito_verify(seccode)
    response = await client.post(url, json=body, headers={
        "Content-Type": "application/json",
        "Referer": "https://www.avito.ru/", "Origin": "https://www.avito.ru"})
    try:
        return response.json()["success"]["result"]["verified"] is True
    except Exception:
        return False


async def unblock(client, page_body: str = "", captcha_id: str | None = None,
                  attempts: int = 3) -> tuple[bool, str]:
    """Решает и сдаёт за один заход на одном и том же клиенте.

    Возвращает (разблокировано, причина). Передай HTML заблокированной страницы в
    page_body, чтобы распознался таймерный блок и взялся живой captcha_id.
    """
    if page_body and classify_block(429, page_body) == BLOCK_TIMER:
        return False, BLOCK_TIMER
    seccode = await solve_geetest(client, captcha_id or extract_captcha_id(page_body), attempts)
    if seccode is None:
        return False, "geetest_failed"
    return (True, "ok") if await submit_to_avito(client, seccode) else (False, "avito_rejected")
