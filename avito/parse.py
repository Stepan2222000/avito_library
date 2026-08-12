"""Разбор страниц Авито: состояние страницы -> данные.

Здесь нет сети, прокси и барьеров — только чистые функции, поэтому всё тестируется
на сохранённых образцах из research/samples без единого запроса.

Что где лежит и с какой заполненностью — в docs/avito-data.md.

Правила разбора, о которых легко забыть:

  * обязательные поля (идентификатор, заголовок, ссылка, время) обязаны быть: объявление
    без них не возвращается, а уходит в брак с причиной;
  * поля, которых у Авито штатно не бывает (продавец, рейтинг, метро, адрес), приходят
    как None или пустой список и браком не считаются;
  * если брака больше пяти процентов страницы, разбор поднимает ОшибкуРазбора: одно кривое
    объявление — это Авито, каждое двадцатое — это мы сломались;
  * рекламные врезки без идентификатора и ссылки — не брак, они просто выбрасываются.
"""
from __future__ import annotations

import datetime as dt
import html as _html
import json
import re

from avito.errors import ОшибкаРазбора

__all__ = ["состояние_выдачи", "состояние_карточки", "разобрать_выдачу",
           "разобрать_карточку", "разобрать_дату", "очистить_текст",
           "ссылка_на_витрину"]

BASE = "https://www.avito.ru"
ДОЛЯ_БРАКА = 0.05

МЕСЯЦЫ = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
          "декабря": 12}


# --------------------------------------------------------------- достаём состояние

def состояние_выдачи(page: str) -> dict:
    """Состояние каталога, выдачи поиска или витрины продавца."""
    m = re.search(r'<script type="mime/invalid" data-mfe-state="true">(.*?)</script>',
                  page, re.S)
    if not m:
        raise ОшибкаРазбора(
            "состояния каталога нет — это не выдача, а страница блокировки")
    raw = m.group(1)
    if "&quot;" in raw[:200]:
        raw = _html.unescape(raw)
    return json.loads(raw)


def состояние_карточки(page: str) -> dict:
    """Состояние карточки: строка внутри JSON.parse, экранированная дважды."""
    i = page.find("window.__staticRouterHydrationData")
    if i < 0:
        raise ОшибкаРазбора("состояния карточки нет — это не объявление, а заглушка")
    try:
        k = page.index('JSON.parse("', i) + len("JSON.parse(")
        p, out = k + 1, []
        while True:
            c = page[p]
            if c == "\\":
                out.append(page[p:p + 2]); p += 2; continue
            if c == '"':
                break
            out.append(c); p += 1
        return json.loads(json.loads('"' + "".join(out) + '"'))
    except (ValueError, IndexError) as e:
        raise ОшибкаРазбора(f"состояние карточки не распаковалось: {e}") from e


# ------------------------------------------------------------------- мелкие помощники

