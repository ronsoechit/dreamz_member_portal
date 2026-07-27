import importlib.util
import os
import unittest
from datetime import date, datetime


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import Member, SyncRun, app, db  # noqa: E402


class FepSnapshotApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["FEP_API_TOKEN"] = "fep-test-token"
        app.config["_RUNTIME_SCHEMA_READY"] = False
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        app.config["FEP_API_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    def test_snapshot_requires_token(self):
        response = self.client.get("/api/fep/member-snapshot")

        self.assertEqual(response.status_code, 403)

    def test_snapshot_fails_closed_until_complete_member_snapshot_exists(self):
        db.session.add(Member(member_id="34203", name="Historical, Member"))
        db.session.add(
            SyncRun(
                source="unit-test",
                status="success",
                members_received=1,
                members_snapshot_complete=False,
            )
        )
        db.session.commit()

        response = self.client.get(
            "/api/fep/member-snapshot",
            headers={"X-FEP-Token": "fep-test-token"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["presence_authoritative"])
        self.assertEqual(response.json["members"], [])

    def test_snapshot_returns_member_billing_fields(self):
        sync_run = SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 6, 18, 8, 30, 0),
            completed_at=datetime(2026, 6, 18, 8, 31, 0),
            members_received=1,
            members_new=1,
            members_updated=0,
            members_snapshot_complete=True,
            member_unique_count=1,
            member_snapshot_protocol="explicit_v1",
        )
        db.session.add(sync_run)
        db.session.flush()
        db.session.add(
            Member(
                member_id="34203",
                name="Polanco Cornelio, Lisbeth Arleny",
                plan_type="Dreamz 12 months",
                contract_type="12-months",
                billing_status="ACTIVE",
                billing_amount=60.0,
                balance=0.0,
                due_date=date(2026, 6, 28),
                last_payment=date(2026, 5, 28),
                last_payment_amount=61.0,
                is_active=True,
                gym_snapshot_run_id=sync_run.id,
            )
        )
        db.session.commit()

        response = self.client.get(
            "/api/fep/member-snapshot",
            headers={"X-FEP-Token": "fep-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["member_count"], 1)
        self.assertEqual(response.json["latest_sync"]["status"], "success")
        member = response.json["members"][0]
        self.assertEqual(member["member_id"], "34203")
        self.assertEqual(member["billing_amount"], 60.0)
        self.assertEqual(member["balance"], 0.0)
        self.assertEqual(member["due_date"], "2026-06-28")


if __name__ == "__main__":
    unittest.main()
