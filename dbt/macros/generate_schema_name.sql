{#
    Schemas are named the way a reader would name them.

    By default dbt glues the target schema onto the declared one and hands you
    analytics_staging, analytics_marts. Here that gluing is switched off: a
    model lands in exactly the schema its directory declared, and a query
    against a mart reads select * from marts.mart_store_daily_sales rather than
    carrying a prefix you have to remember.

    The price is named out loud, because the prefix is not there for nothing: on
    a shared database two developers with different target schemas would
    overwrite each other. Here the database is local and one per project, so
    there is nothing to pay. The moment it becomes shared this macro has to
    go — and this is a decision that depends on circumstances rather than on
    taste.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
