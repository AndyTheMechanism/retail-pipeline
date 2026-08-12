-- Postgres при первом старте создает только базу из POSTGRES_DB, то есть
-- warehouse. Метабаза Airflow держится отдельно: смешивать служебные таблицы
-- планировщика с витринами - значит однажды уронить одно, разбираясь с другим.
--
-- Скрипты из /docker-entrypoint-initdb.d выполняются ровно один раз, при
-- создании тома. Повторный `make up` их не запускает, `make reset` - запускает.

CREATE DATABASE airflow_meta;

COMMENT ON DATABASE warehouse IS 'Хранилище: сырой слой и витрины';
COMMENT ON DATABASE airflow_meta IS 'Метабаза Airflow, появится в работе на этапе 4';
