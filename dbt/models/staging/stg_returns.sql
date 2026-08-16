-- Возвраты.
--
-- delay_days считается здесь, а не разовой командой, потому что на этом числе
-- стоят два решения: размер окна пересчета - 28 дней, return_window_days в
-- dbt_project.yml - и флаг return_outside_window, который ловит возвраты,
-- приехавшие за пределами окна. Замер, от которого что-то зависит, должен быть
-- колонкой, к которой можно написать запрос, а не строчкой в выводе скрипта.

with source as (
    select * from {{ source('raw', 'returns') }}
)

select
    return_id,
    order_id,
    order_date,
    line_no,
    returned_date,
    quantity,
    returned_amount,

    returned_date - order_date as delay_days

from source
