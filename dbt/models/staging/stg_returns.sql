-- Возвраты.
--
-- delay_days считается здесь, а не разовой командой, потому что на этом числе
-- стоят два будущих решения: размер окна пересчета на этапе 4 и флаг "возврат
-- приехал за пределами окна" на этапе 3. Замер, от которого что-то зависит,
-- должен быть колонкой, к которой можно написать запрос, а не строчкой в
-- выводе скрипта.

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
