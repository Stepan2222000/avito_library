"""Разбор страниц Авито: состояние страницы -> данные.

Здесь нет сети, прокси и барьеров — только чистые функции, поэтому всё тестируется
на сохранённых образцах из research/samples без единого запроса.

Что где лежит и с какой заполненностью — в docs/avito-data.md.

Правила разбора, о которых легко забыть:

  * обязательные поля (идентификатор, заголовок, ссылка, время) обязаны быть: объявление
    без них не возвращается, а уходит в брак с причиной;
  * поля, которых у Авито штатно не бывает (продавец, рейтинг, метро, адрес), приходят
    как None или пустой список и браком не считаются;
  * если брака больше пяти процентов страницы, разбор поднимает ParseError: одно кривое
    объявление — это Авито, каждое двадцатое — это мы сломались;
  * рекламные врезки без идентификатора и ссылки — не брак, они просто выбрасываются.
"""
from __future__ import annotations

import datetime as dt
import html as _html
import json
import re

from avito.errors import ОшибкаРазбора

__all__ = ["ParseError", "catalog_state", "hydration", "parse_catalog", "parse_item",
           "parse_date", "clean_text"]

BASE = "https://www.avito.ru"
ДОЛЯ_БРАКА = 0.05

МЕСЯЦЫ = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
          "декабря": 12}


class ParseError(ОшибкаРазбора):
    """Страница не разобралась целиком: либо это не данные, либо структура поехала."""


# --------------------------------------------------------------- достаём состояние

def catalog_state(page: str) -> dict:
    """Состояние каталога, выдачи поиска или витрины продавца."""
    m = re.search(r'<script type="mime/invalid" data-mfe-state="true">(.*?)</script>',
                  page, re.S)
    if not m:
        raise ParseError("состояния каталога нет — это не выдача, а страница блокировки")
    raw = m.group(1)
    if "&quot;" in raw[:200]:
        raw = _html.unescape(raw)
    return json.loads(raw)


def hydration(page: str) -> dict:
    """Состояние карточки: строка внутри JSON.parse, экранированная дважды."""
    i = page.find("window.__staticRouterHydrationData")
    if i < 0:
        raise ParseError("состояния карточки нет — это не объявление, а заглушка")
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
        raise ParseError(f"состояние карточки не распаковалось: {e}") from e


# ------------------------------------------------------------------- мелкие помощники

def clean_text(s: str | None) -> str | None:
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


def parse_date(s: str | None, ref: dt.datetime) -> dt.datetime | None:
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

ОБЯЗАТЕЛЬНЫЕ_В_КАТАЛОГЕ = ("item_id", "title", "url", "published_ts", "published_at")


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
    ид_продавца, бренд = _продавец_из_ссылки(профиль.get("link"))
    return {
        "item_id": str(it["id"]) if it.get("id") else None,
        "title": it.get("title") or None,
        "url": BASE + ссылка.split("?")[0] if ссылка else None,
        "price": _число(цена.get("value")),
        "snippet": clean_text(_iva(it, "DescriptionStep").get("description")),
        # сырая метка как пришла — единственное абсолютное время, какое даёт Авито
        "published_ts": ts,
        # она же разобранная; пояс указываем явно, иначе fromtimestamp возьмёт часовой
        # пояс машины и на сервере в UTC все даты молча уехали бы на часы
        "published_at": dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc) if ts else None,
        "city": (it.get("location") or {}).get("name") or None,
        "location_id": it.get("locationId"),
        # полный адрес лежит в coords.address_user (84%), а не в geo.formattedAddress (40%)
        "address": (коорд.get("address_user") or "").strip() or None,
        "area": (гео.get("formattedAddress") or "").strip() or None,
        "metro": метро,
        "coords": _координаты(коорд),
        "seller_name": профиль.get("title") or None,
        # у частника в ссылке лежит настоящий ключ продавца, тот же, что в карточке;
        # у магазина только слаг бренда, и как sellerId он не работает — проверено
        "seller_id": ид_продавца,
        "seller_brand": бренд,
        "seller_url": BASE + профиль["link"].split("?")[0] if профиль.get("link") else None,
        "seller_rating": рейтинг.get("score"),
        "seller_reviews": _число(рейтинг.get("summary")),
        "images": [u for u in (_макс_фото(im) for im in it.get("images") or []) if u],
        "category": (it.get("category") or {}).get("slug") or None,
        "category_id": it.get("categoryId"),
        "micro_category_id": it.get("microCategoryId"),
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


