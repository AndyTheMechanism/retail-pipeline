# Пайплайн розничной воронки - управление окружением.
#
# Движок контейнеров определяется сам: здесь стоит podman, у проверяющего
# скорее всего docker. Compose-файл для обоих один и тот же, разница только в
# том, какую команду звать.

ENGINE  := $(shell if command -v docker >/dev/null 2>&1; then echo docker; else echo podman; fi)
COMPOSE := $(shell if command -v docker >/dev/null 2>&1; then echo "docker compose"; else echo "podman-compose"; fi)

DB_CONTAINER := retail-pipeline-db
DB_USER      := pipeline
DB_NAME      := warehouse
DB_PASSWORD  := $(or $(POSTGRES_PASSWORD),pipeline)
DB_PORT      := $(or $(POSTGRES_PORT),5432)

# Интерпретатор можно подменить: make venv-dbt PYTHON=python3.12. Нужно это
# ровно в одном случае - если системный python3 старше того, что требует dbt.
PYTHON ?= python3

VENV := .venv
PY   := $(VENV)/bin/python

# Окружение dbt отдельное от генератора. Почему - в requirements-dbt.txt.
DBT_VENV := .venv-dbt
DBT      := $(DBT_VENV)/bin/dbt
DBT_DIR  := dbt

# Профиль лежит в проекте, а не в домашнем каталоге. Без этой переменной dbt
# ушел бы искать его в ~/.dbt и нашел бы там чужой, от другого проекта.
# В окружение уходит через голый export ниже.
DBT_PROFILES_DIR := $(CURDIR)/$(DBT_DIR)

# Окружение сверки. Третье, и по той же причине, что второе: pandas не нужен
# ни генератору, ни моделям.
CHECKS_VENV := .venv-checks
CHECKS_PY   := $(CHECKS_VENV)/bin/python

# Переменные dbt пробрасываются одной строкой, ровно как их понимает сам dbt:
#   make test VARS='{run_date: 2025-02-26}'
#   make models VARS='{return_window_days: 14}'
DBT_VARS = $(if $(VARS),--vars '$(VARS)',)

# Окружение Airflow, четвертое. Ставится по официальному constraints-файлу,
# адрес которого зависит и от версии Airflow, и от версии интерпретатора.
# Версия живет в requirements-airflow.txt, и берется она оттуда, чтобы не
# разъехаться: два места для одного числа рано или поздно разойдутся.
AIRFLOW_VENV    := .venv-airflow
AIRFLOW         := $(AIRFLOW_VENV)/bin/airflow
AIRFLOW_DAG     := retail_pipeline
AIRFLOW_VERSION := $(shell sed -n 's/^apache-airflow.*==\(.*\)$$/\1/p' requirements-airflow.txt)
AIRFLOW_PY_TAG  := $(shell $(PYTHON) -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
AIRFLOW_CONSTRAINTS := https://raw.githubusercontent.com/apache/airflow/constraints-$(AIRFLOW_VERSION)/constraints-$(AIRFLOW_PY_TAG).txt

# Метабаза мигрируется один раз; отметка о том, что это уже сделано.
AIRFLOW_DB_STAMP := $(CURDIR)/airflow/.db-migrated

# Airflow настраивается переменными окружения, а не файлом. Свой airflow.cfg он
# сгенерирует сам - в нем больше ста килобайт чужих умолчаний, и объяснить их
# нельзя. Здесь пять строк, и каждая объяснима за минуту.
AIRFLOW_HOME := $(CURDIR)/airflow
AIRFLOW__DATABASE__SQL_ALCHEMY_CONN := postgresql+psycopg2://$(DB_USER):$(DB_PASSWORD)@127.0.0.1:$(DB_PORT)/airflow_meta
AIRFLOW__CORE__DAGS_FOLDER := $(CURDIR)/airflow/dags
AIRFLOW__CORE__LOAD_EXAMPLES := False
AIRFLOW__CORE__EXECUTOR := LocalExecutor

# Если рядом лежит .env - подхватить и передать генератору. Файл
# необязательный: значения по умолчанию совпадают с docker-compose.yml, и на
# чистом клоне все работает без него.
-include .env
export

.DEFAULT_GOAL := help
.PHONY: help up down reset psql venv venv-dbt venv-airflow airflow-init airflow seed seed-day defects verify measure-returns models dbt reconcile revisions run test backfill scenario-late-return scenario-missing-partition scenario-broken-counter

help: ## Показать список целей
	@echo 'Пайплайн розничной воронки. Движок: $(ENGINE)'
	@echo
	@grep -E '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

# -h 127.0.0.1 в проверке готовности не для красоты. Пока идут скрипты
# инициализации, Postgres слушает только unix-сокет: проверка через сокет
# засчитает временный сервер, и следующая команда упрется в "the database
# system is shutting down". По TCP он в этот момент не отвечает.
up: ## Поднять Postgres и дождаться готовности
	@command -v $(ENGINE) >/dev/null 2>&1 || { \
		echo 'Не найден движок контейнеров: нужен docker или podman.'; \
		echo 'На Windows запускать под WSL2 - нативной поддержки Makefile там нет.'; \
		exit 1; }
	$(COMPOSE) up -d
	@printf 'Жду готовности базы'
	@for i in $$(seq 1 60); do \
		if $(ENGINE) exec $(DB_CONTAINER) pg_isready -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) >/dev/null 2>&1; then \
			printf ' готово\n'; \
			$(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) -tAc \
				"select 'базы на месте: ' || string_agg(datname, ', ' order by datname) \
				 from pg_database where datname in ('warehouse', 'airflow_meta')"; \
			exit 0; \
		fi; \
		printf '.'; \
		sleep 1; \
	done; \
	printf '\nБаза не поднялась за 60 секунд. Смотреть: $(COMPOSE) logs postgres\n'; \
	exit 1

