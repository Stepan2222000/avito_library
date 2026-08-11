-- Схема хранения Авито.
--
-- Устройство в двух словах: у объявления одна строка с самым свежим состоянием
-- (кто принёс данные — каталог или карточка — неважно, важно что позже), рядом
-- журнал осмысленных изменений и отдельная таблица наблюдений, куда пишутся
-- величины, меняющиеся при каждом визите: просмотры, дата публикации, цена.
--
-- Обход хранится курсором по срезам: срез — это набор фильтров, чья выдача
-- помещается под потолок в пять тысяч, и по нему мы помним, до какой страницы
-- дошли. Без этого после смерти адреса пришлось бы перечитывать страницы заново.

create table if not exists sellers (
    seller_id     bigserial primary key,
    -- у частника есть настоящий ключ (работает как sellerId в каталоге),
    -- у магазина из каталога виден только слаг бренда, и ключом он НЕ является
    seller_key    text unique,
    brand         text unique,
    name          text,
    is_company    boolean,
    since         text,                      -- «июля 2019», как отдаёт Авито
    rating        numeric(2,1),
    reviews       integer,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    constraint sellers_has_identity check (seller_key is not null or brand is not null)
);

create table if not exists items (
    item_id          bigint primary key,
    url              text not null,
    title            text not null,
    price            bigint,
    snippet          text,                   -- каталог, обрезан Авито ~250 символами
    description      text,                   -- карточка, полное
    category         text,
    category_id      integer,
    micro_category_id integer,
    city             text,
    location_id      integer,
    address          text,
    area             text,
    lat              double precision,
    lng              double precision,
    metro            jsonb,
    characteristics  jsonb,                  -- только из карточки
    views_total      integer,
    views_today      integer,
    published_ts     bigint,
    published_at     timestamptz,
    is_active        boolean,
    seller_id        bigint references sellers,
    first_seen_at    timestamptz not null default now(),
    last_seen_at     timestamptz not null default now(),
    last_catalog_at  timestamptz,
    last_item_at     timestamptz,
    is_dead          boolean not null default false,
    died_at          timestamptz,
    dead_reason      text                    -- 'removed' по редиректу с /items/{id}
);

create index if not exists items_seller on items (seller_id);
create index if not exists items_category on items (category_id, city);
create index if not exists items_last_seen on items (last_seen_at);

create table if not exists item_images (
    item_id  bigint not null references items on delete cascade,
    url_hash text not null,                  -- md5 ссылки, сами ссылки длинные
    url      text not null,
    max_side integer,                        -- 472 из каталога, 1280 из карточки
    idx      integer,
    source   text not null,                  -- 'catalog' | 'item'
    seen_at  timestamptz not null default now(),
    primary key (item_id, url_hash)
);

-- Одна строка на каждое наблюдение. Сюда идёт всё, что меняется постоянно,
-- чтобы не топить журнал изменений.
create table if not exists item_metrics (
    item_id      bigint not null references items on delete cascade,
    seen_at      timestamptz not null default now(),
    views_total  integer,
    views_today  integer,
    published_ts bigint,
    price        bigint,
    source       text not null,
    primary key (item_id, seen_at)
);

-- Только осмысленные события: цена, заголовок, активность, поднятие, смена продавца.
create table if not exists changes (
    change_id  bigserial primary key,
    item_id    bigint not null,
    field      text not null,
    old_value  text,
    new_value  text,
    changed_at timestamptz not null default now(),
    source     text not null
);

create index if not exists changes_item on changes (item_id, changed_at desc);
create index if not exists changes_field on changes (field, changed_at desc);

-- Журнал задач на каталог: одна строка на выдачу, которую мы забирали. Курсора и
-- состояния здесь нет намеренно — обход по срезам с продолжением отложен, а держать
-- пустые поля «на будущее» значит однажды поверить в их содержимое.
create table if not exists slices (
    slice_id    bigserial primary key,
    url         text not null unique,        -- последний адрес, по которому ходили
    region      text,
    category    text,
    filters     jsonb,                       -- описание фильтра словами, для человека
    total_count bigint,                      -- сколько нашлось в последний раз
    pages_total integer,                     -- докуда пускали листать
    updated_at  timestamptz not null default now()
);

alter table slices drop column if exists last_page;
alter table slices drop column if exists status;

-- Опознавать срез по адресу нельзя: в адресе сидит код фильтра, а он меняется — то
-- Авито перепишет ссылку в свой канонический вид, то поменяется номер значения. Тогда
-- один и тот же по смыслу срез стал бы для базы новым, курсор обнулился бы, а старые
-- записи повисли мусором. Поэтому ключ — устойчивое описание, а адрес живёт рядом как
-- переменная часть.
alter table slices add column if not exists ключ text;
update slices set ключ = url where ключ is null;
alter table slices alter column ключ set not null;
create unique index if not exists slices_ключ on slices (ключ);

create table if not exists slice_items (
    slice_id      bigint not null references slices on delete cascade,
    item_id       bigint not null references items on delete cascade,
    page          integer,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    primary key (slice_id, item_id)
);

create index if not exists slice_items_item on slice_items (item_id);

create table if not exists fetches (
    fetch_id    bigserial primary key,
    slice_id    bigint references slices on delete cascade,
    page        integer,
    fetched_at  timestamptz not null default now(),
    items_count integer,
    proxy       text,
    outcome     text                         -- 'ок' | '403' | 'капча' | 'обрыв'
);

create index if not exists fetches_slice on fetches (slice_id, fetched_at desc);
