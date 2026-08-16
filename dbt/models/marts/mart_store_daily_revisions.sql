-- Журнал ревизий: какое опубликованное число изменилось, когда и почему.
--
-- Ядро проекта. Тезис работодателя - "витрина обновилась молча и неправильно";
-- ответ проекта - "витрина обновилась, и вот строка, которая говорит, что было,
-- что стало и из-за чего". Все остальное в репозитории обслуживает эту таблицу.
--
-- Строка появляется только у той пары "магазин и день", чье опубликованное
-- число действительно поехало. Первая версия ревизией не считается: она не
-- изменение, а первое появление числа.
--
-- Причина не угадывается, а выводится из того, какие величины разошлись.
-- Возвраты изменились при неизменном брутто - приехал поздний возврат, и это
-- штатная жизнь домена. Изменилось брутто - пересобрано сырье, и вот это уже
-- повод посмотреть, почему.
--
-- Представление, а не таблица: это отчет поверх снимка, а не витрина. Считать
-- его заранее незачем, а лишняя материализация создала бы второе место, где
-- журнал может отстать от истории.

{{ config(materialized = 'view') }}

with versions as (
    select
        store_id,
        order_date,

        orders_count,
        revenue_gross,
        returns_amount,
        revenue_net,

        dbt_valid_from as revised_at,

        row_number()           over w as version_no,
        lag(orders_count)      over w as orders_count_before,
        lag(revenue_gross)     over w as revenue_gross_before,
        lag(returns_amount)    over w as returns_amount_before,
        lag(revenue_net)       over w as revenue_net_before

    from {{ ref('snap_store_daily_sales') }}

    window w as (partition by store_id, order_date order by dbt_valid_from)
)

select
    store_id,
    order_date,
    revised_at,
    version_no,

    revenue_net_before                   as revenue_net_was,
    revenue_net                          as revenue_net_became,
    revenue_net - revenue_net_before     as revenue_net_delta,

    returns_amount_before                as returns_amount_was,
    returns_amount                       as returns_amount_became,

    case
        when returns_amount is distinct from returns_amount_before
         and revenue_gross  is not distinct from revenue_gross_before
            then 'поздний возврат'
        when revenue_gross is distinct from revenue_gross_before
            then 'пересобрано сырье'
        when orders_count is distinct from orders_count_before
            then 'изменился состав заказов'
        else 'прочее'
    end as reason

from versions
where version_no > 1
