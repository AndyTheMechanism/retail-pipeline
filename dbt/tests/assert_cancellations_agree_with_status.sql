-- Отмены и статусы заказов согласованы в обе стороны.
--
-- Источник сообщает про отмену дважды: статусом в самом заказе и отдельной
-- строкой в таблице отмен, которая приезжает на день-два позже. Два
-- представления одного факта - это всегда место, где они разойдутся, и
-- разойтись они могут в обе стороны:
--
--   заказ отменен, а строки отмены нет  - отмена потерялась по дороге;
--   строка отмены есть, а заказ живой   - отмена приехала, а статус не обновили.
--
-- Второй случай опаснее первого: заказ с потерянным статусом продолжает
-- считаться выручкой.
--
-- ПРО ГРАНИЦУ ГОРИЗОНТА, и это главное в этом файле.
--
-- Первая проверка ограничена по датам, вторая нет, и разница не произвольная.
-- Отмена приезжает на 0-2 дня позже заказа, поэтому у заказов последних двух
-- дней горизонта строки отмены может еще не быть - не потому, что она
-- потерялась, а потому, что ее время не пришло. На этих данных так и есть: 56
-- заказов 29 и 30 июня отменены статусом, а строк отмены за них нет, потому
-- что их дата вышла за конец горизонта и генератор их отбросил.
--
-- Первая версия теста этого не учитывала и падала на ровном месте. Вывод общий
-- и стоит запомнить: проверка, которая сравнивает два источника с разной
-- задержкой, обязана быть ограничена по датам на глубину этой задержки. Иначе
-- она красная всегда, ее выключают, и гейта больше нет.
--
-- Обратная сторона в границе не нуждается: строка отмены при живом заказе -
-- это рассинхрон в любой день, включая последний.

{% set cancellation_delay_days = var('cancellation_delay_days', 2) %}

with settled as (
    -- Дата, до которой окно приезда отмен уже закрылось.
    select max(order_date) - {{ cancellation_delay_days }} as last_settled_date
    from {{ ref('stg_orders') }}
),

cancelled_without_row as (
    select
        o.order_id,
        o.order_date,
        true  as status_says_cancelled,
        false as has_cancellation_row
    from {{ ref('stg_orders') }} o, settled s
    where o.is_cancelled
      and o.order_date <= s.last_settled_date
      and not exists (
          select 1 from {{ ref('stg_cancellations') }} c
          where c.order_id = o.order_id
      )
),

row_without_cancelled_status as (
    select
        c.order_id,
        c.order_date,
        false as status_says_cancelled,
        true  as has_cancellation_row
    from {{ ref('stg_cancellations') }} c
    where exists (
        select 1 from {{ ref('stg_orders') }} o
        where o.order_id = c.order_id
          and not o.is_cancelled
    )
)

select * from cancelled_without_row
union all
select * from row_without_cancelled_status
