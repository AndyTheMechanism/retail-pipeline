{#
    Uniqueness of a composite grain.

    The built-in unique test works on a single column, while almost every grain
    here is made of two: a line is (order_id, line_no), a mart is (store_id,
    date). The usual move at this point is to reach for dbt_utils and
    unique_combination_of_columns — taking on a package, a lock file and a
    question about version compatibility for the sake of six lines anyone should
    be able to write themselves.

    The test returns the offending rows rather than a boolean answer: dbt counts
    a test as failed if the query returns even one row. So the output shows
    straight away which grain got doubled, and how many times over.
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
