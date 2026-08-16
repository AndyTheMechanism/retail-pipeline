-- Флаги качества: что в этот день у этого магазина считать нельзя.
--
-- Отдельная таблица, а не колонки в витринах, и это решение стоит объяснить.
-- Витрины отдают факты, а суждение о том, можно ли факту верить, - другая
-- сущность с другой судьбой: флаги добавляются и уточняются, их бывает
-- несколько на одну строку, и у каждого своя причина. Строкой это растет
-- естественно, колонкой - нет: каждый новый флаг менял бы схему обеих витрин.
-- На этапе 5 в эту же форму ляжет журнал ревизий.
--
-- Флаг не роняет цепочку и не подменяет число оценкой. Он говорит ровно одно:
-- этой цифре доверять нельзя, а остальная сеть считается как считалась. Разница
-- со стопом здесь не в тяжести проблемы, а в том, чинится ли она пересчетом.
-- Задвоенное зерно чинится, сломанный дверной счетчик - нет, и останавливать
-- из-за него всю сеть значит остаться без цифр по всем магазинам вместо
-- одного.
--
-- Зерно: магазин, день, имя проверки. Имя английское - по нему фильтруют и
-- джойнят; причина русская - ее читает человек в отчете.

{% set return_window_days = var('return_window_days', 30) %}

with conversion_above_threshold as (
    -- Боевое правило из кейса про дверной счетчик: чеков больше, чем
    -- посетителей, физически не бывает. Порог 0,95, а не 1,0, потому что
    -- прибор врет раньше, чем упирается в равенство.
    --
    -- Правило записано умножением, а не делением, намеренно: при нуле
    -- посетителей отношение не существует, а произведение дает ноль, и живые
    -- чеки при нулевом счетчике попадают под тот же флаг, а не выпадают из
    -- проверки в null. Мертвый прибор - худший случай сломанного, и терять
    -- его на форме записи было бы обидно.
    select
        store_id,
        traffic_date as flag_date,
        'conversion_above_threshold' as check_name,
        format(
            'чеков %s при %s посетителях - счетчику доверять нельзя',
            orders_offline, visitors
        ) as reason,
        orders_offline::numeric  as measured_value,
        (visitors * 0.95)::numeric as threshold_value
    from {{ ref('mart_store_daily_conversion') }}
    where has_orders
      and has_traffic
      and orders_offline > visitors * 0.95
),

return_outside_window as (
    -- Возврат, приехавший позже окна пересчета, меняет выручку дня, который
    -- пересобирать уже не будут. Число за тот день останется неверным, и
    -- узнать об этом можно только отсюда.
    --
    -- Это флаг, а не стоп: данные в порядке, мало окно. Тест, который роняет
    -- цепочку из-за того, что окно выбрано узко, останавливал бы работу
    -- каждый день до тех пор, пока кто-нибудь не поменяет константу.
    select
        store_id,
        order_date as flag_date,
        'return_outside_window' as check_name,
        format(
            'возвратов позже окна: %s, худший через %s дней при окне %s',
            count(*), max(delay_days), {{ return_window_days }}
        ) as reason,
        max(delay_days)::numeric as measured_value,
        {{ return_window_days }}::numeric as threshold_value
    from {{ ref('int_returns_attributed') }}
    where delay_days > {{ return_window_days }}
    group by store_id, order_date
),

orders_partition_missing as (
    -- День, за который из источника не пришло ни одной строки заказов.
    --
    -- Стопом это становится только для целевой даты прогона - ее проверяет
    -- assert_source_is_fresh, и там цепочка встает. Дыра, оставшаяся в
    -- истории, останавливать сегодняшнюю работу не должна, но и молчать о
    -- себе не должна тоже: она отмечена здесь и видна запросом.
    select
        store_id,
        order_date as flag_date,
        'orders_partition_missing' as check_name,
        'за этот день не приехало ни одной строки заказов' as reason,
        0::numeric    as measured_value,
        null::numeric as threshold_value
    from {{ ref('mart_store_daily_sales') }}
    where not has_orders
),

traffic_partition_missing as (
    select
        store_id,
        traffic_date as flag_date,
        'traffic_partition_missing' as check_name,
        'за этот день не приехало ни одной строки трафика' as reason,
        0::numeric    as measured_value,
        null::numeric as threshold_value
    from {{ ref('mart_store_daily_conversion') }}
    where not has_traffic
),

flagged as (
    select * from conversion_above_threshold
    union all
    select * from return_outside_window
    union all
    select * from orders_partition_missing
    union all
    select * from traffic_partition_missing
)

select
    store_id,
    flag_date,
    check_name,
    reason,
    measured_value,
    threshold_value
from flagged
