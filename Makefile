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

# Если рядом лежит .env - подхватить и передать генератору. Файл
# необязательный: значения по умолчанию совпадают с docker-compose.yml, и на
# чистом клоне все работает без него.
-include .env
export

.DEFAULT_GOAL := help
.PHONY: help up down reset psql venv venv-dbt seed seed-day defects verify measure-returns dbt run test backfill

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

# .PHONY у этой цели не для порядка: рядом лежит каталог dbt/, и без нее make
# посмотрит на каталог, решит, что цель свежая, и молча ничего не сделает.
dbt: up $(DBT_VENV) ## Позвать dbt напрямую, например make dbt ARGS=debug
	@test -n "$(ARGS)" || { echo 'Нужны аргументы, например: make dbt ARGS=debug'; exit 1; }
	$(DBT) $(ARGS) --project-dir $(DBT_DIR)

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

run: ## Прогнать пайплайн за дату (этапы 2-4)
	@echo 'Этапы 2-4 не сделаны: моделей dbt и DAG еще нет.'
	@echo 'Условие приемки: повторный прогон за ту же дату не задваивает данные.'
	@exit 1

test: ## Прогнать тесты-гейты (этап 3)
	@echo 'Этап 3 не сделан: тестов еще нет.'
	@echo 'Условие приемки: битые данные не публикуются, помеченный магазин не роняет сеть.'
	@exit 1

backfill: ## Пересчитать за период: make backfill FROM=... TO=... (этап 4)
	@echo 'Этап 4 не сделан: пересчета еще нет.'
	@echo 'Условие приемки: пересчет за произвольный прошлый день - одна команда, тот же результат.'
	@exit 1
