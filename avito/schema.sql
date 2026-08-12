-- Схема хранения Авито.
--
-- Устройство в двух словах: у объявления одна строка с самым свежим состоянием
-- (кто принёс данные — каталог или карточка — неважно, важно что позже), рядом
-- журнал осмысленных изменений и отдельная таблица наблюдений, куда пишутся
-- величины, меняющиеся при каждом визите: просмотры, дата поднятия, цена.
--
-- Запрос — это поиск, который мы забирали: путь плюс фильтр. Страницы к нему не
-- относятся как отдельные сущности: сколько бы их ни было, запрос остаётся один, а
-- каждое обращение за страницей записывается рядом. Продолжения с прерванной
-- страницы здесь нет намеренно: обход всегда начинается сначала, а журнал нужен,
-- чтобы потом ответить, каким запросом найдено объявление и насколько полно.
--
-- Имена русские, включая колонки: читать эти таблицы будут люди, а не только код.

-- --------------------------------------------------------------- переименование
-- База пережила англоязычные имена, и в ней уже лежат данные. Переименовываем на
-- месте: alter table rename не копирует ни строки, ни индексы. Блок пропускается,
-- если переименование уже случилось, поэтому схему можно применять сколько угодно.
do $$
begin
    if to_regclass('public.sellers') is not null
       and to_regclass('public.продавцы') is null then
        alter table sellers rename to продавцы;
        alter table продавцы rename column seller_id to номер;
        alter table продавцы rename column seller_key to ключ;
        alter table продавцы rename column brand to бренд;
        alter table продавцы rename column name to имя;
        alter table продавцы rename column is_company to компания;
        alter table продавцы rename column since to на_авито_с;
        alter table продавцы rename column rating to рейтинг;
        alter table продавцы rename column reviews to отзывов;
        alter table продавцы rename column first_seen_at to впервые;
        alter table продавцы rename column last_seen_at to последний_раз;
        alter table продавцы rename constraint sellers_has_identity to продавцы_опознаются;
        alter index if exists sellers_pkey rename to продавцы_pkey;
        alter index if exists sellers_seller_key_key rename to продавцы_ключ_уник;
        alter index if exists sellers_brand_key rename to продавцы_бренд_уник;
    end if;

    if to_regclass('public.items') is not null
       and to_regclass('public.объявления') is null then
        alter table items rename to объявления;
        alter table объявления rename column item_id to номер;
        alter table объявления rename column url to ссылка;
        alter table объявления rename column title to заголовок;
        alter table объявления rename column price to цена;
        alter table объявления rename column snippet to выжимка;
        alter table объявления rename column description to описание;
        alter table объявления rename column category to категория;
        alter table объявления rename column category_id to код_категории;
        alter table объявления rename column micro_category_id to код_подкатегории;
        alter table объявления rename column city to город;
        alter table объявления rename column location_id to код_места;
        alter table объявления rename column address to адрес;
        alter table объявления rename column area to местность;
        alter table объявления rename column lat to широта;
        alter table объявления rename column lng to долгота;
        alter table объявления rename column metro to метро;
        alter table объявления rename column characteristics to характеристики;
        alter table объявления rename column views_total to просмотров_всего;
        alter table объявления rename column views_today to просмотров_сегодня;
        alter table объявления rename column published_ts to поднято_мс;
        alter table объявления rename column published_at to поднято;
        alter table объявления rename column is_active to активно;
        alter table объявления rename column seller_id to продавец;
        alter table объявления rename column first_seen_at to впервые;
        alter table объявления rename column last_seen_at to последний_раз;
        alter table объявления rename column last_catalog_at to видели_в_каталоге;
        alter table объявления rename column last_item_at to видели_карточку;
        alter table объявления rename column is_dead to снято;
        alter table объявления rename column died_at to снято_когда;
        alter table объявления rename column dead_reason to снято_почему;
        alter table объявления rename constraint items_seller_id_fkey to объявления_продавец_fkey;
        alter index if exists items_pkey rename to объявления_pkey;
        alter index if exists items_seller rename to объявления_продавец;
        alter index if exists items_category rename to объявления_категория;
        alter index if exists items_last_seen rename to объявления_последний_раз;
    end if;

    if to_regclass('public.item_images') is not null
       and to_regclass('public.фотографии') is null then
        alter table item_images rename to фотографии;
        alter table фотографии rename column item_id to объявление;
        alter table фотографии rename column url_hash to отпечаток;
        alter table фотографии rename column url to ссылка;
        alter table фотографии rename column max_side to сторона;
        alter table фотографии rename column idx to порядок;
        alter table фотографии rename column source to источник;
        alter table фотографии rename column seen_at to когда;
        alter table фотографии rename constraint item_images_item_id_fkey
                                             to фотографии_объявление_fkey;
        alter index if exists item_images_pkey rename to фотографии_pkey;
    end if;

    if to_regclass('public.item_metrics') is not null
       and to_regclass('public.наблюдения') is null then
        alter table item_metrics rename to наблюдения;
        alter table наблюдения rename column item_id to объявление;
        alter table наблюдения rename column seen_at to когда;
        alter table наблюдения rename column views_total to просмотров_всего;
        alter table наблюдения rename column views_today to просмотров_сегодня;
        alter table наблюдения rename column published_ts to поднято_мс;
        alter table наблюдения rename column price to цена;
        alter table наблюдения rename column source to источник;
        alter table наблюдения rename constraint item_metrics_item_id_fkey
                                              to наблюдения_объявление_fkey;
        alter index if exists item_metrics_pkey rename to наблюдения_pkey;
    end if;

    if to_regclass('public.changes') is not null
       and to_regclass('public.изменения') is null then
        alter table changes rename to изменения;
        alter table изменения rename column change_id to номер;
        alter table изменения rename column item_id to объявление;
        alter table изменения rename column field to поле;
        alter table изменения rename column old_value to было;
        alter table изменения rename column new_value to стало;
        alter table изменения rename column changed_at to когда;
        alter table изменения rename column source to источник;
        alter index if exists changes_pkey rename to изменения_pkey;
        alter index if exists changes_item rename to изменения_объявление;
        alter index if exists changes_field rename to изменения_поле;
    end if;

    if to_regclass('public.slices') is not null
       and to_regclass('public.запросы') is null then
        alter table slices rename to запросы;
        alter table запросы rename column slice_id to номер;
        alter table запросы rename column url to ссылка;
        alter table запросы rename column region to регион;
        alter table запросы rename column category to категория;
        alter table запросы rename column filters to фильтры;
        alter table запросы rename column total_count to нашлось;
        alter table запросы rename column pages_total to страниц;
        alter table запросы rename column updated_at to обновлён;
        alter index if exists slices_pkey rename to запросы_pkey;
        alter index if exists "slices_ключ" rename to "запросы_ключ";
        -- адрес запроса перестал быть опознанием ещё когда появился ключ, а уникальность
        -- на нём осталась и однажды уронила бы страницу на ровном месте: два разных по
        -- смыслу запроса вполне могут привести к одному адресу
        alter table запросы drop constraint if exists slices_url_key;
        alter table запросы drop column if exists last_page;
        alter table запросы drop column if exists status;
    end if;

    if to_regclass('public.slice_items') is not null
       and to_regclass('public.находки') is null then
        alter table slice_items rename to находки;
        alter table находки rename column slice_id to запрос;
        alter table находки rename column item_id to объявление;
        alter table находки rename column page to страница;
        alter table находки rename column first_seen_at to впервые;
        alter table находки rename column last_seen_at to последний_раз;
        alter table находки rename constraint slice_items_slice_id_fkey
                                           to находки_запрос_fkey;
        alter table находки rename constraint slice_items_item_id_fkey
                                           to находки_объявление_fkey;
        alter index if exists slice_items_pkey rename to находки_pkey;
        alter index if exists slice_items_item rename to находки_объявление;
    end if;

    if to_regclass('public.fetches') is not null
       and to_regclass('public.обращения') is null then
        alter table fetches rename to обращения;
        alter table обращения rename column fetch_id to номер;
        alter table обращения rename column slice_id to запрос;
        alter table обращения rename column page to страница;
        alter table обращения rename column fetched_at to когда;
        alter table обращения rename column items_count to объявлений;
        alter table обращения rename column proxy to адрес;
        alter table обращения rename column outcome to исход;
        alter table обращения rename constraint fetches_slice_id_fkey
                                             to обращения_запрос_fkey;
        alter index if exists fetches_pkey rename to обращения_pkey;
        alter index if exists fetches_slice rename to обращения_запрос;
    end if;
