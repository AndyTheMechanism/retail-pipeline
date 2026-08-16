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

with spine as (
    select store_id, calendar_date
    from {{ ref('int_store_day_spine') }}
),

orders as (
    select
        store_id,
        order_date,
        count(*) filter (where not is_cancelled) as orders_count,
        count(*) filter (where is_cancelled)     as orders_cancelled_count
    from {{ ref('stg_orders') }}
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
    group by store_id, order_date
),

returns_by_order_date as (
    select
        store_id,
        order_date,
        sum(returned_amount) as returns_amount
    from {{ ref('int_returns_attributed') }}
    group by store_id, order_date
),

returns_by_arrival as (
    select
        store_id,
        returned_date,
        sum(returned_amount) as returns_arrived_amount
    from {{ ref('int_returns_attributed') }}
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
