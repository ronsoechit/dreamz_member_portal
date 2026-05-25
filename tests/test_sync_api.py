from datetime import date, datetime, timedelta
import importlib.util
import json
import os
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import Member, MemberDocument, SyncRun, app, db  # noqa: E402


class SyncApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["SYNC_API_TOKEN"] = "sync-test-token"
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
        app.config["SYNC_API_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    def test_sync_api_requires_token(self):
        response = self.client.post("/api/sync/members", json={"members": []})

        self.assertEqual(response.status_code, 403)

    def test_sync_api_upserts_members_and_documents(self):
        payload = {
            "source": "unit-test",
            "members": [
                {
                    "member_id": "1206",
                    "name": "Example, Member",
                    "email": "member@example.com",
                    "plan_type": "contract Dreamz 12 m",
                    "contract_type": "12-months",
                    "start_date": "2025-06-23",
                    "billing_amount": 55.0,
                }
            ],
            "documents": {
                "1206": [
                    {
                        "document_type": "contract",
                        "title": "Contract",
                        "path": "Data/Attachments/0001206/contract.pdf",
                        "source_filename": "contract.pdf",
                    }
                ]
            },
        }

        response = self.client.post(
            "/api/sync/members",
            json=payload,
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_new"], 1)
        self.assertEqual(response.json["documents_received"], 1)
        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.start_date, date(2025, 6, 23))
        self.assertEqual(member.billing_amount, 55.0)
        document = MemberDocument.query.filter_by(member_id="1206").one()
        self.assertEqual(document.document_type, "contract")
        sync_run = SyncRun.query.one()
        self.assertEqual(sync_run.status, "success")
        summary = json.loads(sync_run.change_summary)
        self.assertEqual(summary["new_members"][0]["member_id"], "1206")
        self.assertEqual(summary["document_changes"][0]["new_count"], 1)

    def test_sync_file_upload_requires_token(self):
        response = self.client.post("/api/sync/files", data=b"file", headers={"X-Storage-Key": "gymassistant/test.pdf"})

        self.assertEqual(response.status_code, 403)

    def test_sync_file_upload_stores_file_and_returns_uri(self):
        with patch("dreamz_portal.upload_bytes_to_s3", return_value="s3://bucket/gymassistant/test.pdf") as upload:
            response = self.client.post(
                "/api/sync/files",
                data=b"%PDF",
                headers={
                    "X-Sync-Token": "sync-test-token",
                    "X-Storage-Key": "gymassistant/test.pdf",
                    "X-Content-Type": "application/pdf",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["uri"], "s3://bucket/gymassistant/test.pdf")
        upload.assert_called_once_with(b"%PDF", "gymassistant/test.pdf", content_type="application/pdf")

    def test_sync_api_updates_existing_member(self):
        db.session.add(Member(member_id="1206", name="Old Name"))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={"members": [{"member_id": "1206", "name": "New Name"}]},
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_updated"], 1)
        self.assertEqual(Member.query.filter_by(member_id="1206").one().name, "New Name")
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["changed_members"][0]["changes"][0]["label"], "Name")
        self.assertEqual(summary["changed_members"][0]["changes"][0]["old"], "Old Name")
        self.assertEqual(summary["changed_members"][0]["changes"][0]["new"], "New Name")

    def test_sync_api_does_not_count_unchanged_member_as_updated(self):
        db.session.add(Member(member_id="1206", name="Same Name", email="same@example.com"))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={"members": [{"member_id": "1206", "name": "Same Name", "email": "same@example.com"}]},
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_updated"], 0)
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["changed_members"], [])

    def test_staff_sync_status_lists_runs(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 5, 25, 18, 5),
            completed_at=datetime(2026, 5, 25, 18, 5),
            members_received=2,
            members_updated=2,
            documents_received=3,
            change_summary=json.dumps({
                "new_members": [
                    {"member_id": "34836", "name": "Week Pass Member", "plan_type": "Weekpas"}
                ],
                "changed_members": [],
                "document_changes": [
                    {"member_id": "34836", "name": "Week Pass Member", "old_count": 0, "new_count": 1}
                ],
            }),
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/sync")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Sync Status", body)
        self.assertIn("unit-test", body)
        self.assertIn("2 received", body)
        self.assertIn("25/05/2026 14:05", body)
        self.assertIn("Week Pass Member", body)
        self.assertIn("0 -> 1 docs", body)

    def test_staff_sync_status_marks_stale_running_runs_interrupted(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="running",
            started_at=datetime.now() - timedelta(minutes=30),
            members_received=5,
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/sync")

        self.assertEqual(response.status_code, 200)
        sync_run = SyncRun.query.one()
        self.assertEqual(sync_run.status, "interrupted")
        self.assertIsNotNone(sync_run.completed_at)
        body = response.get_data(as_text=True)
        self.assertIn("interrupted", body)
        self.assertIn("The next scheduled run can continue normally.", body)


if __name__ == "__main__":
    unittest.main()
