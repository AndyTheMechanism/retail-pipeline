-- Сверка собранного чека с сырьем за период - прямой наследник боевой сверки
-- с эталоном.
--
-- Тест идет к сырью напрямую через source, а не через staging, и повторяет
-- дедупликацию своими руками. Это принципиально: проверка, построенная на тех
-- же моделях, которые она проверяет, повторила бы их ошибку и не заметила ее.
-- Здесь два независимых пути к одному числу, и сходиться они обязаны.
--
-- Стоит на промежуточном слое, а не на витрине, и это тоже решение. Тест в dbt
-- выполняется после того, как его модель собрана, - то есть тест на витрине
-- падает, когда неверная витрина уже лежит в базе. Чтобы битое число до нее не
-- доехало, сверка обязана стоять выше по графу, и тогда упавший тест просто не
-- пускает сборку витрин дальше.
--
-- Период, а не весь горизонт: сверка идет в каждом ежедневном прогоне, и
-- гонять по трем с половиной миллионам строк каждый раз незачем. Размер окна
-- задается переменной reconcile_days.

{% set reconcile_days = var('reconcile_days', 30) %}

with target as (
    select
        {% if var('run_date', none) %}
        date '{{ var("run_date") }}'
        {% else %}
        (select max(order_date) from {{ source('raw', 'orders') }})
        {% endif %} as day
),

period as (
    select
        day - {{ reconcile_days }} as first_day,
        day                        as last_day
    from target
),

raw_lines as (
    select
        i.order_id,
        i.line_no,
        i.order_date,
        i.line_amount,
        row_number() over (
            partition by i.order_id, i.line_no
            order by i.line_amount, i.quantity, i.unit_price, i.sku
        ) as copy_no
    from {{ source('raw', 'order_items') }} i, period p
    where i.order_date between p.first_day and p.last_day
),

raw_expected as (
    select
        o.store_id,
        l.order_date,
        count(*)            as lines_count,
        sum(l.line_amount)  as revenue_gross
    from raw_lines l
    join {{ source('raw', 'orders') }} o on o.order_id = l.order_id
    where l.copy_no = 1
      and o.status <> 'отменен'
    group by o.store_id, l.order_date
),

model_actual as (
    select
        store_id,
        order_date,
        count(*)         as lines_count,
        sum(line_amount) as revenue_gross
    from {{ ref('int_order_lines') }}, period p
    where order_date between p.first_day and p.last_day
      and not is_cancelled
    group by store_id, order_date
)

select
    coalesce(e.store_id, a.store_id)     as store_id,
    coalesce(e.order_date, a.order_date) as order_date,
    e.revenue_gross                      as expected_revenue,
    a.revenue_gross                      as actual_revenue,
    e.lines_count                        as expected_lines,
    a.lines_count                        as actual_lines
from raw_expected e
full outer join model_actual a
    on a.store_id = e.store_id
   and a.order_date = e.order_date
where e.revenue_gross is distinct from a.revenue_gross
   or e.lines_count is distinct from a.lines_count
