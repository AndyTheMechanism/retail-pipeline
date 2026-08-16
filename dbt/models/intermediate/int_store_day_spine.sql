-- Плотная решетка "магазин на день" - зерно обеих витрин.
--
-- Зачем она нужна. Без спайна витрина - это group by по тому, что есть, и день
-- с непришедшими данными выглядит как отсутствие строки. Тогда "магазин был
-- закрыт", "данные не приехали" и "продаж не было" неразличимы, а различает их
-- mart_store_daily_quality. Со спайном такой день виден строкой, где метрики
-- null, и это разные вещи: ноль означает "посчитали, и вышел ноль", null -
-- "считать не по чему".
--
-- Горизонт берется из наблюдаемых данных, а не константой из config.py.
-- Константа разъехалась бы с генератором молча, и заметили бы это по кривой
-- витрине, а не по ошибке.
--
-- Срез по opened_on обязателен: 12 магазинов из 120 открылись внутри
-- горизонта, и дни до открытия - не пропущенные данные, а честная пустота.
-- Без среза решетка дала бы 65 520 строк вместо 62 690, то есть 2 830
-- выдуманных магазино-дней.

with observed as (
    select min(order_date) as first_day, max(order_date) as last_day
    from {{ ref('stg_orders') }}

    union all

    select min(traffic_date), max(traffic_date)
    from {{ ref('stg_store_traffic') }}
),

bounds as (
    select min(first_day) as first_day, max(last_day) as last_day
    from observed
),

calendar as (
    select generate_series(first_day, last_day, interval '1 day')::date as calendar_date
    from bounds
),

stores as (
    select store_id, opened_on
    from {{ ref('stg_stores') }}
)

select
    s.store_id,
    c.calendar_date
from stores s
join calendar c on c.calendar_date >= s.opened_on
