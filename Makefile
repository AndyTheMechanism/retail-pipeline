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

.DEFAULT_GOAL := help
.PHONY: help up down reset psql seed run test backfill

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

# Цели ниже объявлены здесь с первого дня намеренно: они задают форму проекта
# и не дают ей поплыть. Пока этап не сделан - цель честно падает, а не делает
# вид, что отработала.

seed: ## Загрузить синтетику (этап 1)
	@echo 'Этап 1 не сделан: генератора синтетики еще нет.'
	@echo 'Условие приемки: make seed дважды подряд дает идентичное состояние базы.'
	@exit 1

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
