-- Продажи по магазину и дню.
--
-- Зерно задает спайн, а не данные: строка есть на каждый день работы каждого
-- магазина, даже если в этот день из источника не приехало ничего. Пустота при
-- этом остается пустотой - coalesce до нуля стоит только там, где данные
-- приехали. Ноль означает "посчитали, и вышел ноль", null - "считать не по
-- чему", и подменять второе первым нельзя: это ровно та ошибка, из-за которой
-- витрина обновляется молча и неправильно.
--
-- Главное решение витрины: возврат относится к дате заказа, а не к дате
-- приезда. Иначе вчерашнее число не менялось бы никогда - а весь проект про
-- то, что оно меняется и что это видно. На этом стоит окно пересчета этапа 4 и
-- журнал ревизий этапа 5; атрибуция по дате возврата обнулила бы оба.
--
-- Рядом стоит returns_arrived_amount по второй оси - возвраты, приехавшие в
-- этот день к заказам любых дат. Она не участвует в выручке и нужна ровно для
-- одного: показать, откуда взялось изменение прошлого числа.
--
-- Заказы считаются по шапкам, а не по позициям: заказ без позиций все равно
-- заказ. Отмененные не выбрасываются, а выносятся отдельной колонкой - день,
-- где половина заказов отменилась, обязан отличаться от дня, когда их просто
-- не было.
--
-- ПРО ОКНО ПЕРЕСЧЕТА.
--
-- Витрина инкрементальная, и стратегия записи та же, что у загрузки сырья, -
-- delete-and-insert по ключу. Симметрия не случайная: повторный прогон за ту
-- же дату обязан давать то же состояние и там, и тут, а не задвоение.
--
-- Ежедневный прогон пересобирает не один день, а окно назад, потому что
-- возврат приезжает позже своей покупки и меняет выручку прошлого. Размер окна
-- задан переменной return_window_days в dbt_project.yml, там же обоснование
-- числом. Считать всю историю каждый день дорого, считать только сегодня -
-- неверно; окно - это цена, выбранная осознанно, а не компромисс по невнимению.
--
-- Без run_date фильтра нет и витрина собирается целиком. Оба пути дают
-- одинаковый результат - это и проверяется контрольными суммами.

{{ config(
    materialized = 'incremental',
    unique_key = ['store_id', 'order_date'],
    incremental_strategy = 'delete+insert'
) }}

{% set run_date = var('run_date', none) %}
{% set window_days = var('return_window_days') %}

{% if is_incremental() and run_date %}
    {% set window_start = "date '" ~ run_date ~ "' - " ~ window_days %}
    {% set window_end = "date '" ~ run_date ~ "'" %}
    {% set in_window = "between " ~ window_start ~ " and " ~ window_end %}
{% else %}
    {% set in_window = none %}
{% endif %}

with spine as (
    select store_id, calendar_date
    from {{ ref('int_store_day_spine') }}
    {% if in_window %} where calendar_date {{ in_window }} {% endif %}
),

orders as (
    select
        store_id,
        order_date,
        count(*) filter (where not is_cancelled) as orders_count,
        count(*) filter (where is_cancelled)     as orders_cancelled_count
    from {{ ref('stg_orders') }}
    {% if in_window %} where order_date {{ in_window }} {% endif %}
    group by store_id, order_date
),

lines as (
    select
        store_id,
        order_date,
        count(*)         as lines_count,
        sum(quantity)    as units_sold,
        sum(line_amount) as revenue_gross
    from {{ ref('int_order_lines') }}
    where not is_cancelled
    {% if in_window %} and order_date {{ in_window }} {% endif %}
    group by store_id, order_date
),

returns_by_order_date as (
    select
        store_id,
        order_date,
        sum(returned_amount) as returns_amount
    from {{ ref('int_returns_attributed') }}
    -- Возврат относится к дате заказа, поэтому и окно режется по ней: дата
    -- приезда у этих возвратов может быть любой, в том числе сегодняшней.
    {% if in_window %} where order_date {{ in_window }} {% endif %}
    group by store_id, order_date
),

returns_by_arrival as (
    select
        store_id,
        returned_date,
        sum(returned_amount) as returns_arrived_amount
    from {{ ref('int_returns_attributed') }}
    -- А эта колонка живет по второй оси, и окно ей режется по дате приезда.
    {% if in_window %} where returned_date {{ in_window }} {% endif %}
    group by store_id, returned_date
),

joined as (
    select
        sp.store_id,
        sp.calendar_date as order_date,

        -- Признак того, что заказы за этот день вообще приехали. Для открытого
        -- магазина это норма; ложь по всей сети в один день означает
        -- непришедшую партицию, и ловить ее будет гейт этапа 3.
        o.store_id is not null as has_orders,

        o.orders_count,
        o.orders_cancelled_count,
        l.lines_count,
        l.units_sold,
        l.revenue_gross,
        r.returns_amount,
        ra.returns_arrived_amount

    from spine sp
    left join orders o
        on o.store_id = sp.store_id and o.order_date = sp.calendar_date
    left join lines l
        on l.store_id = sp.store_id and l.order_date = sp.calendar_date
    left join returns_by_order_date r
        on r.store_id = sp.store_id and r.order_date = sp.calendar_date
    left join returns_by_arrival ra
        on ra.store_id = sp.store_id and ra.returned_date = sp.calendar_date
)

select
    store_id,
    order_date,
    has_orders,

    orders_count,
    orders_cancelled_count,

    case when has_orders then coalesce(lines_count, 0)    end as lines_count,
    case when has_orders then coalesce(units_sold, 0)     end as units_sold,
    case when has_orders then coalesce(revenue_gross, 0)  end as revenue_gross,
    case when has_orders then coalesce(returns_amount, 0) end as returns_amount,

    -- Эта колонка живет по своей оси: партиция возвратов за день приезжает
    -- независимо от того, приехали ли заказы. Пустая партиция означает, что
    -- возвратов в этот день не было, - то есть ноль, а не неизвестность.
    coalesce(returns_arrived_amount, 0) as returns_arrived_amount,

    case when has_orders
         then coalesce(revenue_gross, 0) - coalesce(returns_amount, 0)
    end as revenue_net

from joined