def parse_catalog(источник: str | dict) -> dict:
    """Страница каталога -> счётчики и список объявлений.

    Возвращает: count (сколько всего нашлось), items_on_page, last_page (докуда пускают),
    items (разобранные), rejected (брак с причиной), promo (сколько врезок выброшено).
    """
    st = catalog_state(источник) if isinstance(источник, str) else источник
    try:
        d = st["loaderData"]["data"]
        сырые = d["catalog"]["items"]
    except (KeyError, TypeError) as e:
        raise ParseError(f"структура каталога поехала: нет {e}") from e

    последняя = re.search(r"[?&]p=(\d+)", (d["catalog"].get("pager") or {}).get("last") or "")
    items, брак, промо = [], [], 0

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
        items.append(об)

    всего = len(items) + len(брак)
    if всего and len(брак) / всего > ДОЛЯ_БРАКА:
        причины = ", ".join(sorted({b["причина"] for b in брак})[:3])
        raise ParseError(f"брак в {len(брак)} из {всего} объявлений — так не бывает; {причины}")

    return {"count": d.get("count"),
            "items_on_page": d.get("itemsOnPage"),
            "last_page": int(последняя.group(1)) if последняя else 1,
            "items": items, "rejected": брак, "promo": промо}


# ------------------------------------------------------------------------- карточка

ОБЯЗАТЕЛЬНЫЕ_В_КАРТОЧКЕ = ("item_id", "title")


def parse_item(источник: str | dict, ref: dt.datetime | None = None) -> dict:
    """Страница объявления -> данные. ref — момент снятия страницы, для разбора даты."""
    st = hydration(источник) if isinstance(источник, str) else источник
    try:
        bi = st["loaderData"]["catalog-or-main-or-item"]["buyerItem"]
        it = bi["item"]
    except (KeyError, TypeError) as e:
        raise ParseError(f"структура карточки поехала: нет {e}") from e

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
        "item_id": str(it.get("id")) if it.get("id") else None,
        "title": it.get("title") or None,
        "price": _число(it.get("price")),
        "description": clean_text(it.get("description")),
        "published_at": parse_date(it.get("sortFormatedDate"), ref),
        "views_total": просмотры.get("totalViews"),
        "views_today": просмотры.get("todayViews"),
        "city": (it.get("location") or {}).get("name") or None,
        "city_slug": (it.get("location") or {}).get("slug") or None,
        "address": it.get("address") or None,
        "coords": _координаты(гео.get("coords")),
        "metro": метро,
        "characteristics": характеристики,
        "images": [u for u in (_макс_фото(m.get("urls")) for m in медиа) if u],
        "is_active": bi.get("isItemActiveOrUserHasAccess"),
        "closed_status": bi.get("closedItemStatus") or None,
        "canonical": bi.get("seoCanonicalUrl") or None,
        # ключ продавца — именно favoriteSeller.userKey; userHashedId это НЕ продавец,
        # он одинаковый у всех и относится к нашей собственной сессии
        "seller_key": (bi.get("favoriteSeller") or {}).get("userKey") or None,
        # бренд достаём и здесь: по нему карточка склеивается с тем, что видел каталог
        "seller_brand": _продавец_из_ссылки(_ссылка_продавца(bi))[1],
        "seller_name": продавец.get("name") or None,
        "seller_is_company": продавец.get("isCompany"),
        "seller_since": продавец.get("tenureSince") or None,
        "seller_url": _ссылка_продавца(bi),
        "seller_rating": рейтинг.get("scoreFloat"),
        "seller_reviews": _число(рейтинг.get("summary")),
    }

    нет = [k for k in ОБЯЗАТЕЛЬНЫЕ_В_КАРТОЧКЕ if об.get(k) is None]
    if нет:
        raise ParseError("в карточке нет обязательных полей: " + ", ".join(нет))
    return об


def _ссылка_продавца(bi: dict) -> str | None:
    for кандидат in ((bi.get("publicProfile") or {}).get("link"),
                     (bi.get("seller") or {}).get("shopUrl"),
                     (bi.get("favoriteSeller") or {}).get("publicProfileLink")):
        if кандидат:
            путь = str(кандидат).split("?")[0]
            return путь if путь.startswith("http") else BASE + путь
    return None


def seller_catalog_url(seller_key: str, region: str = "all", category: str = "") -> str:
    """Витрина продавца. Без категории в пути список приходит пустым — проверено."""
    хвост = f"/{category}" if category else ""
    return f"{BASE}/{region}{хвост}?sellerId={seller_key}"
