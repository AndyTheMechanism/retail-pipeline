-- Свежесть источника: партиция за целевую дату приехала и непуста.
--
-- Мерится относительно ЦЕЛЕВОЙ ДАТЫ ПРОГОНА, а не настенных часов. Горизонт
-- данных фиксирован и кончается в прошлом, поэтому проверка "свежо
-- относительно сегодня" была бы красной всегда и справедливо - а выключенный
-- гейт хуже отсутствующего.
--
-- Слабое место называю вслух, потому что оно есть. По умолчанию дата берется
-- из самих данных, и такая проверка не заметит, что не приехал весь хвост
-- сразу: максимум просто окажется вчерашним. Ослабляет это то, что максимум
-- берется по двум источникам разом - если заказы за последний день не пришли,
-- а трафик пришел, дата придет из трафика и проверка сработает. Полную силу
-- она обретет на этапе 4, когда дату будет передавать планировщик и она
-- станет по-настоящему внешней. Пока ее можно задать руками:
--
--     make test VARS='{run_date: 2025-02-26}'
--
-- Тест стоит на источнике, а не на модели, намеренно: свежесть - свойство
-- того, что приехало, и падать она должна раньше, чем что-либо соберется.

with target as (
    select
        {% if var('run_date', none) %}
        date '{{ var("run_date") }}'
        {% else %}
        greatest(
            (select max(order_date) from {{ source('raw', 'orders') }}),
            (select max(traffic_date) from {{ source('raw', 'store_traffic') }})
        )
        {% endif %} as day
),

arrived as (
    select
        t.day,
        (select count(*) from {{ source('raw', 'orders') }} o
          where o.order_date = t.day) as order_rows,
        (select count(*) from {{ source('raw', 'store_traffic') }} s
          where s.traffic_date = t.day) as traffic_rows
    from target t
)

select 'raw.orders' as source_table, day as expected_date, order_rows as rows_found
from arrived
where order_rows = 0

union all

select 'raw.store_traffic', day, traffic_rows
from arrived
where traffic_rows = 0