end $$;

-- --------------------------------------------------------------------- таблицы

create table if not exists продавцы (
    номер          bigserial primary key,
    -- у частника есть настоящий ключ (он же sellerId в каталоге), у магазина из
    -- каталога виден только слаг бренда, и ключом он НЕ является
    ключ           text unique,
    бренд          text unique,
    имя            text,
    компания       boolean,
    на_авито_с     text,                     -- «июля 2019», как отдаёт Авито
    рейтинг        numeric(2,1),
    отзывов        integer,
    впервые        timestamptz not null default now(),
    последний_раз  timestamptz not null default now(),
    constraint продавцы_опознаются check (ключ is not null or бренд is not null)
);

create table if not exists объявления (
    номер             bigint primary key,
    ссылка            text not null,
    заголовок         text not null,
    цена              bigint,
    выжимка           text,                  -- каталог, обрезано Авито ~250 знаками
    описание          text,                  -- карточка, полностью
    категория         text,
    код_категории     integer,
    код_подкатегории  integer,
    город             text,
    код_места         integer,
    адрес             text,
    местность         text,
    широта            double precision,
    долгота           double precision,
    метро             jsonb,
    характеристики    jsonb,                 -- только из карточки
    просмотров_всего  integer,
    просмотров_сегодня integer,
    поднято_мс        bigint,                -- метка Авито как есть, миллисекунды
    поднято           timestamptz,
    активно           boolean,
    продавец          bigint references продавцы,
    впервые           timestamptz not null default now(),
    последний_раз     timestamptz not null default now(),
    видели_в_каталоге timestamptz,
    видели_карточку   timestamptz,
    снято             boolean not null default false,
    снято_когда       timestamptz,
    снято_почему      text                   -- «снято» по редиректу с /items/{номер}
);

