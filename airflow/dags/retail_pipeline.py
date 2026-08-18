"""The daily retail funnel run.

The chain is exactly the one from the blueprint: fetch the raw data for a date,
check source freshness, build the marts behind the gate, announce what was
published. It stops in two places — on freshness, and on the tests inside the
build.

The tasks call the tools as subprocesses rather than importing them. That is
not laziness: dbt, the generator and Airflow itself have incompatible pins, and
each lives in its own environment. The interface between them is an exit code,
and that does not break because somebody upgraded a library.

The run date is handed to the tools explicitly, through an environment
variable, rather than read by them off the wall clock. Everything rests on it:
freshness and the reprocessing window alike. A run for a past day must produce
exactly what it would have produced then — otherwise it is not reprocessing but
a new history.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

# The project root comes from the location of this file, not from an
# environment variable: the DAG lives inside the repository, and one more
# setting here would be one more place to get it wrong.
PROJECT_DIR = Path(__file__).resolve().parents[2]

GENERATOR_PY = PROJECT_DIR / ".venv" / "bin" / "python"
DBT = PROJECT_DIR / ".venv-dbt" / "bin" / "dbt"
DBT_DIR = PROJECT_DIR / "dbt"

# The size of the reprocessing window is deliberately NOT in this file.
#
# It lives in the vars of dbt/dbt_project.yml, next to the numbers that justify
# it, and the DAG neither passes it nor overrides it. The first version of this
# file kept a constant of its own and fed it to --vars — and so overrode the
# single source of truth: editing dbt_project.yml would have changed what make
# models does, but not what make run does. Exactly the silent divergence this
# project defends against.
#
# The only thing the DAG has to tell the tools is the run date. Everything else
# they take from their own configs, which live in the repository.
ENV = {
    "RUN_DATE": "{{ ds }}",
    "DBT_PROFILES_DIR": str(DBT_DIR),
}


def alert_on_failure(context) -> None:
    """Alert on failure.

    Writes a line into airflow/alerts.log and into the task log. In production
    this is where email or a messenger goes, but the interface is the same — a
    callback function, and only it would change. Requiring SMTP in a project
    that promises to start with one command would mean demanding setup exactly
    where none was promised.
    """
    ti = context["task_instance"]
    line = "%s  FAILED  dag=%s task=%s date=%s attempt=%s" % (
        pendulum.now("UTC").to_iso8601_string(),
        ti.dag_id,
        ti.task_id,
        context.get("ds"),
        ti.try_number,
    )
    print(line)
    alerts = PROJECT_DIR / "airflow" / "alerts.log"
    alerts.parent.mkdir(parents=True, exist_ok=True)
    with alerts.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


with DAG(
    dag_id="retail_pipeline",
    description="Daily revenue and conversion per store",
    schedule="@daily",
    start_date=pendulum.datetime(2025, 1, 1, tz="UTC"),
    # The synthetic horizon ends on 30 June 2026. Without an end_date the
    # scheduler would try every day to compute a day the source never sent
    # anything for, and would honestly fail on freshness — but a scheduled
    # failure nobody intends to fix is noise, not signal.
    end_date=pendulum.datetime(2026, 6, 30, tz="UTC"),
    catchup=False,
    # The reprocessing windows of neighbouring dates overlap by 28 days. Two
    # runs at once would write the same mart rows, and which of them lands last
    # is a matter of luck. Reprocessing has to be repeatable, so this project
    # allows exactly one run at a time.
    max_active_runs=1,
    default_args={
        # There are no retries by default, and that is deliberate. A retry
        # helps against a transient connection error and against nothing else:
        # a failed data test will not turn green on the second attempt, and
        # three tries only triple the time before a human hears about it.
        # Retries sit precisely where a failure can be temporary.
        "retries": 0,
        "on_failure_callback": alert_on_failure,
    },
    tags=["retail", "dbt"],
    doc_md=__doc__,
) as dag:

    # Landing the raw data for a date. The only task that reaches into an
    # external system — and the only one where a retry makes sense.
    land_partition = BashOperator(
        task_id="land_partition",
        bash_command=f'"{GENERATOR_PY}" -m generator day "$RUN_DATE"',
        cwd=str(PROJECT_DIR),
        env=ENV,
        append_env=True,
        retries=2,
        retry_delay=datetime.timedelta(minutes=1),
        doc_md="Loads the partition for a date with delete-and-insert. A "
               "repeat call leaves the same state, so a retry is safe.",
    )

    # Freshness is checked by a task of its own before the build, even though
    # the same test also runs inside dbt build. The duplication is deliberate:
    # this way the UI shows that the chain stopped on the source rather than
    # somewhere in the middle of the build, and the investigation starts in the
    # right place.
    check_freshness = BashOperator(
        task_id="check_freshness",
        bash_command=(
            f'"{DBT}" test --select assert_source_is_fresh '
            f'--project-dir "{DBT_DIR}" '
            '--vars "{run_date: $RUN_DATE}"'
        ),
        cwd=str(PROJECT_DIR),
        env=ENV,
        append_env=True,
        doc_md="Stop. The partition for the target date must be there and not empty.",
    )

    # dbt build, not dbt run plus dbt test: tests are interleaved with models
    # along the graph, and a failing test on a lower layer does not let the
    # marts be built. The previous mart stays untouched.
    build_marts = BashOperator(
        task_id="build_marts",
        bash_command=(
            f'"{DBT}" build --project-dir "{DBT_DIR}" '
            '--vars "{run_date: $RUN_DATE}"'
        ),
        cwd=str(PROJECT_DIR),
        env=ENV,
        append_env=True,
        doc_md="Builds the marts behind the gate. The sales mart is rebuilt a "
               "window backwards — as far back as the tail of returns reaches; "
               "the window size is set in dbt_project.yml.",
    )

    # Publishing. The mart is already updated — it updated precisely because
    # the tests passed — and the task announces what exactly was published. A
    # line of numbers in the run log is worth more than it looks: later it
    # shows what changed without a trip to the database.
    publish = BashOperator(
        task_id="publish",
        bash_command=f'"{GENERATOR_PY}" airflow/publish.py "$RUN_DATE"',
        cwd=str(PROJECT_DIR),
        env=ENV,
        append_env=True,
        doc_md="What was published for the date: rows, revenue, quality flags.",
    )

    land_partition >> check_freshness >> build_marts >> publish
