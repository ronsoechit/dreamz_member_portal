import copy
import ipaddress
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit


SMOKE_DATABASE_URL = os.getenv("DREAMZ_POSTGRES_SMOKE_URL", "").strip()
if not SMOKE_DATABASE_URL:
    raise unittest.SkipTest("Disposable PostgreSQL workout smoke database is not configured")
SMOKE_CLUSTER_NAME = os.getenv("DREAMZ_POSTGRES_SMOKE_CLUSTER", "").strip()
if not SMOKE_CLUSTER_NAME.startswith("dreamz_smoke_"):
    raise RuntimeError(
        "DREAMZ_POSTGRES_SMOKE_CLUSTER must identify the disposable local "
        "PostgreSQL cluster with a dreamz_smoke_* marker"
    )

parsed_database_url = urlsplit(SMOKE_DATABASE_URL)
database_name = unquote(parsed_database_url.path.lstrip("/"))
if (
    parsed_database_url.scheme not in {"postgresql", "postgres"}
    or parsed_database_url.hostname not in {"127.0.0.1", "localhost"}
    or not database_name.startswith("dreamz_smoke")
    or "/" in database_name
    or parsed_database_url.query
    or parsed_database_url.fragment
):
    raise RuntimeError(
        "DREAMZ_POSTGRES_SMOKE_URL must be a query-free URL for a disposable "
        "local dreamz_smoke* database"
    )

if "dreamz_portal" in sys.modules:
    raise RuntimeError(
        "Run the PostgreSQL smoke module in a dedicated Python process so the "
        "portal engine cannot be inherited from another test module"
    )

os.environ["DATABASE_URL"] = SMOKE_DATABASE_URL
os.environ.setdefault("SECRET_KEY", "postgres-smoke-secret")
os.environ.setdefault("COACH_AI_MODE", "fallback")
os.environ.setdefault("EMAIL_DELIVERY_MODE", "log")
os.environ.setdefault("RUNTIME_SCHEMA_STRICT", "true")

import dreamz_portal as portal  # noqa: E402
from sqlalchemy import Integer, text  # noqa: E402
from dreamz_portal import (  # noqa: E402
    COACH_PLAN_SCHEMA_VERSION,
    CoachActiveWorkout,
    CoachActiveWorkoutExercise,
    CoachActiveWorkoutSet,
    CoachPlan,
    CoachProfile,
    CoachWorkoutExerciseLog,
    CoachWorkoutSession,
    Member,
    app,
    claim_active_workout_revision,
    coach_plan_for_member,
    db,
    ensure_runtime_schema,
)


def assert_disposable_postgres_target():
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            raise RuntimeError(
                "PostgreSQL smoke refused to run because the portal engine is not PostgreSQL"
            )
        with db.engine.connect() as connection:
            connected_database, server_address, cluster_name = connection.execute(text(
                "SELECT current_database(), inet_server_addr()::text, "
                "current_setting('cluster_name')"
            )).one()

    if connected_database != database_name:
        raise RuntimeError(
            "PostgreSQL smoke connected to an unexpected database: "
            f"{connected_database!r}"
        )
    try:
        server_ip = ipaddress.ip_interface(server_address).ip
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"PostgreSQL smoke connected to an invalid server address: {server_address!r}"
        ) from exc
    if not (server_ip.is_loopback or server_ip.is_private):
        raise RuntimeError(
            f"PostgreSQL smoke refused public server address: {server_address!r}"
        )
    if cluster_name != SMOKE_CLUSTER_NAME:
        raise RuntimeError(
            "PostgreSQL smoke connected to a server without the expected disposable "
            f"cluster marker: {cluster_name!r}"
        )


assert_disposable_postgres_target()


class PostgresActiveWorkoutSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(
            TESTING=False,
            COACH_AI_MODE="fallback",
            EMAIL_DELIVERY_MODE="log",
        )
        assert_disposable_postgres_target()
        with app.app_context():
            existing_tables = portal.inspect(db.engine).get_table_names()
        if existing_tables:
            raise RuntimeError(
                "PostgreSQL smoke requires a freshly provisioned empty database; "
                f"found tables: {', '.join(sorted(existing_tables))}"
            )

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def ensure_schema_ready(self):
        with app.app_context():
            ensure_runtime_schema()

    def unique_member_id(self, prefix):
        return f"{prefix}{uuid.uuid4().hex[:10]}"

    def logged_in_client(self, member_id, csrf_token):
        client = app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["member_id"] = member_id
            browser_session["_csrf_token"] = csrf_token
            browser_session["language"] = "en"
        return client

    def wait_for_blocked_queries(self, *, query_fragments, expected_count=2, timeout=30):
        deadline = time.monotonic() + timeout
        observed_rows = []
        with app.app_context():
            with db.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                while time.monotonic() < deadline:
                    observed_rows = connection.execute(text(
                        """
                        SELECT pid, state, wait_event_type, wait_event, query
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                        """
                    )).mappings().all()
                    matching_rows = [
                        row
                        for row in observed_rows
                        if row["wait_event_type"] == "Lock"
                        and all(
                            fragment.lower() in (row["query"] or "").lower()
                            for fragment in query_fragments
                        )
                    ]
                    if len(matching_rows) >= expected_count:
                        return
                    time.sleep(0.05)
        self.fail(
            "Timed out waiting for concurrent PostgreSQL statements blocked on "
            f"{query_fragments!r}; last activity={observed_rows!r}"
        )

    def test_01_parallel_runtime_schema_bootstrap(self):
        worker_code = """
import ipaddress
import sys
import time
from pathlib import Path

from sqlalchemy import text
from dreamz_portal import app, db, ensure_runtime_schema

ready_path = Path(sys.argv[1])
release_path = Path(sys.argv[2])
expected_database = sys.argv[3]
expected_cluster = sys.argv[4]

with app.app_context():
    if db.engine.dialect.name != "postgresql":
        raise RuntimeError("Runtime-schema worker did not bind PostgreSQL")
    with db.engine.connect() as connection:
        connected_database, server_address, cluster_name = connection.execute(
            text(
                "SELECT current_database(), inet_server_addr()::text, "
                "current_setting('cluster_name')"
            )
        ).one()
    if connected_database != expected_database:
        raise RuntimeError(f"Unexpected worker database: {connected_database!r}")
    server_ip = ipaddress.ip_interface(server_address).ip
    if not (server_ip.is_loopback or server_ip.is_private):
        raise RuntimeError(f"Worker refused public address: {server_address!r}")
    if cluster_name != expected_cluster:
        raise RuntimeError(f"Unexpected worker cluster marker: {cluster_name!r}")

    ready_path.touch()
    deadline = time.monotonic() + 30
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Runtime-schema worker start barrier timed out")
        time.sleep(0.02)

    app.config["_RUNTIME_SCHEMA_READY"] = False
    ensure_runtime_schema()
"""
        worker_environment = os.environ.copy()
        worker_environment["DATABASE_URL"] = SMOKE_DATABASE_URL
        worker_environment["DREAMZ_POSTGRES_SMOKE_URL"] = SMOKE_DATABASE_URL
        worker_environment["DREAMZ_POSTGRES_SMOKE_CLUSTER"] = SMOKE_CLUSTER_NAME
        worker_environment["RUNTIME_SCHEMA_STRICT"] = "true"
        project_root = Path(__file__).resolve().parents[1]
        workers = []
        results = []
        with tempfile.TemporaryDirectory(prefix="dreamz-pg-bootstrap-") as barrier_directory:
            barrier_root = Path(barrier_directory)
            release_path = barrier_root / "release"
            ready_paths = [barrier_root / f"ready-{index}" for index in range(2)]
            try:
                workers = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            worker_code,
                            str(ready_path),
                            str(release_path),
                            database_name,
                            SMOKE_CLUSTER_NAME,
                        ],
                        cwd=project_root,
                        env=worker_environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for ready_path in ready_paths
                ]

                deadline = time.monotonic() + 30
                while not all(ready_path.exists() for ready_path in ready_paths):
                    failed_workers = [
                        worker for worker in workers if worker.poll() not in (None, 0)
                    ]
                    if failed_workers:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Runtime-schema subprocesses did not reach the start barrier"
                        )
                    time.sleep(0.02)
                release_path.touch()

                for worker in workers:
                    stdout, stderr = worker.communicate(timeout=180)
                    results.append((worker.returncode, stdout, stderr))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.terminate()
                    try:
                        worker.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.communicate(timeout=5)

        self.assertEqual(
            [result[0] for result in results],
            [0, 0],
            "\n".join(result[2] for result in results),
        )

    def test_02_active_workout_schema_contract(self):
        self.ensure_schema_ready()
        with app.app_context():
            inspector = portal.inspect(db.engine)
            required_tables = {
                "coach_active_workout",
                "coach_active_workout_exercise",
                "coach_active_workout_set",
            }
            self.assertTrue(required_tables.issubset(set(inspector.get_table_names())))

            elapsed_column = next(
                column
                for column in inspector.get_columns("coach_active_workout")
                if column["name"] == "elapsed_seconds"
            )
            self.assertFalse(elapsed_column["nullable"])
            self.assertTrue(portal.column_default_is_zero(elapsed_column["default"]))
            self.assertIsInstance(elapsed_column["type"], Integer)

            workout_unique_constraints = {
                constraint["name"]: tuple(constraint.get("column_names", []))
                for constraint in inspector.get_unique_constraints("coach_active_workout")
            }
            self.assertEqual(
                workout_unique_constraints.get("uq_coach_active_workout_member_request"),
                ("member_id", "start_request_id"),
            )
            unique_index_columns = {
                tuple(index.get("column_names", []))
                for index in inspector.get_indexes("coach_active_workout")
                if index.get("unique")
            }
            self.assertIn(("public_id",), unique_index_columns)
            self.assertIn(("active_member_id",), unique_index_columns)
            self.assertIn(("completed_session_id",), unique_index_columns)

            exercise_unique_constraints = {
                constraint["name"]: tuple(constraint.get("column_names", []))
                for constraint in inspector.get_unique_constraints("coach_active_workout_exercise")
            }
            set_unique_constraints = {
                constraint["name"]: tuple(constraint.get("column_names", []))
                for constraint in inspector.get_unique_constraints("coach_active_workout_set")
            }
            self.assertEqual(
                exercise_unique_constraints.get("uq_coach_active_workout_exercise_order"),
                ("active_workout_id", "exercise_order"),
            )
            self.assertEqual(
                set_unique_constraints.get("uq_coach_active_workout_set_number"),
                ("active_exercise_id", "set_number"),
            )

            expected_index_columns = {
                "coach_active_workout": {
                    ("public_id",),
                    ("member_id",),
                    ("active_member_id",),
                    ("status",),
                    ("completed_session_id",),
                    ("updated_at",),
                },
                "coach_active_workout_exercise": {
                    ("active_workout_id",),
                    ("member_id",),
                },
                "coach_active_workout_set": {
                    ("active_exercise_id",),
                    ("completed_exercise_log_id",),
                    ("member_id",),
                },
            }
            for table_name, expected_columns in expected_index_columns.items():
                actual_columns = {
                    tuple(index.get("column_names", []))
                    for index in inspector.get_indexes(table_name)
                }
                self.assertTrue(
                    expected_columns.issubset(actual_columns),
                    f"Missing {table_name} indexes: {expected_columns - actual_columns}",
                )

            expected_foreign_keys = {
                "coach_active_workout": {
                    (
                        ("completed_session_id",),
                        "coach_workout_session",
                        ("id",),
                    ),
                },
                "coach_active_workout_exercise": {
                    (
                        ("active_workout_id",),
                        "coach_active_workout",
                        ("id",),
                    ),
                },
                "coach_active_workout_set": {
                    (
                        ("active_exercise_id",),
                        "coach_active_workout_exercise",
                        ("id",),
                    ),
                    (
                        ("completed_exercise_log_id",),
                        "coach_workout_exercise_log",
                        ("id",),
                    ),
                },
            }
            for table_name, expected_keys in expected_foreign_keys.items():
                actual_keys = {
                    (
                        tuple(key.get("constrained_columns", [])),
                        key.get("referred_table"),
                        tuple(key.get("referred_columns", [])),
                    )
                    for key in inspector.get_foreign_keys(table_name)
                }
                self.assertTrue(
                    expected_keys.issubset(actual_keys),
                    f"Missing {table_name} foreign keys: {expected_keys - actual_keys}",
                )

    def test_03_concurrent_fresh_plan_generation_reuses_the_winner(self):
        self.ensure_schema_ready()
        member_id = self.unique_member_id("PLAN")
        with app.app_context():
            member = Member(
                member_id=member_id,
                name="Postgres Plan Smoke",
                birthdate=date(1990, 1, 1),
                plan_type="no contract 1 month",
                is_active=True,
            )
            profile = CoachProfile(
                member_id=member_id,
                sex="male",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=2,
                session_minutes=45,
                training_place="dreamz_gym",
                height_cm=180,
                weight_kg=82,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="none",
                allergies="none",
            )
            db.session.add_all([member, profile])
            db.session.commit()

        barrier = threading.Barrier(2)
        original_generate = portal.generate_openai_coach_plan

        def synchronized_fallback(_member, _profile, fallback_plan, **_kwargs):
            tagged_plan = copy.deepcopy(fallback_plan)
            tagged_plan[0]["smoke_tag"] = uuid.uuid4().hex
            barrier.wait(timeout=30)
            return tagged_plan, f"smoke-context-{tagged_plan[0]['smoke_tag']}"

        def generate_plan():
            with app.app_context():
                member = Member.query.filter_by(member_id=member_id).one()
                profile = CoachProfile.query.filter_by(member_id=member_id).one()
                return coach_plan_for_member(member, profile, language="en")

        portal.generate_openai_coach_plan = synchronized_fallback
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                plans = list(executor.map(lambda _: generate_plan(), range(2)))
        finally:
            portal.generate_openai_coach_plan = original_generate

        self.assertTrue(all(plans))
        with app.app_context():
            records = CoachPlan.query.filter_by(member_id=member_id).all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].plan_version, COACH_PLAN_SCHEMA_VERSION)
            winning_plan = portal.stored_coach_plan(records[0])
        self.assertEqual(plans[0], winning_plan)
        self.assertEqual(plans[1], winning_plan)

    def test_04_revision_claim_is_atomic(self):
        self.ensure_schema_ready()
        member_id = self.unique_member_id("CAS")
        public_id = uuid.uuid4().hex
        with app.app_context():
            workout = CoachActiveWorkout(
                public_id=public_id,
                member_id=member_id,
                active_member_id=member_id,
                start_request_id=uuid.uuid4().hex,
                status="active",
                revision=0,
            )
            db.session.add(workout)
            db.session.commit()

        barrier = threading.Barrier(2)

        def claim():
            with app.app_context():
                workout = CoachActiveWorkout.query.filter_by(public_id=public_id).one()
                barrier.wait(timeout=30)
                claimed = claim_active_workout_revision(
                    workout,
                    0,
                    status="completed",
                )
                if claimed:
                    db.session.commit()
                return claimed

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))

        self.assertEqual(sorted(claims), [False, True])
        with app.app_context():
            workout = CoachActiveWorkout.query.filter_by(public_id=public_id).one()
            self.assertEqual(workout.status, "completed")
            self.assertEqual(workout.revision, 1)

    def test_05_elapsed_updates_are_monotonic_without_revision_changes(self):
        self.ensure_schema_ready()
        member_id = self.unique_member_id("TIME")
        public_id = uuid.uuid4().hex
        csrf_token = uuid.uuid4().hex
        with app.app_context():
            db.session.add(Member(member_id=member_id, name="Postgres Timer Smoke"))
            db.session.add(CoachActiveWorkout(
                public_id=public_id,
                member_id=member_id,
                active_member_id=member_id,
                start_request_id=uuid.uuid4().hex,
                status="active",
                revision=0,
                elapsed_seconds=0,
            ))
            db.session.commit()

        barrier = threading.Barrier(2)

        def update_elapsed(value):
            client = self.logged_in_client(member_id, csrf_token)
            barrier.wait(timeout=30)
            response = client.post(
                f"/coach/workout-session/{public_id}/elapsed",
                json={"elapsedSeconds": value},
                headers={"X-CSRF-Token": csrf_token},
            )
            return response.status_code

        with app.app_context():
            blocker = db.engine.connect()
            blocker_transaction = blocker.begin()
            blocker.execute(
                text(
                    "SELECT id FROM coach_active_workout "
                    "WHERE public_id = :public_id FOR UPDATE"
                ),
                {"public_id": public_id},
            )

        executor = ThreadPoolExecutor(max_workers=2)
        futures = []
        try:
            futures = [
                executor.submit(update_elapsed, value)
                for value in (120, 300)
            ]
            self.wait_for_blocked_queries(
                query_fragments=("update coach_active_workout",),
            )
            blocker_transaction.commit()
            statuses = [future.result(timeout=60) for future in futures]
        finally:
            if blocker_transaction.is_active:
                blocker_transaction.rollback()
            blocker.close()
            executor.shutdown(wait=True, cancel_futures=True)

        self.assertEqual(statuses, [200, 200])
        with app.app_context():
            workout = CoachActiveWorkout.query.filter_by(public_id=public_id).one()
            self.assertEqual(workout.elapsed_seconds, 300)
            self.assertEqual(workout.revision, 0)

    def test_06_double_finish_materializes_history_once(self):
        self.ensure_schema_ready()
        member_id = self.unique_member_id("DONE")
        public_id = uuid.uuid4().hex
        csrf_token = uuid.uuid4().hex
        finish_request_id = uuid.uuid4().hex
        with app.app_context():
            db.session.add(Member(
                member_id=member_id,
                name="Postgres Finish Smoke",
                birthdate=date(1990, 1, 1),
            ))
            workout = CoachActiveWorkout(
                public_id=public_id,
                member_id=member_id,
                active_member_id=member_id,
                start_request_id=uuid.uuid4().hex,
                session_number=1,
                focus="Postgres concurrency",
                planned_minutes=30,
                language="en",
                status="active",
                revision=0,
                elapsed_seconds=60,
            )
            db.session.add(workout)
            db.session.flush()
            exercise = CoachActiveWorkoutExercise(
                active_workout_id=workout.id,
                member_id=member_id,
                exercise_order=1,
                exercise_name="Smoke press",
                planned_sets="1",
                planned_set_count=1,
                planned_reps="10",
                tracking_mode="reps",
                completion_status="completed",
                completed=True,
            )
            db.session.add(exercise)
            db.session.flush()
            db.session.add(CoachActiveWorkoutSet(
                active_exercise_id=exercise.id,
                member_id=member_id,
                set_number=1,
                reps_completed=10,
                weight_kg=25,
                rpe=7,
                completed=True,
            ))
            db.session.commit()

        barrier = threading.Barrier(2)

        def finish():
            client = self.logged_in_client(member_id, csrf_token)
            barrier.wait(timeout=30)
            response = client.post(
                f"/coach/workout-session/{public_id}/finish",
                json={
                    "requestId": finish_request_id,
                    "revision": 0,
                    "elapsedSeconds": 90,
                },
                headers={"X-CSRF-Token": csrf_token},
            )
            return response.status_code, response.get_json()

        with app.app_context():
            blocker = db.engine.connect()
            blocker_transaction = blocker.begin()
            blocker.execute(
                text(
                    "SELECT id FROM coach_active_workout "
                    "WHERE public_id = :public_id FOR UPDATE"
                ),
                {"public_id": public_id},
            )

        executor = ThreadPoolExecutor(max_workers=2)
        futures = []
        try:
            futures = [executor.submit(finish) for _ in range(2)]
            self.wait_for_blocked_queries(
                query_fragments=("select coach_active_workout",),
            )
            blocker_transaction.commit()
            responses = [future.result(timeout=60) for future in futures]
        finally:
            if blocker_transaction.is_active:
                blocker_transaction.rollback()
            blocker.close()
            executor.shutdown(wait=True, cancel_futures=True)

        self.assertEqual([response[0] for response in responses], [200, 200])
        self.assertEqual(
            {response[1]["workoutId"] for response in responses},
            {responses[0][1]["workoutId"]},
        )
        with app.app_context():
            self.assertEqual(
                CoachWorkoutSession.query.filter_by(member_id=member_id).count(),
                1,
            )
            self.assertEqual(
                CoachWorkoutExerciseLog.query.filter_by(member_id=member_id).count(),
                1,
            )
            workout = CoachActiveWorkout.query.filter_by(public_id=public_id).one()
            self.assertEqual(workout.status, "completed")
            self.assertEqual(workout.revision, 1)


if __name__ == "__main__":
    unittest.main()