create index if not exists объявления_продавец on объявления (продавец);
create index if not exists объявления_категория on объявления (код_категории, город);
create index if not exists объявления_последний_раз on объявления (последний_раз);

create table if not exists фотографии (
    объявление bigint not null references объявления on delete cascade,
    отпечаток  text not null,                -- md5 ссылки, сами ссылки длинные
    ссылка     text not null,
    сторона    integer,                      -- 472 из каталога, 1280 из карточки
    порядок    integer,
    источник   text not null,                -- 'каталог' | 'карточка'
    когда      timestamptz not null default now(),
    primary key (объявление, отпечаток)
);

-- Одна строка на каждое наблюдение. Сюда идёт всё, что меняется постоянно,
-- чтобы не топить журнал изменений.
create table if not exists наблюдения (
    объявление         bigint not null references объявления on delete cascade,
    когда              timestamptz not null default now(),
    просмотров_всего   integer,
    просмотров_сегодня integer,
    поднято_мс         bigint,
    цена               bigint,
    источник           text not null,
    primary key (объявление, когда)
);

-- Только осмысленные события: цена, заголовок, активность, поднятие, смена продавца.
create table if not exists изменения (
    номер      bigserial primary key,
    объявление bigint not null,
    поле       text not null,
    было       text,
    стало      text,
    когда      timestamptz not null default now(),
    источник   text not null
);

create index if not exists изменения_объявление on изменения (объявление, когда desc);
create index if not exists изменения_поле on изменения (поле, когда desc);

-- Журнал поисков: одна строка на запрос, который мы забирали.
create table if not exists запросы (
    номер     bigserial primary key,
    -- опознание запроса. По адресу опознавать нельзя: в нём сидит код фильтра, а он
    -- меняется — то Авито перепишет ссылку в свой канонический вид, то поменяется
    -- номер значения. Тогда один и тот же по смыслу запрос стал бы для базы новым,
    -- а старые записи повисли бы мусором
    ключ      text not null,
    ссылка    text not null,                 -- последний адрес, по которому ходили
    регион    text,
    категория text,
    фильтры   jsonb,                         -- описание фильтра словами, для человека
    нашлось   bigint,                        -- сколько нашлось в последний раз
    страниц   integer,                       -- докуда пускали листать
    обновлён  timestamptz not null default now()
);

create unique index if not exists запросы_ключ on запросы (ключ);

create table if not exists находки (
    запрос        bigint not null references запросы on delete cascade,
    объявление    bigint not null references объявления on delete cascade,
    страница      integer,
    впервые       timestamptz not null default now(),
    последний_раз timestamptz not null default now(),
    primary key (запрос, объявление)
);

create index if not exists находки_объявление on находки (объявление);

create table if not exists обращения (
    номер       bigserial primary key,
    запрос      bigint references запросы on delete cascade,
    страница    integer,
    когда       timestamptz not null default now(),
    объявлений  integer,
    адрес       text,                        -- с какого прокси ходили
    исход       text                         -- 'ок' | 'таймер' | 'капча' | 'обрыв'
);

create index if not exists обращения_запрос on обращения (запрос, когда desc);

-- Не только имена, но и то, что лежит внутри: источник данных и название изменившегося
-- поля раньше записывались по-английски. Иначе получится схема по-русски с английскими
-- значениями внутри, а это хуже, чем любой из двух языков по отдельности.
update наблюдения set источник = 'каталог'  where источник = 'catalog';
update наблюдения set источник = 'карточка' where источник = 'item';
update фотографии set источник = 'каталог'  where источник = 'catalog';
update фотографии set источник = 'карточка' where источник = 'item';
update изменения  set источник = 'каталог'  where источник = 'catalog';
update изменения  set источник = 'карточка' where источник = 'item';

update изменения set поле = 'цена'       where поле = 'price';
update изменения set поле = 'заголовок'  where поле = 'title';
update изменения set поле = 'активно'    where поле = 'is_active';
update изменения set поле = 'поднято_мс' where поле = 'published_ts';
update изменения set поле = 'продавец'   where поле = 'seller_id';
