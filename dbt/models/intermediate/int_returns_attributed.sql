-- Возврат, привязанный к магазину через свой заказ.
--
-- Модель существует не для красоты слоев: в raw.returns нет store_id вовсе.
-- Возврат знает только идентификатор заказа, и без этой привязки отнести его к
-- магазину нельзя ничем.
--
-- Здесь же сведены две оси даты, между которыми выбирает витрина:
-- order_date - когда покупку сделали, returned_date - когда возврат приехал.
-- Витрина продаж относит возврат к первой, и поэтому вчерашнее число меняется;
-- вторая остается рядом, чтобы было видно, откуда взялось изменение.
--
-- Джойн левый по той же причине, что и в сборке чека: возврат-сирота должен
-- остаться видимым со store_id = null, а не исчезнуть до того, как его найдет
-- тест not_null на store_id. is_cancelled тянется сюда как раз для проверки -
-- возврат к отмененному заказу невозможен по смыслу, и это спрашивает
-- tests/assert_returns_belong_to_live_orders.sql.

with returns as (
    select * from {{ ref('stg_returns') }}
),

orders as (
    select * from {{ ref('stg_orders') }}
)

select
    r.return_id,
    r.order_id,
    r.line_no,

    o.store_id,
    o.is_cancelled,

    r.order_date,
    r.returned_date,
    r.delay_days,

    r.quantity,
    r.returned_amount

from returns r
left join orders o on o.order_id = r.order_id
