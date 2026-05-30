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

    def test_sync_api_keeps_document_ids_stable_when_documents_are_unchanged(self):
        payload = {
            "source": "unit-test",
            "members": [{"member_id": "1206", "name": "Example, Member"}],
            "documents": {
                "1206": [
                    {
                        "document_type": "signup_form",
                        "title": "Signup Form",
                        "path": "s3://bucket/signup.pdf",
                        "source_filename": "signup.pdf",
                    }
                ]
            },
        }
        headers = {"X-Sync-Token": "sync-test-token"}

        first_response = self.client.post("/api/sync/members", json=payload, headers=headers)
        first_document_id = MemberDocument.query.filter_by(member_id="1206").one().id
        second_response = self.client.post("/api/sync/members", json=payload, headers=headers)
        second_document = MemberDocument.query.filter_by(member_id="1206").one()
        second_summary = json.loads(SyncRun.query.order_by(SyncRun.id.desc()).first().change_summary)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_document.id, first_document_id)
        self.assertEqual(second_summary["document_changes"], [])

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

    def test_sync_member_ids_requires_token(self):
        response = self.client.get("/api/sync/member-ids")

        self.assertEqual(response.status_code, 403)

    def test_sync_member_ids_returns_existing_member_ids(self):
        db.session.add(Member(member_id="1206", name="Example, Member"))
        db.session.add(Member(member_id="34838", name="Live, Member"))
        db.session.commit()

        response = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json["member_ids"]), {"1206", "34838"})

    def test_sync_missing_file_keys_reports_missing_storage_objects(self):
        db.session.add(Member(member_id="1206", name="Example, Member", photo_path="s3://bucket/gymassistant/photos/1206.jpg"))
        db.session.add(MemberDocument(
            member_id="1206",
            document_type="contract",
            title="Contract",
            path="s3://bucket/gymassistant/docs/contract.pdf",
            source_filename="contract.pdf",
        ))
        db.session.commit()

        def fake_exists(uri, client=None):
            return uri.endswith("photos/1206.jpg")

        with patch("dreamz_portal.s3_object_exists", side_effect=fake_exists):
            response = self.client.get(
                "/api/sync/missing-file-keys",
                headers={"X-Sync-Token": "sync-test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["missing_keys"], ["gymassistant/docs/contract.pdf"])
        self.assertEqual(response.json["checked"], 2)

    def test_member_document_file_returns_404_when_storage_object_is_missing(self):
        db.session.add(Member(member_id="1206", name="Example, Member"))
        db.session.add(MemberDocument(
            member_id="1206",
            document_type="signup_form",
            title="Signup Form",
            path="s3://bucket/missing.pdf",
            source_filename="missing.pdf",
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
        document = MemberDocument.query.one()

        with patch("dreamz_portal.open_s3_object", side_effect=FileNotFoundError("missing")):
            response = self.client.get(f"/documents/item/{document.id}/file")

        self.assertEqual(response.status_code, 404)
        self.assertIn("Stored file not found or unavailable", response.get_data(as_text=True))

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

    def test_sync_api_ignores_invalid_gymassistant_sentinel_values(self):
        db.session.add(Member(
            member_id="15733",
            name="Cecilia, Sebastian",
            plan_type="KMAR medewerker 2018",
            billing_status="ACTIVE",
            is_active=True,
        ))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={
                "members": [
                    {
                        "member_id": "15733",
                        "name": "Cecilia, Sebastian",
                        "plan_type": "<INVALID>",
                        "billing_status": "ACTIVE",
                        "is_active": True,
                    }
                ]
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_updated"], 0)
        member = Member.query.filter_by(member_id="15733").one()
        self.assertEqual(member.plan_type, "KMAR medewerker 2018")
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["changed_members"], [])
        self.assertIn("Ignored 1 invalid GymAssistant sentinel", SyncRun.query.one().error)

    def test_staff_daily_changes_hides_legacy_invalid_membership_changes(self):
        db.session.add(Member(member_id="15733", name="Cecilia, Sebastian", plan_type="KMAR medewerker 2018"))
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 5, 28, 5, 36),
            completed_at=datetime(2026, 5, 28, 5, 36),
            members_received=1,
            members_updated=1,
            change_summary=json.dumps({
                "new_members": [],
                "changed_members": [
                    {
                        "member_id": "15733",
                        "name": "Cecilia, Sebastian",
                        "plan_type": "<INVALID>",
                        "changes": [
                            {"field": "plan_type", "label": "Plan", "old": "KMAR medewerker 2018", "new": "<INVALID>"}
                        ],
                    }
                ],
                "document_changes": [],
            }),
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/changes?date=2026-05-28")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("<INVALID>", body)
        self.assertNotIn("Cecilia, Sebastian", body)
        self.assertIn("No tracked GymAssistant changes", body)

    def test_sync_api_ignores_blank_values_over_existing_member_data(self):
        db.session.add(Member(
            member_id="34878",
            name="Acosta, Rocila",
            email="rocilacosta@gmail.com",
            mobile="297-592-2601",
            plan_type="1 WEEK PASS",
        ))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={
                "members": [
                    {
                        "member_id": "34878",
                        "name": "Acosta, Rocila",
                        "email": "",
                        "mobile": "",
                        "plan_type": "1 WEEK PASS",
                    }
                ]
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_updated"], 0)
        member = Member.query.filter_by(member_id="34878").one()
        self.assertEqual(member.email, "rocilacosta@gmail.com")
        self.assertEqual(member.mobile, "297-592-2601")
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["changed_members"], [])
        self.assertIn("Ignored 2 blank GymAssistant value", SyncRun.query.one().error)

    def test_sync_api_imports_birthdate_and_protects_app_birthdate_from_blank_backup(self):
        db.session.add(Member(
            member_id="13659",
            name="Ron Soechit",
            birthdate=date(1990, 1, 1),
        ))
        db.session.add(Member(member_id="24680", name="Zahira Test"))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={
                "members": [
                    {
                        "member_id": "13659",
                        "name": "Ron Soechit",
                        "birthdate": None,
                    },
                    {
                        "member_id": "24680",
                        "name": "Zahira Test",
                        "birthdate": "1992-04-15",
                    },
                ]
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        ron = Member.query.filter_by(member_id="13659").one()
        zahira = Member.query.filter_by(member_id="24680").one()
        self.assertEqual(ron.birthdate, date(1990, 1, 1))
        self.assertEqual(zahira.birthdate, date(1992, 4, 15))
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(len(summary["changed_members"]), 1)
        self.assertEqual(summary["changed_members"][0]["member_id"], "24680")
        self.assertEqual(summary["changed_members"][0]["changes"][0]["field"], "birthdate")
        self.assertIn("Ignored 1 blank GymAssistant value", SyncRun.query.one().error)

    def test_staff_daily_changes_hides_legacy_blank_contact_changes(self):
        db.session.add(Member(member_id="34878", name="Acosta, Rocila"))
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 5, 28, 5, 51),
            completed_at=datetime(2026, 5, 28, 5, 51),
            members_received=1,
            members_updated=1,
            change_summary=json.dumps({
                "new_members": [],
                "changed_members": [
                    {
                        "member_id": "34878",
                        "name": "Acosta, Rocila",
                        "plan_type": "1 WEEK PASS",
                        "changes": [
                            {"field": "email", "label": "Email", "old": "rocilacosta@gmail.com", "new": ""},
                            {"field": "mobile", "label": "Mobile", "old": "297-592-2601", "new": ""},
                        ],
                    }
                ],
                "document_changes": [],
            }),
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/changes?date=2026-05-28")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("Acosta, Rocila", body)
        self.assertNotIn("rocilacosta@gmail.com", body)
        self.assertNotIn("297-592-2601", body)
        self.assertIn("No tracked GymAssistant changes", body)

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
        self.assertIn("sync-status-panel", body)
        self.assertIn("Auto-refreshing every 20 seconds.", body)
        self.assertIn("setInterval(refreshSyncStatus, 20000)", body)
        self.assertIn("Showing sync runs 1-1 of 1", body)

    def test_staff_sync_status_paginates_runs_newest_first(self):
        base_time = datetime(2026, 5, 25, 18, 0)
        for index in range(25):
            db.session.add(SyncRun(
                source=f"run-{index:02d}",
                status="success",
                started_at=base_time + timedelta(minutes=index),
                completed_at=base_time + timedelta(minutes=index),
                members_received=index,
                members_updated=index,
                documents_received=index,
            ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        first_page = self.client.get("/staff/sync")
        second_page = self.client.get("/staff/sync?page=2")

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        first_body = first_page.get_data(as_text=True)
        second_body = second_page.get_data(as_text=True)
        self.assertIn("Showing sync runs 1-20 of 25", first_body)
        self.assertIn("Page 1 of 2", first_body)
        self.assertIn("run-24", first_body)
        self.assertIn("run-05", first_body)
        self.assertNotIn("run-04", first_body)
        self.assertIn("href=\"/staff/sync?page=2\"", first_body)
        self.assertIn("Showing sync runs 21-25 of 25", second_body)
        self.assertIn("Page 2 of 2", second_body)
        self.assertIn("run-04", second_body)
        self.assertNotIn("run-24", second_body)

    def test_staff_sync_status_warns_when_latest_sync_is_stale(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime.now() - timedelta(minutes=46),
            completed_at=datetime.now() - timedelta(minutes=45),
            members_received=2,
            documents_received=3,
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/sync")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Gym Assistant sync is not checking in", body)
        self.assertIn("No recent sync", body)
        self.assertIn("Last received:", body)

    def test_staff_sync_status_shows_live_connection_for_recent_sync(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime.now() - timedelta(minutes=6),
            completed_at=datetime.now() - timedelta(minutes=5),
            members_received=2,
            documents_received=3,
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/sync")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Connection", body)
        self.assertIn("Live", body)
        self.assertNotIn("Gym Assistant sync is not checking in", body)

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
