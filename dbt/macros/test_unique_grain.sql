{#
    Уникальность составного зерна.

    Штатный тест unique работает по одной колонке, а зерно почти везде здесь из
    двух: позиция это (order_id, line_no), витрина это (store_id, дата). Обычно
    в этом месте ставят dbt_utils ради unique_combination_of_columns - и тянут
    пакет, файл блокировки и вопрос про совместимость версий ради шести строк,
    которые надо уметь написать самому.

    Тест возвращает строки-нарушители, а не булев ответ: dbt считает тест
    провалившимся, если запрос вернул хоть одну строку. Поэтому в выводе сразу
    видно, какое именно зерно задвоилось и во сколько раз.
#}
{% test unique_grain(model, columns) %}

with grain as (
    select
        {{ columns | join(',\n        ') }},
        count(*) as rows_per_key
    from {{ model }}
    group by {{ range(1, columns | length + 1) | join(', ') }}
)

select *
from grain
where rows_per_key > 1

{% endtest %}
