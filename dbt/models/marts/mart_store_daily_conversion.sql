-- Конверсия по магазину и дню: чеки на посетителя.
--
-- Числитель - офлайн-заказы без отмененных. Офлайн, потому что дверной счетчик
-- не видит ни сайт, ни приложение, и смешение дало бы конверсию, растущую от
-- роста онлайна. Без отмененных, потому что отмененный заказ не чек, - и по
-- тому же набору заказов считается выручка в соседней витрине: одна витрина -
-- один набор.
--
-- Замер, который стоит знать при чтении этих чисел: синтетика строит трафик
-- от офлайн-заказов вместе с отмененными, поэтому измеренная конверсия
-- систематически ниже заложенной примерно на долю отмен, около 3%. На
-- срабатывание порога "чеки больше трафика на 0,95" это не влияет никак, а
-- вот проверку на диапазон пришлось бы отсчитывать от смещенного числа, а не
-- от заложенного.
--
-- Ноль посетителей при живых чеках - не ноль, а сообщение источника о том, что
-- прибор не считал. Поэтому nullif и null в результате, а не деление на ноль и
-- не бесконечность. Сам флаг качества здесь не выставляется: витрина отдает
-- ингредиенты, а суждение выносит гейт.
--
-- ОКНА ПЕРЕСЧЕТА У ЭТОЙ ВИТРИНЫ НЕТ, и это главное, что стоит про нее знать.
--
-- Витрина продаж пересобирает 28 дней назад, потому что возврат приезжает
-- позже покупки и меняет выручку прошлого. Здесь менять задним числом нечего:
-- ни трафик, ни заказы за прошлый день не переписываются - у них нет второй
-- даты, по которой они могли бы приехать позже. Поэтому прогон трогает ровно
-- целевую дату.
--
-- Отсюда правило, которое дороже самой настройки: окно пересчета - не общая
-- настройка пайплайна, а свойство конкретной витрины. Ставить его везде
-- одинаково значит либо пересчитывать лишнее, либо не досчитывать нужное.

{{ config(
    materialized = 'incremental',
    unique_key = ['store_id', 'traffic_date'],
    incremental_strategy = 'delete+insert'
) }}

{% set run_date = var('run_date', none) %}

{% if is_incremental() and run_date %}
    {% set on_run_date = "= date '" ~ run_date ~ "'" %}
{% else %}
    {% set on_run_date = none %}
{% endif %}

with spine as (
    select store_id, calendar_date
    from {{ ref('int_store_day_spine') }}
    {% if on_run_date %} where calendar_date {{ on_run_date }} {% endif %}
),

traffic as (
    select store_id, traffic_date, visitors
    from {{ ref('stg_store_traffic') }}
    {% if on_run_date %} where traffic_date {{ on_run_date }} {% endif %}
),

offline_orders as (
    select
        store_id,
        order_date,
        count(*) as orders_offline
    from {{ ref('stg_orders') }}
    where is_offline and not is_cancelled
    {% if on_run_date %} and order_date {{ on_run_date }} {% endif %}
    group by store_id, order_date
),

orders_present as (
    select distinct store_id, order_date
    from {{ ref('stg_orders') }}
    {% if on_run_date %} where order_date {{ on_run_date }} {% endif %}
),

joined as (
    select
        sp.store_id,
        sp.calendar_date as traffic_date,

        t.store_id is not null  as has_traffic,
        op.store_id is not null as has_orders,

        t.visitors,
        oo.orders_offline

    from spine sp
    left join traffic t
        on t.store_id = sp.store_id and t.traffic_date = sp.calendar_date
    left join offline_orders oo
        on oo.store_id = sp.store_id and oo.order_date = sp.calendar_date
    left join orders_present op
        on op.store_id = sp.store_id and op.order_date = sp.calendar_date
)

select
    store_id,
    traffic_date,
    has_traffic,
    has_orders,

    visitors,

    case when has_orders then coalesce(orders_offline, 0) end as orders_offline,

    case when has_orders and has_traffic
         then coalesce(orders_offline, 0)::numeric / nullif(visitors, 0)
    end as conversion

from joined
