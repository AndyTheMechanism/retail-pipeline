# Retail funnel pipeline — environment management.
#
# The container engine is detected, not configured: podman is what is installed
# here, whoever reviews this most likely has docker. The compose file is the
# same for both, the only difference is which command to call.

# .env is included FIRST, before a single assignment, and that is not style but
# a bug that was fixed. It used to sit at the bottom of the file while the
# variables above it expanded with := — that is, the values from .env never
# reached them. It looked like this: a port that was already in use was
# overridden in .env, the database dutifully came up on the new port, the
# generator and dbt too, while the Airflow connection string stayed on 5432 and
# silently went off into somebody else's Postgres. Exactly the silent
# divergence .env.example warns about.
-include .env

ENGINE  := $(shell if command -v docker >/dev/null 2>&1; then echo docker; else echo podman; fi)
COMPOSE := $(shell if command -v docker >/dev/null 2>&1; then echo "docker compose"; else echo "podman-compose"; fi)

DB_CONTAINER := retail-pipeline-db
DB_NAME      := warehouse
DB_USER      := $(or $(POSTGRES_USER),pipeline)
DB_PASSWORD  := $(or $(POSTGRES_PASSWORD),pipeline)
DB_PORT      := $(or $(POSTGRES_PORT),5432)

# The interpreter can be swapped: make venv-dbt PYTHON=python3.12. That is
# needed in exactly one case — when the system python3 is older than dbt wants.
PYTHON ?= python3

VENV := .venv
PY   := $(VENV)/bin/python

# The dbt environment is separate from the generator one. Why — in
# requirements-dbt.txt.
DBT_VENV := .venv-dbt
DBT      := $(DBT_VENV)/bin/dbt
DBT_DIR  := dbt

# The profile lives in the project, not in the home directory. Without this
# variable dbt would go looking in ~/.dbt and find somebody else's profile
# there, left over from another project. It reaches the environment through the
# bare export below.
DBT_PROFILES_DIR := $(CURDIR)/$(DBT_DIR)

# The reconciliation environment. The third one, and for the same reason as the
# second: pandas is needed neither by the generator nor by the models.
CHECKS_VENV := .venv-checks
CHECKS_PY   := $(CHECKS_VENV)/bin/python

# dbt variables are passed through as one string, exactly as dbt reads them:
#   make test VARS='{run_date: 2025-02-26}'
#   make models VARS='{return_window_days: 14}'
DBT_VARS = $(if $(VARS),--vars '$(VARS)',)

