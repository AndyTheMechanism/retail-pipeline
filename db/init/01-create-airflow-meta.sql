-- On first start Postgres creates only the database named in POSTGRES_DB,
-- which is warehouse. Airflow's metadata database is kept separate: mixing the
-- scheduler's own tables in with the marts means one day taking one of them
-- down while sorting out the other.
--
-- Scripts in /docker-entrypoint-initdb.d run exactly once, when the volume is
-- created. A repeat `make up` does not run them; `make reset` does.

CREATE DATABASE airflow_meta;

COMMENT ON DATABASE warehouse IS 'Warehouse: the raw layer and the marts';
COMMENT ON DATABASE airflow_meta IS 'Airflow metadata: scheduler state and run history';
