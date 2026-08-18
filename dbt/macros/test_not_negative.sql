{#
    A value is never negative.

    The second test people normally reach for dbt_utils to get. Here it is
    needed for quantities where a negative value means not a rare case but an
    impossibility: a return before its order, a negative number of visitors, a
    negative quantity on a line.

    Zero is allowed deliberately. A zero quantity is one of the planted
    outliers, and catching it is the job of the outlier test, not of a test
    about signs: mixing two different complaints into one check gets you a
    failure report nobody can read.
#}
{% test not_negative(model, column_name) %}

select {{ column_name }}
from {{ model }}
where {{ column_name }} < 0

{% endtest %}