# The Airflow environment, the fourth. It is installed against the official
# constraints file, whose address depends on both the Airflow version and the
# interpreter version. The version lives in requirements-airflow.txt and is
# read from there so the two cannot drift apart: two places for one number will
# part ways sooner or later.
AIRFLOW_VENV    := .venv-airflow
AIRFLOW         := $(AIRFLOW_VENV)/bin/airflow
AIRFLOW_DAG     := retail_pipeline
AIRFLOW_VERSION := $(shell sed -n 's/^apache-airflow.*==\(.*\)$$/\1/p' requirements-airflow.txt)
AIRFLOW_PY_TAG  := $(shell $(PYTHON) -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
AIRFLOW_CONSTRAINTS := https://raw.githubusercontent.com/apache/airflow/constraints-$(AIRFLOW_VERSION)/constraints-$(AIRFLOW_PY_TAG).txt

# The metadata database is migrated once; this marks that it has been done.
AIRFLOW_DB_STAMP := $(CURDIR)/airflow/.db-migrated

# Airflow is configured with environment variables rather than a file. It
# generates an airflow.cfg of its own — over a hundred kilobytes of somebody
# else's defaults, and those cannot be explained. Here there are six lines, and
# every one of them can be explained in a minute.
AIRFLOW_HOME := $(CURDIR)/airflow
AIRFLOW__DATABASE__SQL_ALCHEMY_CONN := postgresql+psycopg2://$(DB_USER):$(DB_PASSWORD)@127.0.0.1:$(DB_PORT)/airflow_meta
AIRFLOW__CORE__DAGS_FOLDER := $(CURDIR)/airflow/dags
AIRFLOW__CORE__LOAD_EXAMPLES := False
AIRFLOW__CORE__EXECUTOR := LocalExecutor

# The same decision as for Postgres in docker-compose.yml, and for the same
# reason. The Airflow default is 0.0.0.0, meaning the UI is visible to the
# whole network. On top of that, standalone creates an admin user with the
# password in plain text and says so itself. There is no reason to keep that
# reachable from somebody else's Wi-Fi: one person looks at it, from this very
# machine.
AIRFLOW__API__HOST := 127.0.0.1

# The bare export hands every variable above to child processes: the generator,
# dbt and Airflow. The .env file stays optional — the defaults match
# docker-compose.yml, and a clean clone works without it.
export

.DEFAULT_GOAL := help
.PHONY: help demo up down reset psql venv venv-dbt venv-airflow airflow-init airflow seed seed-day defects verify measure-returns models dbt docs dictionary reconcile revisions run test backfill scenario-late-return scenario-missing-partition scenario-broken-counter

help: ## Show the list of targets
	@echo 'Retail funnel pipeline. Engine: $(ENGINE)'
	@echo
	@grep -E '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

# One command for whoever opens the repository for the first time: data, marts
# behind the gate, and a reconciliation of them against the raw layer. It
# exists because "reproduces from scratch with one command" is a completion
# criterion for this project, and without this target there would be two
# commands and the promise would be inexact.
demo: seed models reconcile ## From scratch: data, marts behind the gate, reconciliation
	@echo
	@echo 'Done. Next:'
	@echo '  make run DATE=2026-03-14   run the pipeline for one date'
	@echo '  make revisions             what changed and why'
	@echo "  make scenario-late-return  yesterday's number moves before your eyes"

# The -h 127.0.0.1 in the readiness check is not decoration. While the
# initialisation scripts are running, Postgres listens on the unix socket only:
# a check through the socket would count the temporary server, and the next
# command would run into "the database system is shutting down". Over TCP it
# does not answer at that moment.
up: ## Start Postgres and wait until it is ready
	@command -v $(ENGINE) >/dev/null 2>&1 || { \
		echo 'No container engine found: docker or podman is required.'; \
		echo 'On Windows, run under WSL2 - there is no native Makefile support there.'; \
		exit 1; }
	@# An engine on its own does not mean compose is there. The combination
	@# "podman without podman-compose" is common, and on Fedora with the
	@# podman-docker package the docker command exists but has no compose
	@# subcommand - and then the error is about a missing command rather than
	@# about what to install.
	@$(COMPOSE) version >/dev/null 2>&1 || { \
		echo 'Engine $(ENGINE) is here, but its compose is not.'; \
		echo '  Fedora, podman:       sudo dnf install -y podman-compose'; \
		echo '  Debian, Ubuntu, WSL:  sudo apt install -y podman-compose'; \
		echo '  docker:               add the docker-compose-plugin package'; \
		exit 1; }
	$(COMPOSE) up -d
	@printf 'Waiting for the database'
	@for i in $$(seq 1 60); do \
		if $(ENGINE) exec $(DB_CONTAINER) pg_isready -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) >/dev/null 2>&1; then \
			printf ' ready\n'; \
			$(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) -tAc \
				"select 'databases in place: ' || string_agg(datname, ', ' order by datname) \
				 from pg_database where datname in ('warehouse', 'airflow_meta')"; \
			exit 0; \
		fi; \
		printf '.'; \
		sleep 1; \
	done; \
	printf '\nThe database did not come up in 60 seconds. Look at: $(COMPOSE) logs postgres\n'; \
	exit 1

down: ## Stop the container, keep the data
	$(COMPOSE) down

reset: ## Drop the container and its data - the next up rebuilds it
	$(COMPOSE) down -v

# up is a prerequisite here, and not for symmetry with the other targets.
# Without it the command fails on a stopped container with "can only create
# exec sessions on running containers" - a message about the internals of the
# engine, not about the database not being up. You run into it after the very
# first reboot.
psql: up ## Open psql inside the container, no client needed on the host
	$(ENGINE) exec -it $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME)

