"""Запись разобранных данных в PostgreSQL через asyncpg.

Правила, по которым это устроено:

  * побеждает самое свежее — неважно, пришло оно из каталога или из карточки;
  * но пустотой не затираем: если источник поля не знает, старое значение остаётся
    (каталог не знает описания и характеристик, карточка не знает позиции в выдаче);
  * всё, что меняется при каждом визите — просмотры, дата публикации, цена, — уходит
    в item_metrics отдельной строкой на наблюдение;
  * в changes попадают только осмысленные события: цена, заголовок, активность,
    поднятие объявления, смена продавца;
  * при недоступной базе ничего не буферизуем и падаем — так решено сознательно.

Схема живёт рядом в schema.sql и применяется методом apply_schema().
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import pathlib

import asyncpg

def _дсн(явно: str | None = None) -> str:
    """Строка подключения. В коде её нет намеренно: пароль в репозитории — плохая идея."""
    из = явно or os.environ.get("AVITO_DSN")
    if not из:
        raise RuntimeError(
            "не задана строка подключения к базе. Укажите переменную окружения "
            "AVITO_DSN, например: postgresql://пользователь:пароль@хост:5424/avito_data")
    return из

# поля, изменение которых пишется в журнал
ОТСЛЕЖИВАЕМЫЕ = ("price", "title", "is_active", "published_ts", "seller_id")


def _хеш(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class Store:
    def __init__(self, dsn: str | None = None, *, min_size: int = 2, max_size: int = 10):
        self._dsn, self._min, self._max = _дсн(dsn), min_size, max_size
        self._pool: asyncpg.Pool | None = None

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=self._min, max_size=self._max,
                command_timeout=60, statement_cache_size=0)
        return self._pool

    async def close(self, *, ждать: float = 10.0):
        """Закрыть пул. Вежливое прощание ограничено по времени: база бывает далеко,
        и ждать его бесконечно — значит подвесить прогон уже после того, как всё записано."""
        if self._pool is None:
            return
        try:
            await asyncio.wait_for(self._pool.close(), ждать)
        except (TimeoutError, asyncio.TimeoutError):
            self._pool.terminate()
        finally:
            self._pool = None

    async def apply_schema(self):
        sql = (pathlib.Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        async with (await self.pool()).acquire() as c:
            await c.execute(sql)

    # ------------------------------------------------------------------ каталог

    async def apply_catalog(self, разобранное: dict, *, slice_key: str,
                            slice_url: str | None = None, page: int = 1,
                            region: str | None = None, category: str | None = None,
                            filters: str | None = None, proxy: str | None = None) -> dict:
        """Записывает страницу выдачи: объявления, продавцов, фото, членство, курсор.

        slice_key — устойчивое имя среза, по нему он и опознаётся между прогонами.
        slice_url — его нынешний адрес: он меняется вместе с кодом фильтра.
        """
        items = разобранное["items"]
        async with (await self.pool()).acquire() as c, c.transaction():
            slice_id = await c.fetchval(
                """
                insert into slices (ключ, url, region, category, filters, total_count,
                                    pages_total, updated_at)
                values ($1, $2, $3, $4, $5, $6, $7, now())
                on conflict (ключ) do update set
                    url         = coalesce(excluded.url, slices.url),
                    filters     = coalesce(excluded.filters, slices.filters),
                    total_count = excluded.total_count,
                    pages_total = excluded.pages_total,
                    updated_at  = now()
                returning slice_id
                """,
                slice_key, slice_url or slice_key, region, category,
                json.dumps({"описание": filters}, ensure_ascii=False) if filters else None,
                разобранное.get("count"), разобранное.get("last_page"))

            записано = await self._записать_объявления(c, items, источник="catalog")

            await c.execute(
                """
                insert into slice_items (slice_id, item_id, page)
                select $1, x.item_id, $2
                from jsonb_to_recordset($3::jsonb) as x(item_id bigint)
                on conflict (slice_id, item_id) do update set
                    page = excluded.page, last_seen_at = now()
                """, slice_id, page,
                json.dumps([{"item_id": int(i["item_id"])} for i in items]))

            await c.execute(
                """
                insert into fetches (slice_id, page, items_count, proxy, outcome)
                values ($1, $2, $3, $4, 'ок')
                """, slice_id, page, len(items), proxy)

        записано["slice_id"] = slice_id
        return записано

    # ------------------------------------------------------------------ карточка

    async def apply_item(self, карточка: dict, *, proxy: str | None = None) -> dict:
        """Записывает карточку: она приносит описание, характеристики и просмотры."""
        async with (await self.pool()).acquire() as c, c.transaction():
            return await self._записать_объявления(c, [карточка], источник="item")

    async def mark_removed(self, item_id: int | str, *, reason: str = "removed") -> bool:
        """Объявление снято — узнаём по редиректу с /items/{id}, это точный сигнал."""
        async with (await self.pool()).acquire() as c:
            строка = await c.fetchrow(
                """
                update items set is_dead = true, died_at = coalesce(died_at, now()),
                                 dead_reason = $2, last_seen_at = now()
                where item_id = $1 and not is_dead
                returning item_id
                """, int(item_id), reason)
        return строка is not None

    # ------------------------------------------------------------------ внутреннее

    async def _записать_объявления(self, c: asyncpg.Connection, items: list[dict],
                                   *, источник: str) -> dict:
        if not items:
            return {"объявлений": 0, "изменений": 0}

        продавцы = await self._записать_продавцов(c, items)
        payload = []
        for об in items:
            ключ = об.get("seller_key") or об.get("seller_id")
            бренд = об.get("seller_brand")
            payload.append({
                "item_id": int(об["item_id"]),
                "url": об.get("url") or об.get("canonical"),
                "title": об.get("title"),
                "price": об.get("price"),
                "snippet": об.get("snippet"),
                "description": об.get("description"),
                "category": об.get("category"),
                "category_id": об.get("category_id"),
                "micro_category_id": об.get("micro_category_id"),
                "city": об.get("city"),
                "location_id": об.get("location_id"),
                "address": об.get("address"),
                "area": об.get("area"),
                "lat": (об.get("coords") or {}).get("lat"),
                "lng": (об.get("coords") or {}).get("lng"),
                # кладём как есть: весь payload сериализуется один раз ниже,
                # иначе jsonb получит строку вместо объекта
                "metro": об.get("metro"),
                "characteristics": об.get("characteristics"),
                "views_total": об.get("views_total"),
                "views_today": об.get("views_today"),
                "published_ts": об.get("published_ts"),
                "published_at": об.get("published_at"),
                "is_active": об.get("is_active"),
                "seller_id": продавцы.get(ключ or бренд),
            })

        строки = json.dumps(payload, ensure_ascii=False, default=str)
        изменений = await c.fetchval(_SQL_ОБЪЯВЛЕНИЯ, строки, источник)
        await self._записать_фото(c, items, источник)
        return {"объявлений": len(items), "изменений": изменений or 0}

    async def _записать_продавцов(self, c: asyncpg.Connection, items: list[dict]) -> dict:
        сырые = {}
        for об in items:
            ключ = об.get("seller_key") or об.get("seller_id")
            бренд = об.get("seller_brand")
            if not ключ and not бренд:
                continue
            сырые[ключ or бренд] = (ключ, бренд, об.get("seller_name"),
                                    об.get("seller_is_company"), об.get("seller_since"),
                                    об.get("seller_rating"), об.get("seller_reviews"))
        if not сырые:
            return {}
        payload = [{"опознание": о, "seller_key": к, "brand": б, "name": и,
                    "is_company": комп, "since": ст, "rating": р, "reviews": отз}
                   for о, (к, б, и, комп, ст, р, отз) in сырые.items()]
        строки = await c.fetch(_SQL_ПРОДАВЦЫ, json.dumps(payload, ensure_ascii=False,
                                                         default=str))
        return {r["опознание"]: r["seller_id"] for r in строки}

    async def _записать_фото(self, c: asyncpg.Connection, items: list[dict], источник: str):
        строки = []
        for об in items:
            for i, url in enumerate(об.get("images") or []):
                сторона = None
                if "/image/" in url:
                    сторона = 1280 if источник == "item" else 472
                строки.append((int(об["item_id"]), _хеш(url), url, сторона, i, источник))
        if not строки:
            return
        payload = [{"item_id": a, "url_hash": b, "url": u, "max_side": m,
                    "idx": i, "source": src} for a, b, u, m, i, src in строки]
        await c.execute(
            """
            insert into item_images (item_id, url_hash, url, max_side, idx, source)
            select * from jsonb_to_recordset($1::jsonb) as x(
                item_id bigint, url_hash text, url text, max_side int, idx int, source text)
            on conflict (item_id, url_hash) do update set
                idx = excluded.idx, seen_at = now()
            """, json.dumps(payload, ensure_ascii=False))


# Одним запросом: upsert объявлений, наблюдения и журнал изменений.
# Порядок важен: CTE «прежние» читает таблицу до записи, поэтому видит старые значения.
_SQL_ОБЪЯВЛЕНИЯ = """
with новые as (
    select * from jsonb_to_recordset($1::jsonb) as x(
        item_id bigint, url text, title text, price bigint, snippet text,
        description text, category text, category_id int, micro_category_id int,
        city text, location_id int, address text, area text,
        lat double precision, lng double precision, metro jsonb, characteristics jsonb,
        views_total int, views_today int, published_ts bigint, published_at timestamptz,
        is_active boolean, seller_id bigint)
),
прежние as (
    select i.item_id, i.price, i.title, i.is_active, i.published_ts, i.seller_id
    from items i join новые n using (item_id)
),
запись as (
    insert into items as i (
        item_id, url, title, price, snippet, description, category, category_id,
        micro_category_id, city, location_id, address, area, lat, lng, metro,
        characteristics, views_total, views_today, published_ts, published_at,
        is_active, seller_id, last_seen_at,
        last_catalog_at, last_item_at)
    select n.item_id, n.url, n.title, n.price, n.snippet, n.description, n.category,
           n.category_id, n.micro_category_id, n.city, n.location_id, n.address, n.area,
           n.lat, n.lng, n.metro, n.characteristics, n.views_total, n.views_today,
           n.published_ts, n.published_at, n.is_active, n.seller_id, now(),
           case when $2 = 'catalog' then now() end,
           case when $2 = 'item'    then now() end
    from новые n
    on conflict (item_id) do update set
        -- свежее побеждает, но пустотой не затираем: источник может не знать поля
        url             = coalesce(excluded.url, i.url),
        title           = coalesce(excluded.title, i.title),
        price           = coalesce(excluded.price, i.price),
        snippet         = coalesce(excluded.snippet, i.snippet),
        description     = coalesce(excluded.description, i.description),
        category        = coalesce(excluded.category, i.category),
        category_id     = coalesce(excluded.category_id, i.category_id),
        micro_category_id = coalesce(excluded.micro_category_id, i.micro_category_id),
        city            = coalesce(excluded.city, i.city),
        location_id     = coalesce(excluded.location_id, i.location_id),
        address         = coalesce(excluded.address, i.address),
        area            = coalesce(excluded.area, i.area),
        lat             = coalesce(excluded.lat, i.lat),
        lng             = coalesce(excluded.lng, i.lng),
        metro           = coalesce(excluded.metro, i.metro),
        characteristics = coalesce(excluded.characteristics, i.characteristics),
        views_total     = coalesce(excluded.views_total, i.views_total),
        views_today     = coalesce(excluded.views_today, i.views_today),
        published_ts    = coalesce(excluded.published_ts, i.published_ts),
        published_at    = coalesce(excluded.published_at, i.published_at),
        is_active       = coalesce(excluded.is_active, i.is_active),
        seller_id       = coalesce(excluded.seller_id, i.seller_id),
        last_seen_at    = now(),
        last_catalog_at = case when $2 = 'catalog' then now() else i.last_catalog_at end,
        last_item_at    = case when $2 = 'item'    then now() else i.last_item_at end,
        -- увидели снова — значит живо
        is_dead = false, died_at = null, dead_reason = null
    returning i.item_id
),
наблюдения as (
    insert into item_metrics (item_id, views_total, views_today, published_ts, price, source)
    select n.item_id, n.views_total, n.views_today, n.published_ts, n.price, $2
    from новые n
    on conflict (item_id, seen_at) do nothing
),
события as (
    insert into changes (item_id, field, old_value, new_value, source)
    select n.item_id, поле, старое, новое, $2
    from новые n
    join прежние p using (item_id)
    cross join lateral (values
        ('price',        p.price::text,        n.price::text),
        ('title',        p.title,              n.title),
        ('is_active',    p.is_active::text,    n.is_active::text),
        ('published_ts', p.published_ts::text, n.published_ts::text),
        ('seller_id',    p.seller_id::text,    n.seller_id::text)
    ) as v(поле, старое, новое)
    -- из «не знали» в «узнали» — это не изменение объявления, а наше знание
    where новое is not null and старое is not null and старое is distinct from новое
    returning 1
)
select count(*)::int from события
"""


# Продавцы одним запросом: находим по ключу или бренду, недостающих вставляем,
# у найденных дозаполняем пустоты. Иначе на удалённой базе это сотня обращений.
_SQL_ПРОДАВЦЫ = """
with новые as (
    select * from jsonb_to_recordset($1::jsonb) as x(
        опознание text, seller_key text, brand text, name text,
        is_company boolean, since text, rating numeric, reviews int)
),
найденные as (
    select distinct on (n.опознание) n.опознание, s.seller_id
    from новые n
    join sellers s on (n.seller_key is not null and s.seller_key = n.seller_key)
                   or (n.brand is not null and s.brand = n.brand)
    order by n.опознание, s.seller_id
),
дополнение as (
    update sellers s set
        seller_key   = coalesce(s.seller_key, n.seller_key),
        brand        = coalesce(s.brand, n.brand),
        name         = coalesce(n.name, s.name),
        is_company   = coalesce(n.is_company, s.is_company),
        since        = coalesce(n.since, s.since),
        rating       = coalesce(n.rating, s.rating),
        reviews      = coalesce(n.reviews, s.reviews),
        last_seen_at = now()
    from найденные f join новые n using (опознание)
    where s.seller_id = f.seller_id
),
вставка as (
    insert into sellers (seller_key, brand, name, is_company, since, rating, reviews)
    select n.seller_key, n.brand, n.name, n.is_company, n.since, n.rating, n.reviews
    from новые n
    where not exists (select 1 from найденные f where f.опознание = n.опознание)
    returning seller_id, seller_key, brand
)
select f.опознание, f.seller_id from найденные f
union all
select n.опознание, v.seller_id
from вставка v join новые n
  on (v.seller_key is not null and v.seller_key = n.seller_key)
  or (v.seller_key is null and v.brand = n.brand)
"""