down: ## Остановить контейнер, данные сохранить
	$(COMPOSE) down

reset: ## Снести контейнер вместе с данными - следующий up соберет базу заново
	$(COMPOSE) down -v

psql: ## Открыть psql в контейнере, ставить клиент на хост не нужно
	$(ENGINE) exec -it $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME)

# Проверка перед сборкой окружения. На Debian и Ubuntu - а значит и в типичной
# WSL - модуль venv поставляется отдельным пакетом, и без него python3 -m venv
# падает с сообщением про ensurepip, по которому непонятно, что делать.
# Пять строк здесь дешевле одного такого тупика у проверяющего.
$(VENV): requirements.txt
	@python3 -c 'import ensurepip' >/dev/null 2>&1 || { \
		echo 'Нет модуля venv для python3.'; \
		echo '  Debian, Ubuntu, WSL:  sudo apt install -y python3-venv'; \
		echo '  Fedora:               sudo dnf install -y python3'; \
		exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip -q install --upgrade pip
	$(VENV)/bin/pip -q install -r requirements.txt
	@touch $(VENV)

venv: $(VENV) ## Собрать виртуальное окружение генератора

# Проверка версии стоит до создания окружения: dbt требует Python 3.10 и выше,
# а его собственное сообщение об этом тонет в выводе резолвера pip.
$(DBT_VENV): requirements-dbt.txt
	@$(PYTHON) -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || { \
		echo 'dbt требует Python 3.10 или новее, а $(PYTHON) старше.'; \
		echo '  Debian, Ubuntu, WSL:  sudo apt install -y python3.12 python3.12-venv'; \
		echo '  Fedora:               sudo dnf install -y python3.12'; \
		echo 'Дальше звать с явным интерпретатором: make venv-dbt PYTHON=python3.12'; \
		exit 1; }
	$(PYTHON) -m venv $(DBT_VENV)
	$(DBT_VENV)/bin/pip -q install --upgrade pip
	$(DBT_VENV)/bin/pip -q install -r requirements-dbt.txt
	@touch $(DBT_VENV)

venv-dbt: $(DBT_VENV) ## Собрать виртуальное окружение dbt

# Официальный compose-файл Airflow не используется намеренно: восемь сервисов и
# больше двухсот строк. Здесь Airflow ставится локально, а метабаза живет во
# второй базе того же контейнера, что и хранилище.
$(AIRFLOW_VENV): requirements-airflow.txt
	@test -n "$(AIRFLOW_VERSION)" || { \
		echo 'Не удалось прочитать версию Airflow из requirements-airflow.txt.'; \
		exit 1; }
	$(PYTHON) -m venv $(AIRFLOW_VENV)
	$(AIRFLOW_VENV)/bin/pip -q install --upgrade pip
	@echo 'Ставлю Airflow $(AIRFLOW_VERSION) по constraints для Python $(AIRFLOW_PY_TAG)'
	$(AIRFLOW_VENV)/bin/pip -q install -r requirements-airflow.txt \
		--constraint "$(AIRFLOW_CONSTRAINTS)"
	@touch $(AIRFLOW_VENV)

venv-airflow: $(AIRFLOW_VENV) ## Собрать виртуальное окружение Airflow

$(AIRFLOW_DB_STAMP): $(AIRFLOW_VENV)
	$(AIRFLOW) db migrate
	@touch $@

airflow-init: up $(AIRFLOW_DB_STAMP) ## Создать метабазу Airflow

airflow: up $(AIRFLOW_DB_STAMP) ## Поднять веб-интерфейс Airflow на localhost:8080
	$(AIRFLOW) standalone

$(CHECKS_VENV): requirements-checks.txt
	$(PYTHON) -m venv $(CHECKS_VENV)
	$(CHECKS_VENV)/bin/pip -q install --upgrade pip
	$(CHECKS_VENV)/bin/pip -q install -r requirements-checks.txt
	@touch $(CHECKS_VENV)

# Пустое сырье - самая тихая ошибка на этом этапе: dbt честно соберет витрины
# из ничего и отчитается об успехе. Проверка стоит одной команды, а разбор
# "почему в витрине ноль строк" - вечера.
models: up $(DBT_VENV) ## Собрать витрины с гейтом: dbt build
	@rows=$$($(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) \
		-tAc 'select count(*) from raw.orders' 2>/dev/null || echo 0); \
	test "$$rows" -gt 0 2>/dev/null || { \
		echo 'В сыром слое пусто - собирать нечего. Сначала make seed.'; \
		exit 1; }
	$(DBT) build --project-dir $(DBT_DIR) $(DBT_VARS)

# .PHONY у этой цели не для порядка: рядом лежит каталог dbt/, и без нее make
# посмотрит на каталог, решит, что цель свежая, и молча ничего не сделает.
dbt: up $(DBT_VENV) ## Позвать dbt напрямую, например make dbt ARGS=debug
	@test -n "$(ARGS)" || { echo 'Нужны аргументы, например: make dbt ARGS=debug'; exit 1; }
	$(DBT) $(ARGS) --project-dir $(DBT_DIR)

# Сверка нарочно написана не на SQL и живет снаружи пайплайна: проверка тем же
# инструментом повторила бы ошибку модели и не заметила ее.
reconcile: up $(CHECKS_VENV) ## Сверить витрины с сырым слоем
	$(CHECKS_PY) checks/reconcile.py

# seed сам поднимает базу и собирает окружение. Иначе "воспроизводится одной
# командой" превратилось бы в три команды с примечанием.
seed: up $(VENV) ## Сгенерировать и загрузить синтетику за весь горизонт
	$(PY) -m generator seed

seed-day: up $(VENV) ## Пересобрать одну партицию: make seed-day DATE=2026-03-14
	@test -n "$(DATE)" || { echo 'Нужна дата: make seed-day DATE=2026-03-14'; exit 1; }
	$(PY) -m generator day $(DATE)

defects: $(VENV) ## Показать карту заложенных дефектов
	$(PY) -m generator defects

verify: up $(VENV) ## Контрольные суммы таблиц - чем проверяется повторяемость
	$(PY) -m generator verify

measure-returns: up $(VENV) ## Распределение задержки возвратов, вход для этапа 4
	$(PY) -m generator measure-returns

# Цели ниже объявлены с первого дня намеренно: они задают форму проекта и не
# дают ей поплыть. Пока этап не сделан - цель честно падает, а не делает вид,
# что отработала.

# dbt build, а не dbt run плюс dbt test: в build тесты идут вперемежку с
# моделями по графу, и упавший тест на слое ниже просто не пускает сборку
# витрин дальше. Это и есть "битые данные не публикуются" - без своей механики
# со схемами-кандидатами и подменами. Прошлая витрина при этом остается на
# месте нетронутой: вчерашнее число не переписано, а не переписано неверно.
test: up $(DBT_VENV) ## Прогнать тесты-гейты по уже собранным моделям
	$(DBT) test --project-dir $(DBT_DIR) $(DBT_VARS)

# Прогон идет через airflow dags test: DAG выполняется целиком и синхронно, без
# поднятого планировщика. Для проекта, который должен заводиться одной командой,
# это важнее, чем демонстрация демонов: зависимости, ретраи и расписание в DAG
# объявлены, а запустить его можно, ничего не поднимая. Веб-интерфейс, если он
# нужен, поднимается отдельно - make airflow.
run: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Прогнать пайплайн за дату: make run DATE=2026-03-14
	@test -n "$(DATE)" || { echo 'Нужна дата: make run DATE=2026-03-14'; exit 1; }
	$(AIRFLOW) dags test $(AIRFLOW_DAG) $(DATE)

revisions: up ## Показать журнал ревизий: что изменилось, когда и почему
	@$(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) -P pager=off -c \
		"select store_id, order_date, revised_at, revenue_net_was, revenue_net_became, \
		        revenue_net_delta, reason \
		 from marts.mart_store_daily_revisions \
		 order by revised_at desc, revenue_net_delta limit 20"

# Сценарии зовут те же команды, что позвал бы человек, а не лезут в базу в обход
# пайплайна: иначе показ доказывал бы работу показа.
scenario-late-return: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Сценарий: вчерашнее число изменилось, и видно почему
	$(PY) -m scenarios late-return

scenario-missing-partition: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Сценарий: источник не приехал, цепочка встала
	$(PY) -m scenarios missing-partition

scenario-broken-counter: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Сценарий: прибор врет, сеть считается дальше
	$(PY) -m scenarios broken-counter

# Даты считает python, а не date -d: GNU-шный синтаксис сдвига даты есть не
# везде, а интерпретатор здесь и так требуется.
backfill: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Пересчитать период: make backfill FROM=2026-03-01 TO=2026-03-07
	@test -n "$(FROM)" -a -n "$(TO)" || { \
		echo 'Нужны границы: make backfill FROM=2026-03-01 TO=2026-03-07'; exit 1; }
	@for day in $$($(PY) -c "import datetime as dt; \
a = dt.date.fromisoformat('$(FROM)'); b = dt.date.fromisoformat('$(TO)'); \
print(' '.join(str(a + dt.timedelta(days=i)) for i in range((b - a).days + 1)))"); do \
		echo; echo "=== $$day ==="; \
		$(AIRFLOW) dags test $(AIRFLOW_DAG) $$day || exit 1; \
	done