# A check before the environment is built. On Debian and Ubuntu - and so in a
# typical WSL - the venv module ships as a separate package, and without it
# python3 -m venv fails with a message about ensurepip that says nothing about
# what to do. Five lines here are cheaper than one such dead end for a reviewer.
$(VENV): requirements.txt
	@python3 -c 'import ensurepip' >/dev/null 2>&1 || { \
		echo 'No venv module for python3.'; \
		echo '  Debian, Ubuntu, WSL:  sudo apt install -y python3-venv'; \
		echo '  Fedora:               sudo dnf install -y python3'; \
		exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip -q install --upgrade pip
	$(VENV)/bin/pip -q install -r requirements.txt
	@touch $(VENV)

venv: $(VENV) ## Build the generator's virtual environment

# The version check comes before the environment is created: dbt requires
# Python 3.10 or newer, and its own message about that drowns in the output of
# the pip resolver.
$(DBT_VENV): requirements-dbt.txt
	@$(PYTHON) -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || { \
		echo 'dbt requires Python 3.10 or newer, and $(PYTHON) is older.'; \
		echo '  Debian, Ubuntu, WSL:  sudo apt install -y python3.12 python3.12-venv'; \
		echo '  Fedora:               sudo dnf install -y python3.12'; \
		echo 'Then call with an explicit interpreter: make venv-dbt PYTHON=python3.12'; \
		exit 1; }
	$(PYTHON) -m venv $(DBT_VENV)
	$(DBT_VENV)/bin/pip -q install --upgrade pip
	$(DBT_VENV)/bin/pip -q install -r requirements-dbt.txt
	@touch $(DBT_VENV)

venv-dbt: $(DBT_VENV) ## Build the dbt virtual environment

# The official Airflow compose file is deliberately not used: eight services
# and over two hundred lines. Here Airflow is installed locally, and its
# metadata database lives in a second database inside the same container as the
# warehouse.
$(AIRFLOW_VENV): requirements-airflow.txt
	@test -n "$(AIRFLOW_VERSION)" || { \
		echo 'Could not read the Airflow version from requirements-airflow.txt.'; \
		exit 1; }
	$(PYTHON) -m venv $(AIRFLOW_VENV)
	$(AIRFLOW_VENV)/bin/pip -q install --upgrade pip
	@echo 'Installing Airflow $(AIRFLOW_VERSION) against constraints for Python $(AIRFLOW_PY_TAG)'
	$(AIRFLOW_VENV)/bin/pip -q install -r requirements-airflow.txt \
		--constraint "$(AIRFLOW_CONSTRAINTS)"
	@touch $(AIRFLOW_VENV)

venv-airflow: $(AIRFLOW_VENV) ## Build the Airflow virtual environment

$(AIRFLOW_DB_STAMP): $(AIRFLOW_VENV)
	$(AIRFLOW) db migrate
	@touch $@

airflow-init: up $(AIRFLOW_DB_STAMP) ## Create the Airflow metadata database

# PATH here is not for convenience. standalone starts the scheduler, the web
# server, the DAG file processor and the triggerer as subprocesses and calls
# them by the short name `airflow`, that is, looks them up on PATH. The Makefile
# calls the binary by its path inside the venv - enough for the process itself
# but not for its children: all four fail with "No such file or directory:
# 'airflow'", and it looks as though Airflow is not installed. PATH is
# deliberately not patched globally: the other targets have environments of
# their own, and the Airflow python must not end up ahead of somebody else's.
airflow: up $(AIRFLOW_DB_STAMP) ## Start the Airflow web UI on localhost:8080
	PATH="$(CURDIR)/$(AIRFLOW_VENV)/bin:$$PATH" $(AIRFLOW) standalone

$(CHECKS_VENV): requirements-checks.txt
	$(PYTHON) -m venv $(CHECKS_VENV)
	$(CHECKS_VENV)/bin/pip -q install --upgrade pip
	$(CHECKS_VENV)/bin/pip -q install -r requirements-checks.txt
	@touch $(CHECKS_VENV)

# An empty raw layer is the quietest failure at this stage: dbt will honestly
# build the marts out of nothing and report success. The check costs one
# command, while working out "why are there zero rows in the mart" costs an
# evening.
models: up $(DBT_VENV) ## Build the marts behind the gate: dbt build
	@rows=$$($(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) \
		-tAc 'select count(*) from raw.orders' 2>/dev/null || echo 0); \
	test "$$rows" -gt 0 2>/dev/null || { \
		echo 'The raw layer is empty - nothing to build. Run make seed first.'; \
		exit 1; }
	$(DBT) build --project-dir $(DBT_DIR) $(DBT_VARS)

# .PHONY on this target is not for tidiness: there is a dbt/ directory next to
# it, and without it make would look at the directory, decide the target is up
# to date, and silently do nothing.
dbt: up $(DBT_VENV) ## Call dbt directly, for example make dbt ARGS=debug
	@test -n "$(ARGS)" || { echo 'Arguments are required, for example: make dbt ARGS=debug'; exit 1; }
	$(DBT) $(ARGS) --project-dir $(DBT_DIR)

# The reconciliation is deliberately not written in SQL and lives outside the
# pipeline: a check made with the same tool would repeat the model's mistake
# without noticing it.
reconcile: up $(CHECKS_VENV) ## Reconcile the marts against the raw layer
	@rows=$$($(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) \
		-tAc 'select count(*) from marts.mart_store_daily_sales' 2>/dev/null || echo 0); \
	test "$$rows" -gt 0 2>/dev/null || { \
		echo 'There are no marts - nothing to reconcile. Run make models first.'; \
		exit 1; }
	$(CHECKS_PY) checks/reconcile.py

# seed brings the database up and builds the environment itself. Otherwise
# "reproduces with one command" would turn into three commands with a footnote.
seed: up $(VENV) ## Generate and load the synthetic data for the whole horizon
	$(PY) -m generator seed

seed-day: up $(VENV) ## Rebuild one partition: make seed-day DATE=2026-03-14
	@test -n "$(DATE)" || { echo 'A date is required: make seed-day DATE=2026-03-14'; exit 1; }
	$(PY) -m generator day $(DATE)

defects: $(VENV) ## Show the map of planted defects
	$(PY) -m generator defects

verify: up $(VENV) ## Table checksums - how repeatability is checked
	$(PY) -m generator verify

measure-returns: up $(VENV) ## Return delay distribution - what the window rests on
	$(PY) -m generator measure-returns

# The targets below were declared from day one on purpose: they set the shape
# of the project and keep it from drifting. Until a stage is done, its target
# fails honestly instead of pretending it did something.

# dbt build, not dbt run plus dbt test: in build the tests are interleaved with
# the models along the graph, and a failing test on a lower layer simply does
# not let the marts be built. That is what "broken data is not published" comes
# down to - with no machinery of its own for candidate schemas and swaps. The
# previous mart stays in place, untouched: yesterday's number is not rewritten,
# rather than rewritten wrongly.
test: up $(DBT_VENV) ## Run the gate tests against the models already built
	$(DBT) test --project-dir $(DBT_DIR) $(DBT_VARS)

# The run goes through airflow dags test: the DAG executes in full and
# synchronously, without a scheduler running. For a project that has to start
# with one command that matters more than a demonstration of daemons:
# dependencies, retries and the schedule are declared in the DAG, and it can be
# run without bringing anything up. The web UI, if you want it, comes up
# separately - make airflow.
run: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Run the pipeline for a date: make run DATE=2026-03-14
	@test -n "$(DATE)" || { echo 'A date is required: make run DATE=2026-03-14'; exit 1; }
	$(AIRFLOW) dags test $(AIRFLOW_DAG) $(DATE)

# The lineage is generated, not maintained by hand: a picture drawn in an
# editor would part ways with the code within the first week.
docs: up $(DBT_VENV) ## dbt lineage and documentation in a browser
	$(DBT) docs generate --project-dir $(DBT_DIR)
	$(DBT) docs serve --project-dir $(DBT_DIR)

# The dictionary is built too, not written. What is written by hand are the
# column descriptions - in the same yml files as their checks. The build fails
# if even one column is left without a description: a dictionary with holes is
# worse than no dictionary, people make decisions on it believing it to be
# complete.
dictionary: up $(DBT_VENV) $(VENV) ## Rebuild DICTIONARY.md from the descriptions in yml
	$(DBT) docs generate --project-dir $(DBT_DIR)
	$(PY) docs/build_dictionary.py

revisions: up ## Show the revision log: what changed, when and why
	@$(ENGINE) exec $(DB_CONTAINER) psql -h 127.0.0.1 -U $(DB_USER) -d $(DB_NAME) -P pager=off -c \
		"select store_id, order_date, revised_at, revenue_net_was, revenue_net_became, \
		        revenue_net_delta, reason \
		 from marts.mart_store_daily_revisions \
		 order by revised_at desc, revenue_net_delta limit 20"

# The scenarios call the same commands a human would call rather than reaching
# into the database behind the pipeline's back: otherwise the demo would prove
# that the demo works.
scenario-late-return: up models $(AIRFLOW_DB_STAMP) ## Scenario: yesterday's number moved, and you can see why
	$(PY) -m scenarios late-return

scenario-missing-partition: up models $(AIRFLOW_DB_STAMP) ## Scenario: source did not arrive, the chain stops
	$(PY) -m scenarios missing-partition

scenario-broken-counter: up models $(AIRFLOW_DB_STAMP) ## Scenario: the device lies, the network still counts
	$(PY) -m scenarios broken-counter

# The dates are computed by python rather than by date -d: the GNU syntax for
# date arithmetic is not available everywhere, and an interpreter is required
# here anyway.
backfill: up $(VENV) $(DBT_VENV) $(AIRFLOW_DB_STAMP) ## Reprocess a period: make backfill FROM=2026-03-01 TO=2026-03-07
	@test -n "$(FROM)" -a -n "$(TO)" || { \
		echo 'Bounds are required: make backfill FROM=2026-03-01 TO=2026-03-07'; exit 1; }
	@for day in $$($(PY) -c "import datetime as dt; \
a = dt.date.fromisoformat('$(FROM)'); b = dt.date.fromisoformat('$(TO)'); \
print(' '.join(str(a + dt.timedelta(days=i)) for i in range((b - a).days + 1)))"); do \
		echo; echo "=== $$day ==="; \
		$(AIRFLOW) dags test $(AIRFLOW_DAG) $$day || exit 1; \
	done