def очистить_текст(s: str | None) -> str | None:
    """HTML описания -> обычный текст: абзацы и <br> становятся переносами строк."""
    if not s:
        return None
    s = re.sub(r"<br\s*/?>|</p>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() or None


def _число(s) -> int | None:
    """«295 отзывов» -> 295, «1 200 ₽» -> 1200."""
    if isinstance(s, (int, float)):
        return int(s)
    if not s:
        return None
    цифры = re.sub(r"[^\d]", "", str(s).replace("\xa0", ""))
    return int(цифры) if цифры else None


def _макс_фото(urls: dict | None) -> str | None:
    """Из набора размеров берём самый большой."""
    if not urls:
        return None
    try:
        return max(urls.items(), key=lambda kv: int(kv[0].split("x")[0]))[1]
    except (ValueError, AttributeError):
        return None


def _iva(it: dict, шаг: str) -> dict:
    """payload первого блока шага iva; их набор зависит от раздела."""
    блоки = (it.get("iva") or {}).get(шаг) or []
    if not блоки or not isinstance(блоки[0], dict):
        return {}
    return блоки[0].get("payload") or {}


def разобрать_дату(s: str | None, ref: dt.datetime) -> dt.datetime | None:
    """«29 июня в 10:25» -> datetime. ref — момент СНЯТИЯ страницы, не текущий.

    Внимание: строку в карточке Авито рисует в часовом поясе того, кто смотрит, —
    проверено, с нашего выхода в UTC+4 показывается на час позже московского. Поэтому
    результат тут в поясе смотрящего, и если нужна точность, лучше брать время из
    каталога: там абсолютная метка sortTimeStamp.
    """
    if not s:
        return None
    s = s.strip().lower()
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    if s.startswith("сегодня"):
        d = ref.date()
    elif s.startswith("вчера"):
        d = ref.date() - dt.timedelta(days=1)
    else:
        mo = re.match(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?", s)
        if not mo or mo.group(2) not in МЕСЯЦЫ:
            return None
        day, mon = int(mo.group(1)), МЕСЯЦЫ[mo.group(2)]
        if mo.group(3):
            year = int(mo.group(3))
        else:
            year = ref.year
            if dt.date(year, mon, day) > ref.date() + dt.timedelta(days=1):
                year -= 1                     # дата из будущего — значит прошлый год
        try:
            d = dt.date(year, mon, day)
        except ValueError:
            return None
    return dt.datetime(d.year, d.month, d.day, hh, mm)


# ------------------------------------------------------------------------- каталог

ОБЯЗАТЕЛЬНЫЕ_В_КАТАЛОГЕ = ("номер", "заголовок", "ссылка", "поднято_мс", "поднято")


def _объявление_каталога(it: dict) -> dict:
    ui = _iva(it, "UserInfoStep")
    профиль = (ui or {}).get("profile") or {}
    рейтинг = it.get("rating") or {}
    гео = it.get("geo") or {}
    цена = it.get("priceDetailed") or {}
    ts = it.get("sortTimeStamp")

    метро = []
    for r in гео.get("geoReferences") or []:
        метро.append({"станция": r.get("content"),
                      "расстояние": (r.get("after") or "").strip() or None})

    ссылка = it.get("urlPath") or ""
    коорд = it.get("coords") or {}
    ключ_продавца, бренд = _продавец_из_ссылки(профиль.get("link"))
    return {
        "номер": str(it["id"]) if it.get("id") else None,
        "заголовок": it.get("title") or None,
        "ссылка": BASE + ссылка.split("?")[0] if ссылка else None,
        "цена": _число(цена.get("value")),
        "выжимка": очистить_текст(_iva(it, "DescriptionStep").get("description")),
        # сырая метка как пришла — единственное абсолютное время, какое даёт Авито
        "поднято_мс": ts,
        # она же разобранная; пояс указываем явно, иначе fromtimestamp возьмёт часовой
        # пояс машины и на сервере в UTC все даты молча уехали бы на часы
        "поднято": dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc) if ts else None,
        "город": (it.get("location") or {}).get("name") or None,
        "код_места": it.get("locationId"),
        # полный адрес лежит в coords.address_user (84%), а не в geo.formattedAddress (40%)
        "адрес": (коорд.get("address_user") or "").strip() or None,
        "местность": (гео.get("formattedAddress") or "").strip() or None,
        "метро": метро,
        "координаты": _координаты(коорд),
        "продавец_имя": профиль.get("title") or None,
        # у частника в ссылке лежит настоящий ключ продавца, тот же, что в карточке;
        # у магазина только слаг бренда, и как sellerId он не работает — проверено
        "продавец_ключ": ключ_продавца,
        "продавец_бренд": бренд,
        "продавец_ссылка": BASE + профиль["link"].split("?")[0] if профиль.get("link") else None,
        "продавец_рейтинг": рейтинг.get("score"),
        "продавец_отзывов": _число(рейтинг.get("summary")),
        "фотографии": [u for u in (_макс_фото(im) for im in it.get("images") or []) if u],
        "категория": (it.get("category") or {}).get("slug") or None,
        "код_категории": it.get("categoryId"),
        "код_подкатегории": it.get("microCategoryId"),
    }


def _продавец_из_ссылки(link: str | None) -> tuple[str | None, str | None]:
    """Из ссылки на профиль: (ключ продавца, слаг бренда).

    У частника ссылка вида /user/{32 знака}/profile — и это тот же ключ, что лежит в
    карточке в favoriteSeller.userKey, сверено. У магазина ссылка /brands/{слаг}, и слаг
    ключом не является: запрос каталога с ним отдаёт всю выдачу, фильтр игнорируется.
    """
    if not link:
        return None, None
    m = re.search(r"/user/([0-9a-f]{16,})/", link)
    if m:
        return m.group(1), None
    m = re.search(r"/brands/([^/?#]+)", link)
    return (None, m.group(1)) if m else (None, None)


def _координаты(c) -> dict | None:
    if not c:
        return None
    try:
        return {"lat": float(c["lat"]), "lng": float(c["lng"])}
    except (KeyError, TypeError, ValueError):
        return None


def разобрать_выдачу(источник: str | dict) -> dict:
    """Страница каталога -> счётчики и список объявлений.

    Возвращает: нашлось (сколько всего по мнению Авито), на_странице, страниц (докуда
    пускают листать), объявления (разобранные), брак (с причиной), врезок (сколько
    рекламы выброшено).
    """
    st = состояние_выдачи(источник) if isinstance(источник, str) else источник
    try:
        d = st["loaderData"]["data"]
        сырые = d["catalog"]["items"]
    except (KeyError, TypeError) as e:
        raise ОшибкаРазбора(f"структура каталога поехала: нет {e}") from e

    последняя = re.search(r"[?&]p=(\d+)", (d["catalog"].get("pager") or {}).get("last") or "")
    объявления, брак, промо = [], [], 0

    for it in сырые:
        if not it.get("id") and not it.get("urlPath"):
            промо += 1                        # рекламная врезка, это не брак
            continue
        try:
            об = _объявление_каталога(it)
        except Exception as e:                # noqa: BLE001 — причина уходит в брак
            брак.append({"id": it.get("id"), "причина": f"{type(e).__name__}: {e}"})
            continue
        нет = [k for k in ОБЯЗАТЕЛЬНЫЕ_В_КАТАЛОГЕ if об.get(k) is None]
        if нет:
            брак.append({"id": it.get("id"), "причина": "нет обязательных: " + ", ".join(нет)})
            continue
        объявления.append(об)

    всего = len(объявления) + len(брак)
    if всего and len(брак) / всего > ДОЛЯ_БРАКА:
        причины = ", ".join(sorted({b["причина"] for b in брак})[:3])
        raise ОшибкаРазбора(
            f"брак в {len(брак)} из {всего} объявлений — так не бывает; {причины}")

    return {"нашлось": d.get("count"),
            "на_странице": d.get("itemsOnPage"),
            "страниц": int(последняя.group(1)) if последняя else 1,
            "объявления": объявления, "брак": брак, "врезок": промо}


# ------------------------------------------------------------------------- карточка

ОБЯЗАТЕЛЬНЫЕ_В_КАРТОЧКЕ = ("номер", "заголовок")


def разобрать_карточку(источник: str | dict, ref: dt.datetime | None = None) -> dict:
    """Страница объявления -> данные. ref — момент снятия страницы, для разбора даты."""
    st = состояние_карточки(источник) if isinstance(источник, str) else источник
    try:
        bi = st["loaderData"]["catalog-or-main-or-item"]["buyerItem"]
        it = bi["item"]
    except (KeyError, TypeError) as e:
        raise ОшибкаРазбора(f"структура карточки поехала: нет {e}") from e

    ref = ref or dt.datetime.now()
    продавец = bi.get("seller") or {}
    рейтинг = bi.get("rating") or {}
    гео = it.get("geo") or {}
    просмотры = bi.get("viewStat") or {}
    медиа = [m for m in (bi.get("galleryInfo") or {}).get("media") or [] if not m.get("isVideo")]

    характеристики = {}
    for блок in ("paramsBlock", "advancedParamsBlock"):
        for g in (bi.get(блок) or {}).get("items") or []:
            имя = (g.get("title") or "").rstrip(":")
            if имя:
                характеристики[имя] = g.get("description") or g.get("value")

    метро = [{"станция": r.get("content"),
              "расстояние": (r.get("after") or "").strip() or None}
             for r in гео.get("references") or []]

    об = {
        "номер": str(it.get("id")) if it.get("id") else None,
        "заголовок": it.get("title") or None,
        "цена": _число(it.get("price")),
        "описание": очистить_текст(it.get("description")),
        "поднято": разобрать_дату(it.get("sortFormatedDate"), ref),
        "просмотров_всего": просмотры.get("totalViews"),
        "просмотров_сегодня": просмотры.get("todayViews"),
        "город": (it.get("location") or {}).get("name") or None,
        "город_слаг": (it.get("location") or {}).get("slug") or None,
        "адрес": it.get("address") or None,
        "координаты": _координаты(гео.get("coords")),
        "метро": метро,
        "характеристики": характеристики,
        "фотографии": [u for u in (_макс_фото(m.get("urls")) for m in медиа) if u],
        "активно": bi.get("isItemActiveOrUserHasAccess"),
        "закрыто_почему": bi.get("closedItemStatus") or None,
        "постоянная_ссылка": bi.get("seoCanonicalUrl") or None,
        # ключ продавца — именно favoriteSeller.userKey; userHashedId это НЕ продавец,
        # он одинаковый у всех и относится к нашей собственной сессии. Имя ключа общее
        # с каталогом: там он приходит из ссылки на профиль и это то же самое значение
        "продавец_ключ": (bi.get("favoriteSeller") or {}).get("userKey") or None,
        # бренд достаём и здесь: по нему карточка склеивается с тем, что видел каталог
        "продавец_бренд": _продавец_из_ссылки(_ссылка_продавца(bi))[1],
        "продавец_имя": продавец.get("name") or None,
        "продавец_компания": продавец.get("isCompany"),
        "продавец_на_авито_с": продавец.get("tenureSince") or None,
        "продавец_ссылка": _ссылка_продавца(bi),
        "продавец_рейтинг": рейтинг.get("scoreFloat"),
        "продавец_отзывов": _число(рейтинг.get("summary")),
    }

    нет = [k for k in ОБЯЗАТЕЛЬНЫЕ_В_КАРТОЧКЕ if об.get(k) is None]
    if нет:
        raise ОшибкаРазбора("в карточке нет обязательных полей: " + ", ".join(нет))
    return об


def _ссылка_продавца(bi: dict) -> str | None:
    for кандидат in ((bi.get("publicProfile") or {}).get("link"),
                     (bi.get("seller") or {}).get("shopUrl"),
                     (bi.get("favoriteSeller") or {}).get("publicProfileLink")):
        if кандидат:
            путь = str(кандидат).split("?")[0]
            return путь if путь.startswith("http") else BASE + путь
    return None


def ссылка_на_витрину(ключ_продавца: str, регион: str = "all", категория: str = "") -> str:
    """Витрина продавца. Без категории в пути список приходит пустым — проверено."""
    хвост = f"/{категория}" if категория else ""
    return f"{BASE}/{регион}{хвост}?sellerId={ключ_продавца}"
