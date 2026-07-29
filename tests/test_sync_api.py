from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import inspect
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import (  # noqa: E402
    FepPaymentProcessCommand,
    FepPaymentUpdate,
    Member,
    MemberDocument,
    SyncRun,
    acquire_fep_payment_command_agent_lock,
    acquire_fep_payment_writer_lock,
    api_sync_fep_payment_process_commands,
    api_sync_fep_payment_update_result,
    app,
    create_fep_payment_process_command,
    db,
    fep_payment_command_agent_lock_key,
    fep_payment_writer_lock_key,
    hold_fep_payment_writer_lock,
    normalize_fep_payment_agent,
    release_expired_fep_payment_claims,
    release_expired_fep_payment_process_commands,
)


class SyncApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["SYNC_API_TOKEN"] = "sync-test-token"
        app.config["FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP"] = None
        app.config["FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE"] = None
        app.config["FEP_PAYMENT_SYNC_TOKEN_RESERVE_8KM7V7D"] = None
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
        app.config["SYNC_API_TOKEN"] = None
        app.config["FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP"] = None
        app.config["FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE"] = None
        app.config["FEP_PAYMENT_SYNC_TOKEN_RESERVE_8KM7V7D"] = None
        app.config["FEP_API_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    def fep_headers(self, token="fep-test-token"):
        return {
            "X-FEP-Token": token,
            "Authorization": f"Bearer {token}",
        }

    def add_direct_debit_member(self, **overrides):
        data = {
            "member_id": "34203",
            "name": "Direct, Debit",
            "billing_status": "ACTIVE",
            "is_active": True,
            "billing_amount": 60.0,
            "balance": 0.0,
            "last_payment": date(2026, 5, 29),
            "last_payment_amount": 60.0,
            "due_date": date(2026, 7, 1),
            "next_payment": date(2026, 7, 1),
        }
        data.update(overrides)
        member = Member(**data)
        db.session.add(member)
        db.session.commit()
        return member

    def add_member_document(self, member_id, document_type, source_filename, path=None, display_order=0):
        document = MemberDocument(
            member_id=str(member_id),
            document_type=document_type,
            title=document_type.replace("_", " ").title(),
            path=path or rf"C:\Gym Assistant 2.6\Data\Attachments\{str(member_id).zfill(7)}\{source_filename}",
            source_filename=source_filename,
            display_order=display_order,
        )
        db.session.add(document)
        db.session.commit()
        return document

    def document_sync_item(self, member_id, document_type, source_filename, hash_character="a"):
        member_id = str(member_id)
        return {
            "member_id": member_id,
            "document_type": document_type,
            "source_filename": source_filename,
            "storage_uri": (
                f"s3://dreamz-test/gymassistant/Data/Attachments/"
                f"{member_id.zfill(7)}/{source_filename}"
            ),
            "sha256": hash_character * 64,
            "size_bytes": 4096,
        }

    def document_sync_payload(self, expected_member_ids, documents, expected_document_count=None):
        return {
            "expected_member_ids": [str(member_id) for member_id in expected_member_ids],
            "expected_document_count": (
                len(documents)
                if expected_document_count is None
                else expected_document_count
            ),
            "documents": documents,
        }

    def fep_payment_payload(self, **overrides):
        payload = {
            "source": "fep_manager_bank_upload_auto",
            "idempotency_key": "fep-bank-upload:522:123:700",
            "upload_id": 522,
            "upload_filename": "202607directdebits.txt",
            "record_id": 123,
            "client_number": "34203",
            "mcb_account": "1234567890",
            "name_owner": "Direct Debit",
            "details": "JUL Dreamz Fitness",
            "transaction_date": "20260629",
            "member_id": "34203",
            "member_name": "Direct, Debit",
            "membership_period": "2026-07",
            "bank_amount": "61.00",
            "base_amount": "60.00",
            "gym_billing_amount": "60.00",
            "statement_id": 700,
            "statement_date": "20260629",
            "statement_description": "MCB direct debit",
        }
        payload.update(overrides)
        return payload

    def add_pending_fep_payment_update(
        self,
        upload_id,
        *,
        target_agent="frontdesk_dreamz",
        suffix="1",
    ):
        record = FepPaymentUpdate(
            idempotency_key=f"scope-test:{upload_id}:{suffix}",
            source="scope-test",
            status="pending_gym_assistant_apply",
            member_id=f"9{str(upload_id)[-4:]}{suffix}",
            member_name=f"Scope {upload_id}",
            membership_period="2026-07",
            upload_id=int(upload_id),
            upload_filename=f"upload-{upload_id}.txt",
            record_id=int(f"{str(upload_id)[-4:]}{suffix}"),
            request_payload_hash=(str(suffix)[-1:] or "a") * 64,
            request_payload_json="{}",
            old_values_json="{}",
            new_values_json="{}",
            target_agent=target_agent,
            apply_attempts=0,
        )
        db.session.add(record)
        db.session.commit()
        return record

    def add_fep_payment_process_command(
        self,
        upload_id,
        *,
        status="pending",
        target_agent="frontdesk_dreamz",
        claimed_by=None,
        claim_expires_at=None,
    ):
        now = datetime.now()
        command = FepPaymentProcessCommand(
            source="scope-test",
            status=status,
            target_agent=target_agent,
            requested_limit=25,
            requested_by="scope-test",
            upload_id=upload_id,
            upload_filename=f"upload-{upload_id}.txt",
            request_payload_json="{}",
            claimed_by=claimed_by,
            claimed_at=now if claimed_by else None,
            claim_expires_at=claim_expires_at,
            started_at=now if status == "running" else None,
            created_at=now,
            updated_at=now,
        )
        db.session.add(command)
        db.session.commit()
        return command

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

    def test_sync_api_preserves_uploaded_s3_path_over_legacy_local_document_path(self):
        db.session.add(Member(member_id="1206", name="Example, Member"))
        db.session.commit()
        document = self.add_member_document(
            "1206",
            "contract",
            "contract.pdf",
            path="s3://dreamz-test/gymassistant/Data/Attachments/0001206/contract.pdf",
        )
        original_document_id = document.id
        payload = {
            "source": "unit-test",
            "members": [{"member_id": "1206", "name": "Example, Member"}],
            "documents": {
                "1206": [
                    {
                        "document_type": "contract",
                        "title": "Contract",
                        "path": r"C:\Gym Assistant 2.6\Data\Attachments\0001206\contract.pdf",
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
        refreshed = MemberDocument.query.one()
        self.assertEqual(refreshed.id, original_document_id)
        self.assertEqual(
            refreshed.path,
            "s3://dreamz-test/gymassistant/Data/Attachments/0001206/contract.pdf",
        )
        summary = json.loads(SyncRun.query.order_by(SyncRun.id.desc()).first().change_summary)
        self.assertEqual(summary["document_changes"], [])

    def test_sync_api_allows_new_s3_uri_to_replace_existing_s3_document_path(self):
        db.session.add(Member(member_id="1206", name="Example, Member"))
        db.session.commit()
        self.add_member_document(
            "1206",
            "contract",
            "contract.pdf",
            path="s3://dreamz-test/gymassistant/Data/Attachments/0001206/old-contract.pdf",
        )
        payload = {
            "source": "unit-test",
            "members": [{"member_id": "1206", "name": "Example, Member"}],
            "documents": {
                "1206": [
                    {
                        "document_type": "contract",
                        "title": "Contract",
                        "path": "s3://dreamz-test/gymassistant/Data/Attachments/0001206/contract.pdf",
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
        self.assertEqual(
            MemberDocument.query.one().path,
            "s3://dreamz-test/gymassistant/Data/Attachments/0001206/contract.pdf",
        )

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

    def test_sync_member_documents_requires_token(self):
        response = self.client.post(
            "/api/sync/member-documents",
            json={"expected_member_ids": [], "expected_document_count": 0, "documents": []},
        )

        self.assertEqual(response.status_code, 403)

    def test_sync_member_documents_updates_only_existing_document_paths(self):
        first_member = Member(
            member_id="26812",
            name="Existing, Member",
            email="member@example.com",
            billing_amount=60.0,
            balance=12.0,
        )
        second_member = Member(
            member_id="32348",
            name="No Email, Member",
            email=None,
            billing_amount=80.0,
        )
        db.session.add_all([first_member, second_member])
        db.session.commit()
        first_document = self.add_member_document("26812", "signup_form", "signup.pdf")
        second_document = self.add_member_document("32348", "contract", "contract.pdf")
        first_document_id = first_document.id
        second_document_id = second_document.id
        payload = self.document_sync_payload(
            ["26812", "32348"],
            [
                self.document_sync_item("26812", "signup_form", "signup.pdf"),
                self.document_sync_item("32348", "contract", "contract.pdf", "b"),
            ],
        )

        with (
            patch("dreamz_portal.s3_bucket_name", return_value="dreamz-test"),
            patch("dreamz_portal.s3_object_exists", return_value=True) as object_exists,
            patch("dreamz_portal.reconcile_pending_portal_invitations") as reconcile,
            patch("dreamz_portal.sync_invoice_membership_events") as invoice_sync,
        ):
            response = self.client.post(
                "/api/sync/member-documents",
                json=payload,
                headers={"X-Sync-Token": "sync-test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["member_count"], 2)
        self.assertEqual(response.json["document_count"], 2)
        self.assertEqual(response.json["documents_updated"], 2)
        self.assertEqual(response.json["documents_unchanged"], 0)
        self.assertEqual(object_exists.call_count, 2)
        self.assertEqual(db.session.get(MemberDocument, first_document_id).path, payload["documents"][0]["storage_uri"])
        self.assertEqual(db.session.get(MemberDocument, second_document_id).path, payload["documents"][1]["storage_uri"])
        refreshed_first_member = Member.query.filter_by(member_id="26812").one()
        refreshed_second_member = Member.query.filter_by(member_id="32348").one()
        self.assertEqual(refreshed_first_member.email, "member@example.com")
        self.assertEqual(refreshed_first_member.balance, 12.0)
        self.assertIsNone(refreshed_second_member.email)
        self.assertEqual(refreshed_second_member.billing_amount, 80.0)
        reconcile.assert_not_called()
        invoice_sync.assert_not_called()

    def test_sync_member_documents_is_idempotent_for_same_storage_uris(self):
        db.session.add(Member(member_id="26812", name="Existing, Member"))
        db.session.commit()
        document = self.add_member_document("26812", "contract", "contract.pdf")
        original_document_id = document.id
        item = self.document_sync_item("26812", "contract", "contract.pdf")
        payload = self.document_sync_payload(["26812"], [item])
        headers = {"X-Sync-Token": "sync-test-token"}

        with (
            patch("dreamz_portal.s3_bucket_name", return_value="dreamz-test"),
            patch("dreamz_portal.s3_object_exists", return_value=True),
        ):
            first_response = self.client.post("/api/sync/member-documents", json=payload, headers=headers)
            second_response = self.client.post("/api/sync/member-documents", json=payload, headers=headers)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json["documents_updated"], 1)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json["documents_updated"], 0)
        self.assertEqual(second_response.json["documents_unchanged"], 1)
        refreshed = MemberDocument.query.one()
        self.assertEqual(refreshed.id, original_document_id)
        self.assertEqual(refreshed.path, item["storage_uri"])

    def test_sync_member_documents_rejects_identity_mismatch_atomically(self):
        db.session.add(Member(member_id="26812", name="Existing, Member"))
        db.session.commit()
        first_document = self.add_member_document(
            "26812",
            "signup_form",
            "signup.pdf",
            path=r"C:\old\signup.pdf",
            display_order=0,
        )
        second_document = self.add_member_document(
            "26812",
            "contract",
            "contract.pdf",
            path=r"C:\old\contract.pdf",
            display_order=1,
        )
        payload = self.document_sync_payload(
            ["26812"],
            [
                self.document_sync_item("26812", "signup_form", "signup.pdf"),
                self.document_sync_item("26812", "contract", "different.pdf", "b"),
            ],
        )

        with (
            patch("dreamz_portal.s3_bucket_name", return_value="dreamz-test"),
            patch("dreamz_portal.s3_object_exists", return_value=True) as object_exists,
        ):
            response = self.client.post(
                "/api/sync/member-documents",
                json=payload,
                headers={"X-Sync-Token": "sync-test-token"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.session.get(MemberDocument, first_document.id).path, r"C:\old\signup.pdf")
        self.assertEqual(db.session.get(MemberDocument, second_document.id).path, r"C:\old\contract.pdf")
        object_exists.assert_not_called()

    def test_sync_member_documents_rejects_unknown_member_and_ambiguous_document(self):
        unknown_payload = self.document_sync_payload(
            ["99999"],
            [self.document_sync_item("99999", "contract", "contract.pdf")],
        )
        headers = {"X-Sync-Token": "sync-test-token"}
        with patch("dreamz_portal.s3_bucket_name", return_value="dreamz-test"):
            unknown_response = self.client.post(
                "/api/sync/member-documents",
                json=unknown_payload,
                headers=headers,
            )

        self.assertEqual(unknown_response.status_code, 409)

        db.session.add(Member(member_id="26812", name="Existing, Member"))
        db.session.commit()
        self.add_member_document("26812", "contract", "contract.pdf", path=r"C:\old\first.pdf", display_order=0)
        self.add_member_document("26812", "contract", "contract.pdf", path=r"C:\old\second.pdf", display_order=1)
        ambiguous_payload = self.document_sync_payload(
            ["26812"],
            [self.document_sync_item("26812", "contract", "contract.pdf")],
        )
        with patch("dreamz_portal.s3_bucket_name", return_value="dreamz-test"):
            ambiguous_response = self.client.post(
                "/api/sync/member-documents",
                json=ambiguous_payload,
                headers=headers,
            )

        self.assertEqual(ambiguous_response.status_code, 409)
        self.assertEqual(
            {document.path for document in MemberDocument.query.all()},
            {r"C:\old\first.pdf", r"C:\old\second.pdf"},
        )

    def test_sync_member_documents_rejects_count_and_member_set_mismatches(self):
        item = self.document_sync_item("26812", "contract", "contract.pdf")
        headers = {"X-Sync-Token": "sync-test-token"}
        count_response = self.client.post(
            "/api/sync/member-documents",
            json=self.document_sync_payload(["26812"], [item], expected_document_count=2),
            headers=headers,
        )
        member_response = self.client.post(
            "/api/sync/member-documents",
            json=self.document_sync_payload(["26812", "32348"], [item]),
            headers=headers,
        )

        self.assertEqual(count_response.status_code, 400)
        self.assertEqual(member_response.status_code, 400)

    def test_sync_member_documents_rejects_wrong_bucket_or_member_prefix(self):
        db.session.add(Member(member_id="26812", name="Existing, Member"))
        db.session.commit()
        self.add_member_document("26812", "contract", "contract.pdf")
        wrong_bucket = self.document_sync_item("26812", "contract", "contract.pdf")
        wrong_bucket["storage_uri"] = wrong_bucket["storage_uri"].replace("dreamz-test", "other-bucket")
        wrong_prefix = self.document_sync_item("26812", "contract", "contract.pdf")
        wrong_prefix["storage_uri"] = wrong_prefix["storage_uri"].replace("0026812", "0032348")
        headers = {"X-Sync-Token": "sync-test-token"}

        with patch("dreamz_portal.s3_bucket_name", return_value="dreamz-test"):
            bucket_response = self.client.post(
                "/api/sync/member-documents",
                json=self.document_sync_payload(["26812"], [wrong_bucket]),
                headers=headers,
            )
            prefix_response = self.client.post(
                "/api/sync/member-documents",
                json=self.document_sync_payload(["26812"], [wrong_prefix]),
                headers=headers,
            )

        self.assertEqual(bucket_response.status_code, 400)
        self.assertEqual(prefix_response.status_code, 400)

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

    def test_sync_api_records_photo_update_without_exposing_storage_uri(self):
        db.session.add(Member(
            member_id="33159",
            name="Martina, Ist",
            photo_path="s3://bucket/gymassistant/Data/Pictures/0033159-old.jpg",
        ))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={
                "source": "unit-test",
                "members": [{
                    "member_id": "33159",
                    "photo_path": "s3://bucket/gymassistant/Data/Pictures/0033159-new.jpg",
                }],
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_updated"], 1)
        summary = json.loads(SyncRun.query.one().change_summary)
        photo_change = summary["changed_members"][0]["changes"][0]
        self.assertEqual(photo_change["field"], "photo_path")
        self.assertEqual(photo_change["label"], "Photo")
        self.assertEqual(photo_change["old"], "Photo on file")
        self.assertEqual(photo_change["new"], "Updated photo")

    def test_sync_api_does_not_replace_versioned_photo_with_legacy_agent_path(self):
        versioned_path = "s3://bucket/gymassistant/Data/Pictures/0033159-9a83afe8a7309011.jpg"
        db.session.add(Member(
            member_id="33159",
            name="Martina, Ist",
            photo_path=versioned_path,
        ))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={
                "source": "legacy-frontdesk-agent",
                "members": [{
                    "member_id": "33159",
                    "photo_path": "s3://bucket/gymassistant/Data/Pictures/0033159.jpg",
                }],
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["members_updated"], 0)
        self.assertEqual(Member.query.filter_by(member_id="33159").one().photo_path, versioned_path)
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["changed_members"], [])

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

    def test_sync_api_blocks_suspicious_name_change_over_existing_full_name(self):
        db.session.add(Member(
            member_id="34933",
            name="Wissenmansen, Dennis",
            email="denniswissenmansen@hotmail.com",
            balance=0,
        ))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={
                "source": "Z:\\Data\\Temp Files\\AddedMembers.btx",
                "members": [
                    {
                        "member_id": "34933",
                        "name": "A, Bn",
                        "email": "denniswissenmansen@hotmail.com",
                        "balance": 5.0,
                    }
                ],
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        member = Member.query.filter_by(member_id="34933").one()
        self.assertEqual(member.name, "Wissenmansen, Dennis")
        self.assertEqual(member.balance, 5.0)
        sync_run = SyncRun.query.one()
        self.assertIn("Blocked 1 suspicious GymAssistant name change", sync_run.error)
        summary = json.loads(sync_run.change_summary)
        self.assertEqual(summary["suspicious_name_changes"][0]["member_id"], "34933")
        self.assertEqual(summary["suspicious_name_changes"][0]["raw_new"], "A, Bn")
        self.assertEqual(summary["suspicious_name_changes"][0]["parsed_old"], "Wissenmansen, Dennis")
        self.assertEqual(summary["changed_members"][0]["changes"][0]["field"], "balance")
        self.assertNotIn("name", [change["field"] for change in summary["changed_members"][0]["changes"]])

    def test_sync_api_allows_correct_full_name_to_replace_suspicious_existing_name(self):
        db.session.add(Member(member_id="34933", name="A, Bn"))
        db.session.commit()

        response = self.client.post(
            "/api/sync/members",
            json={"members": [{"member_id": "34933", "name": "Wissenmansen, Dennis"}]},
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Member.query.filter_by(member_id="34933").one().name, "Wissenmansen, Dennis")
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["changed_members"][0]["changes"][0]["field"], "name")
        self.assertEqual(summary["suspicious_name_changes"], [])

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

    def test_fep_payment_update_requires_token(self):
        self.add_direct_debit_member()

        response = self.client.post("/api/fep/payment-update", json=self.fep_payment_payload())

        self.assertEqual(response.status_code, 403)

    def test_fep_payment_update_queues_without_mutating_member(self):
        self.add_direct_debit_member()

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertFalse(response.json["applied"])
        self.assertTrue(response.json["queued"])
        self.assertEqual(response.json["status"], "pending_gym_assistant_apply")
        member = Member.query.filter_by(member_id="34203").one()
        self.assertEqual(member.last_payment, date(2026, 5, 29))
        self.assertEqual(member.next_payment, date(2026, 7, 1))
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.member_id, "34203")
        self.assertEqual(record.target_agent, "frontdesk_dreamz")
        self.assertIn("***7890", record.request_payload_json)
        self.assertNotIn("1234567890", record.request_payload_json)
        self.assertIn("\"next_payment\": \"2026-08-01\"", record.new_values_json)

    def test_fep_payment_update_accepts_ron_laptop_target_agent(self):
        self.add_direct_debit_member()

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="ron_laptop"),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["target_agent"], "ron_laptop")
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.target_agent, "ron_laptop")

    def test_fep_payment_update_accepts_reserve_target_only_by_allowlisted_alias(self):
        self.assertEqual(
            normalize_fep_payment_agent(
                "Reserve laptop 8KM7V7D",
                strict=True,
            ),
            "reserve_8km7v7d",
        )
        self.add_direct_debit_member()

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(
                target_agent="Reserve laptop 8KM7V7D"
            ),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["target_agent"],
            "reserve_8km7v7d",
        )
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.target_agent, "reserve_8km7v7d")

    def test_fep_payment_update_accepts_strict_dreamz_office_aliases(self):
        for alias in (
            "office",
            "office-pc",
            "dreamz_office",
            "Dreamz Office PC",
            "office computer dreamz",
        ):
            self.assertEqual(
                normalize_fep_payment_agent(alias, strict=True),
                "dreamz_office",
            )

        self.add_direct_debit_member()
        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="Dreamz Office PC"),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["target_agent"], "dreamz_office")
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.target_agent, "dreamz_office")

    def test_fep_payment_update_rejects_unknown_target_agent(self):
        self.add_direct_debit_member()

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="unknown_pc"),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])
        self.assertIn("target_agent", response.json["error"])
        self.assertEqual(FepPaymentUpdate.query.count(), 0)

    def test_fep_payment_update_duplicate_is_idempotent(self):
        self.add_direct_debit_member()
        payload = self.fep_payment_payload()

        first = self.client.post("/api/fep/payment-update", json=payload, headers=self.fep_headers())
        second = self.client.post("/api/fep/payment-update", json=payload, headers=self.fep_headers())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json["duplicate"])
        self.assertFalse(second.json["applied"])
        self.assertEqual(FepPaymentUpdate.query.count(), 1)

    def test_fep_payment_update_rejects_idempotency_payload_conflict(self):
        self.add_direct_debit_member()
        payload = self.fep_payment_payload()
        self.client.post("/api/fep/payment-update", json=payload, headers=self.fep_headers())

        changed_payload = self.fep_payment_payload(statement_description="different statement")
        response = self.client.post("/api/fep/payment-update", json=changed_payload, headers=self.fep_headers())

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("different payload", response.json["error"])
        self.assertEqual(FepPaymentUpdate.query.count(), 1)

    def test_fep_payment_update_rejects_amount_mismatch(self):
        self.add_direct_debit_member()

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(base_amount="55.00", gym_billing_amount="55.00"),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("does not match member billing_amount", response.json["error"])
        self.assertEqual(FepPaymentUpdate.query.count(), 0)

    def test_fep_payment_update_rejects_dependent_member(self):
        self.add_direct_debit_member(responsible_member_id="17436")

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("dependent of responsible member #17436", response.json["error"])
        self.assertEqual(FepPaymentUpdate.query.count(), 0)

    def test_fep_payment_update_rejects_member_with_dependents(self):
        self.add_direct_debit_member(dependent_member_ids="28193")

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("linked dependent membership", response.json["error"])
        self.assertEqual(FepPaymentUpdate.query.count(), 0)

    def test_fep_payment_update_rejects_period_when_current_due_does_not_match(self):
        self.add_direct_debit_member(due_date=date(2026, 6, 1), next_payment=date(2026, 6, 1))

        response = self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(),
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("does not match membership_period", response.json["error"])
        self.assertEqual(FepPaymentUpdate.query.count(), 0)

    def test_sync_agent_queue_result_and_member_sync_confirmation(self):
        self.add_direct_debit_member()
        self.client.post("/api/fep/payment-update", json=self.fep_payment_payload(), headers=self.fep_headers())

        queue_response = self.client.get(
            "/api/sync/fep-payment-updates",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(queue_response.status_code, 200)
        update = queue_response.json["updates"][0]
        self.assertEqual(update["member_id"], "34203")
        self.assertEqual(update["status"], "processing_gym_assistant_apply")
        self.assertEqual(update["claimed_by"], "frontdesk_dreamz")
        self.assertEqual(update["target_values"]["last_payment"], "2026-06-29")
        self.assertEqual(update["target_values"]["next_payment"], "2026-08-01")

        result_response = self.client.post(
            f"/api/sync/fep-payment-updates/{update['id']}/result",
            json={"status": "applied", "writer": "unit-test"},
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(result_response.status_code, 200)
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.status, "applied_to_gym_assistant")
        record.applied_at = datetime(2026, 6, 30, 12, 0)
        db.session.commit()
        member = Member.query.filter_by(member_id="34203").one()
        self.assertEqual(member.next_payment, date(2026, 7, 1))

        sync_response = self.client.post(
            "/api/sync/members",
            json={
                "source": "unit-test-gymassistant-sync",
                "members": [
                    {
                        "member_id": "34203",
                        "last_payment": "2026-06-30",
                        "last_payment_amount": 60.0,
                        "due_date": "2026-08-01",
                        "next_payment": "2026-08-01",
                        "billing_status": "ACTIVE",
                        "is_active": True,
                        "billing_amount": 60.0,
                        "balance": 0.0,
                    }
                ],
            },
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(sync_response.status_code, 200)
        db.session.refresh(record)
        db.session.refresh(member)
        self.assertEqual(record.status, "confirmed_by_gym_assistant_sync")
        self.assertIsNotNone(record.confirmed_at)
        self.assertEqual(member.last_payment, date(2026, 6, 30))
        self.assertEqual(member.next_payment, date(2026, 8, 1))

    def test_sync_agent_queue_claims_only_matching_target_agent(self):
        self.add_direct_debit_member()
        self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="ron_laptop"),
            headers=self.fep_headers(),
        )

        frontdesk_response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        laptop_response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=ron_laptop",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(frontdesk_response.status_code, 200)
        self.assertEqual(frontdesk_response.json["updates"], [])
        self.assertEqual(laptop_response.status_code, 200)
        self.assertEqual(len(laptop_response.json["updates"]), 1)
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.status, "processing_gym_assistant_apply")
        self.assertEqual(record.claimed_by, "ron_laptop")

    def test_sync_agent_queue_peek_does_not_claim(self):
        self.add_direct_debit_member()
        self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="ron_laptop"),
            headers=self.fep_headers(),
        )

        response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=ron_laptop&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["peek"])
        self.assertEqual(len(response.json["updates"]), 1)
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.status, "pending_gym_assistant_apply")
        self.assertIsNone(record.claimed_by)

    def test_payment_token_rollout_is_backward_compatible_per_agent(self):
        response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=dreamz_office&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["agent_id"], "dreamz_office")
        self.assertEqual(response.json["agent_label"], "Dreamz Office PC")

    def test_dedicated_payment_tokens_are_agent_bound_and_payment_only(self):
        app.config["FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP"] = "ron-payment-token"
        app.config["FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE"] = "office-payment-token"

        office = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=dreamz_office&peek=1",
            headers={"X-Sync-Token": "office-payment-token"},
        )
        office_with_shared = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=dreamz_office&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        office_with_ron = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=dreamz_office&peek=1",
            headers={"X-Sync-Token": "ron-payment-token"},
        )
        ron_with_office = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=ron_laptop",
            headers={"X-Sync-Token": "office-payment-token"},
        )
        non_payment = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "office-payment-token"},
        )
        frontdesk_legacy = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(office.status_code, 200)
        self.assertEqual(office_with_shared.status_code, 403)
        self.assertEqual(office_with_ron.status_code, 403)
        self.assertEqual(ron_with_office.status_code, 403)
        self.assertEqual(non_payment.status_code, 403)
        self.assertEqual(frontdesk_legacy.status_code, 200)

    def test_reserve_agent_requires_its_own_payment_only_token(self):
        without_dedicated = self.client.get(
            "/api/sync/fep-payment-updates"
            "?agent_id=reserve_8km7v7d&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        app.config[
            "FEP_PAYMENT_SYNC_TOKEN_RESERVE_8KM7V7D"
        ] = "reserve-payment-token"
        matching = self.client.get(
            "/api/sync/fep-payment-updates"
            "?agent_id=reserve_8km7v7d&peek=1",
            headers={"X-Sync-Token": "reserve-payment-token"},
        )
        shared = self.client.get(
            "/api/sync/fep-payment-updates"
            "?agent_id=reserve_8km7v7d&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        wrong_agent = self.client.get(
            "/api/sync/fep-payment-updates"
            "?agent_id=ron_laptop&peek=1",
            headers={"X-Sync-Token": "reserve-payment-token"},
        )
        non_payment = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "reserve-payment-token"},
        )

        self.assertEqual(without_dedicated.status_code, 503)
        self.assertEqual(matching.status_code, 200)
        self.assertEqual(
            matching.json["agent_label"],
            "Reserve laptop 8KM7V7D",
        )
        self.assertEqual(shared.status_code, 403)
        self.assertEqual(wrong_agent.status_code, 403)
        self.assertEqual(non_payment.status_code, 403)

    def test_reserve_command_peek_is_read_only_and_sanitized(self):
        app.config[
            "FEP_PAYMENT_SYNC_TOKEN_RESERVE_8KM7V7D"
        ] = "reserve-payment-token"
        command = FepPaymentProcessCommand(
            source="fep",
            status="pending",
            target_agent="reserve_8km7v7d",
            requested_limit=1,
            requested_by="test-admin",
            upload_id=522,
            upload_filename="sensitive-filename.csv",
        )
        db.session.add(command)
        db.session.commit()

        response = self.client.get(
            "/api/sync/fep-payment-process-commands"
            "?agent_id=reserve_8km7v7d&peek=1",
            headers={"X-Sync-Token": "reserve-payment-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["read_only"])
        self.assertTrue(response.json["pending_command_available"])
        self.assertTrue(response.json["writer_reserved"])
        self.assertEqual(response.json["commands"], [])
        self.assertNotIn("upload_filename", response.json)
        self.assertNotIn("requested_by", response.json)
        db.session.refresh(command)
        self.assertEqual(command.status, "pending")
        self.assertIsNone(command.claimed_by)
        self.assertIsNone(command.claimed_at)
        self.assertIsNone(command.claim_expires_at)
        self.assertIsNone(command.started_at)

    def test_dedicated_token_never_becomes_general_sync_token(self):
        app.config["FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP"] = "sync-test-token"

        payment = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=ron_laptop&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        non_payment = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(payment.status_code, 200)
        self.assertEqual(non_payment.status_code, 403)

    def test_dedicated_payment_token_is_rejected_by_every_sync_data_route(self):
        app.config["FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE"] = "office-payment-token"
        headers = {"X-Sync-Token": "office-payment-token"}
        responses = [
            self.client.post("/api/sync/members", json={"members": []}, headers=headers),
            self.client.get("/api/sync/member-ids", headers=headers),
            self.client.post(
                "/api/sync/files",
                data=b"not-uploaded",
                headers={
                    **headers,
                    "X-Storage-Key": "test/file.bin",
                },
            ),
            self.client.post(
                "/api/sync/member-documents",
                json={
                    "expected_member_ids": [],
                    "expected_document_count": 0,
                    "documents": [],
                },
                headers=headers,
            ),
            self.client.get("/api/sync/missing-file-keys", headers=headers),
        ]

        self.assertEqual([response.status_code for response in responses], [403] * 5)

    def test_duplicate_dedicated_payment_tokens_fail_closed(self):
        app.config["FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP"] = "duplicate-token"
        app.config["FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE"] = "duplicate-token"

        response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=dreamz_office&peek=1",
            headers={"X-Sync-Token": "duplicate-token"},
        )

        self.assertEqual(response.status_code, 503)

    def test_fep_payment_process_request_creates_agent_command(self):
        self.add_direct_debit_member()
        self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="frontdesk_dreamz"),
            headers=self.fep_headers(),
        )

        response = self.client.post(
            "/api/fep/payment-process-request",
            json={
                "source": "fep_manager",
                "target_agent": "frontdesk_dreamz",
                "limit": 12,
                "requested_by": "ron",
                "upload_id": 522,
            },
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertEqual(response.json["status"], "pending")
        self.assertEqual(response.json["target_agent"], "frontdesk_dreamz")
        self.assertEqual(response.json["requested_limit"], 12)
        self.assertEqual(response.json["pending_payments"], 1)
        command = FepPaymentProcessCommand.query.one()
        self.assertEqual(command.target_agent, "frontdesk_dreamz")
        self.assertEqual(command.upload_id, 522)

    def test_fep_payment_process_request_reuses_active_command(self):
        self.add_direct_debit_member()
        self.client.post("/api/fep/payment-update", json=self.fep_payment_payload(), headers=self.fep_headers())
        first = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "limit": 5},
            headers=self.fep_headers(),
        )
        second = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "limit": 5},
            headers=self.fep_headers(),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json["duplicate"])
        self.assertEqual(FepPaymentProcessCommand.query.count(), 1)

    def test_fep_payment_process_command_is_scoped_to_its_upload(self):
        first = self.add_pending_fep_payment_update(522, suffix="1")
        second = self.add_pending_fep_payment_update(900, suffix="2")

        create_response = self.client.post(
            "/api/fep/payment-process-request",
            json={
                "target_agent": "frontdesk_dreamz",
                "upload_id": 522,
                "limit": 25,
            },
            headers=self.fep_headers(),
        )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json["pending_payments"], 1)
        self.assertEqual(create_response.json["upload_id"], 522)

        command_response = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        command = command_response.json["commands"][0]
        scoped_updates = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz&limit=100",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(scoped_updates.status_code, 200)
        self.assertTrue(scoped_updates.json["upload_scoped"])
        self.assertEqual(scoped_updates.json["upload_id"], 522)
        self.assertEqual(scoped_updates.json["command_id"], command["id"])
        self.assertEqual(scoped_updates.json["limit_applied"], 1)
        self.assertEqual(
            [row["id"] for row in scoped_updates.json["updates"]],
            [first.id],
        )
        db.session.refresh(first)
        db.session.refresh(second)
        self.assertEqual(first.status, "processing_gym_assistant_apply")
        self.assertEqual(second.status, "pending_gym_assistant_apply")

        update_result = self.client.post(
            f"/api/sync/fep-payment-updates/{first.id}/result",
            json={"status": "applied", "writer": "scope-test"},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )
        self.assertEqual(update_result.status_code, 200)
        command_result = self.client.post(
            f"/api/sync/fep-payment-process-commands/{command['id']}/result",
            json={
                "status": "completed",
                "summary": {
                    "received": 1,
                    "applied": 1,
                    "failed": 0,
                    "deferred": 0,
                },
            },
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )
        self.assertEqual(command_result.status_code, 200)

        second_create = self.client.post(
            "/api/fep/payment-process-request",
            json={
                "target_agent": "frontdesk_dreamz",
                "upload_id": 900,
                "limit": 25,
            },
            headers=self.fep_headers(),
        )
        self.assertEqual(second_create.status_code, 200)
        self.assertEqual(second_create.json["pending_payments"], 1)
        second_claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        self.assertEqual(second_claim.json["commands"][0]["upload_id"], 900)
        second_updates = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        self.assertEqual(
            [row["id"] for row in second_updates.json["updates"]],
            [second.id],
        )

    def test_dreamz_office_runs_an_upload_scoped_command_end_to_end(self):
        office_update = self.add_pending_fep_payment_update(
            522,
            target_agent="dreamz_office",
            suffix="1",
        )
        frontdesk_update = self.add_pending_fep_payment_update(
            522,
            target_agent="frontdesk_dreamz",
            suffix="2",
        )
        created = self.client.post(
            "/api/fep/payment-process-request",
            json={
                "target_agent": "Dreamz Office PC",
                "upload_id": 522,
                "limit": 25,
            },
            headers=self.fep_headers(),
        )
        command = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=dreamz_office",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        updates = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=dreamz_office&limit=100",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json["target_agent"], "dreamz_office")
        self.assertEqual(created.json["target_agent_label"], "Dreamz Office PC")
        self.assertEqual(command.status_code, 200)
        self.assertEqual(command.json["commands"][0]["upload_id"], 522)
        self.assertEqual(updates.status_code, 200)
        self.assertTrue(updates.json["upload_scoped"])
        self.assertEqual(
            [row["id"] for row in updates.json["updates"]],
            [office_update.id],
        )
        db.session.refresh(frontdesk_update)
        self.assertEqual(frontdesk_update.status, "pending_gym_assistant_apply")

    def test_process_request_rejects_other_upload_while_agent_is_busy(self):
        self.add_pending_fep_payment_update(522, suffix="1")
        self.add_pending_fep_payment_update(900, suffix="2")
        first = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 522},
            headers=self.fep_headers(),
        )
        second = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 900},
            headers=self.fep_headers(),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertIn("another upload", second.json["error"])
        self.assertEqual(FepPaymentProcessCommand.query.count(), 1)

    def test_unclaimed_process_command_blocks_unscoped_update_claim(self):
        first = self.add_pending_fep_payment_update(522, suffix="1")
        second = self.add_pending_fep_payment_update(900, suffix="2")
        create_response = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 522},
            headers=self.fep_headers(),
        )
        self.assertEqual(create_response.status_code, 200)

        updates = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(updates.status_code, 200)
        self.assertTrue(updates.json["command_waiting_for_claim"])
        self.assertEqual(updates.json["updates"], [])
        db.session.refresh(first)
        db.session.refresh(second)
        self.assertEqual(first.status, "pending_gym_assistant_apply")
        self.assertEqual(second.status, "pending_gym_assistant_apply")

    def test_update_result_rejects_pending_or_raw_unclaimed_record(self):
        self.add_pending_fep_payment_update(522, suffix="1")
        record = self.add_pending_fep_payment_update(900, suffix="2")
        self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 522},
            headers=self.fep_headers(),
        )

        pending_response = self.client.post(
            f"/api/sync/fep-payment-updates/{record.id}/result",
            json={"status": "applied"},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )
        self.assertEqual(pending_response.status_code, 409)
        self.assertIn("pending_gym_assistant_apply", pending_response.json["error"])

        record.status = "processing_gym_assistant_apply"
        record.claimed_by = ""
        record.claimed_at = datetime.now()
        record.claim_expires_at = datetime.now() + timedelta(minutes=5)
        db.session.commit()
        unclaimed_response = self.client.post(
            f"/api/sync/fep-payment-updates/{record.id}/result",
            json={"status": "applied"},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )

        self.assertEqual(unclaimed_response.status_code, 409)
        db.session.refresh(record)
        self.assertEqual(record.status, "processing_gym_assistant_apply")
        self.assertEqual(record.claimed_by, "")

    def test_process_command_cannot_complete_with_processing_update(self):
        record = self.add_pending_fep_payment_update(522, suffix="1")
        create_response = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 522},
            headers=self.fep_headers(),
        )
        command_id = create_response.json["command_id"]
        self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        response = self.client.post(
            f"/api/sync/fep-payment-process-commands/{command_id}/result",
            json={"status": "completed", "summary": {"received": 1, "applied": 1}},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["status"], "failed")
        self.assertEqual(response.json["processing_updates"], 1)
        command = db.session.get(FepPaymentProcessCommand, command_id)
        db.session.refresh(record)
        self.assertEqual(command.status, "failed")
        self.assertIn("manual reconciliation", command.error)
        self.assertEqual(record.status, "processing_gym_assistant_apply")

    def test_deferred_update_becomes_terminal_and_next_row_can_run(self):
        first = self.add_pending_fep_payment_update(522, suffix="1")
        second = self.add_pending_fep_payment_update(522, suffix="2")
        self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 522},
            headers=self.fep_headers(),
        )
        self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        claimed = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        self.assertEqual(claimed.json["updates"][0]["id"], first.id)

        deferred = self.client.post(
            f"/api/sync/fep-payment-updates/{first.id}/result",
            json={"status": "deferred", "reason": "desktop_not_idle"},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )
        next_claim = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(deferred.status_code, 200)
        self.assertEqual(deferred.json["status"], "failed")
        db.session.refresh(first)
        db.session.refresh(second)
        self.assertEqual(first.status, "failed")
        self.assertIn("Manual retry is required", first.error)
        self.assertEqual(
            [row["id"] for row in next_claim.json["updates"]],
            [second.id],
        )
        self.assertEqual(second.status, "processing_gym_assistant_apply")

    def test_process_command_claims_are_serialized_per_agent(self):
        first = self.add_fep_payment_process_command(522)
        second = self.add_fep_payment_process_command(900)

        first_claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        blocked_claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(first_claim.status_code, 200)
        self.assertEqual(first_claim.json["commands"][0]["id"], first.id)
        self.assertEqual(blocked_claim.status_code, 200)
        self.assertTrue(blocked_claim.json["command_in_progress"])
        self.assertEqual(blocked_claim.json["commands"], [])
        db.session.refresh(first)
        db.session.refresh(second)
        self.assertEqual(first.status, "running")
        self.assertEqual(second.status, "pending")

    def test_process_command_claims_are_serialized_globally_across_agents(self):
        frontdesk = self.add_fep_payment_process_command(
            522,
            target_agent="frontdesk_dreamz",
        )
        laptop = self.add_fep_payment_process_command(
            523,
            target_agent="ron_laptop",
        )
        office = self.add_fep_payment_process_command(
            524,
            target_agent="dreamz_office",
        )

        frontdesk_claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        laptop_blocked = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=ron_laptop",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        office_blocked = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=dreamz_office",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(
            frontdesk_claim.json["commands"][0]["id"],
            frontdesk.id,
        )
        for blocked in (laptop_blocked, office_blocked):
            self.assertEqual(blocked.status_code, 200)
            self.assertTrue(blocked.json["writer_busy"])
            self.assertEqual(blocked.json["writer_agent_id"], "frontdesk_dreamz")
            self.assertEqual(blocked.json["commands"], [])
        self.assertEqual(
            FepPaymentProcessCommand.query.filter_by(status="running").count(),
            1,
        )

        completed = self.client.post(
            f"/api/sync/fep-payment-process-commands/{frontdesk.id}/result",
            json={"status": "completed"},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )
        laptop_claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=ron_laptop",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(laptop_claim.json["commands"][0]["id"], laptop.id)
        db.session.refresh(office)
        self.assertEqual(office.status, "pending")
        self.assertEqual(
            FepPaymentProcessCommand.query.filter_by(status="running").count(),
            1,
        )

    def test_running_office_command_blocks_frontdesk_legacy_payment_claim(self):
        frontdesk_update = self.add_pending_fep_payment_update(
            522,
            target_agent="frontdesk_dreamz",
        )
        office_command = self.add_fep_payment_process_command(
            900,
            target_agent="dreamz_office",
        )
        office_claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=dreamz_office",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        frontdesk_claim = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(office_claim.json["commands"][0]["id"], office_command.id)
        self.assertEqual(frontdesk_claim.status_code, 200)
        self.assertEqual(frontdesk_claim.json["updates"], [])
        self.assertTrue(frontdesk_claim.json["writer_busy"])
        self.assertEqual(frontdesk_claim.json["writer_agent_id"], "dreamz_office")
        db.session.refresh(frontdesk_update)
        self.assertEqual(frontdesk_update.status, "pending_gym_assistant_apply")

    def test_pending_other_agent_command_preserves_frontdesk_legacy_claim(self):
        frontdesk_update = self.add_pending_fep_payment_update(
            522,
            target_agent="frontdesk_dreamz",
        )
        office_command = self.add_fep_payment_process_command(
            900,
            target_agent="dreamz_office",
        )

        frontdesk_claim = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz&limit=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(frontdesk_claim.status_code, 200)
        self.assertEqual(
            [row["id"] for row in frontdesk_claim.json["updates"]],
            [frontdesk_update.id],
        )
        db.session.refresh(office_command)
        self.assertEqual(office_command.status, "pending")

    def test_legacy_processing_claim_blocks_new_cross_agent_command(self):
        laptop_update = self.add_pending_fep_payment_update(
            522,
            target_agent="ron_laptop",
        )
        laptop_update.status = "processing_gym_assistant_apply"
        laptop_update.claimed_by = "ron_laptop"
        laptop_update.claimed_at = datetime.now()
        laptop_update.claim_expires_at = datetime.now() + timedelta(minutes=5)
        office_command = self.add_fep_payment_process_command(
            900,
            target_agent="dreamz_office",
        )
        db.session.commit()

        blocked = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=dreamz_office",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(blocked.status_code, 200)
        self.assertTrue(blocked.json["writer_busy"])
        self.assertEqual(blocked.json["writer_agent_id"], "ron_laptop")
        self.assertEqual(blocked.json["processing_updates"], 1)
        self.assertEqual(blocked.json["commands"], [])
        db.session.refresh(office_command)
        self.assertEqual(office_command.status, "pending")

    def test_parallel_cross_agent_command_claims_start_only_one_writer(self):
        self.add_fep_payment_process_command(
            522,
            target_agent="frontdesk_dreamz",
        )
        self.add_fep_payment_process_command(
            523,
            target_agent="ron_laptop",
        )
        app.config["_RUNTIME_SCHEMA_READY"] = True
        barrier = threading.Barrier(2)

        def claim(agent_id):
            barrier.wait(timeout=5)
            with app.test_client() as client:
                response = client.get(
                    f"/api/sync/fep-payment-process-commands?agent_id={agent_id}",
                    headers={"X-Sync-Token": "sync-test-token"},
                )
                return response.status_code, response.get_json()

        app.config["TESTING"] = False
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(claim, ("frontdesk_dreamz", "ron_laptop"))
                )
        finally:
            app.config["TESTING"] = True

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(
            sum(bool(payload["commands"]) for _, payload in results),
            1,
        )
        db.session.expire_all()
        self.assertEqual(
            FepPaymentProcessCommand.query.filter_by(status="running").count(),
            1,
        )

    def test_parallel_legacy_claims_do_not_offer_a_second_ui_batch(self):
        self.add_pending_fep_payment_update(522, suffix="1")
        self.add_pending_fep_payment_update(522, suffix="2")
        app.config["_RUNTIME_SCHEMA_READY"] = True
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            with app.test_client() as client:
                response = client.get(
                    "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz&limit=1",
                    headers={"X-Sync-Token": "sync-test-token"},
                )
                return response.status_code, response.get_json()

        app.config["TESTING"] = False
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: claim(), range(2)))
        finally:
            app.config["TESTING"] = True

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(
            sum(len(payload["updates"]) for _, payload in results),
            1,
        )
        db.session.expire_all()
        self.assertEqual(
            FepPaymentUpdate.query.filter_by(
                status="processing_gym_assistant_apply"
            ).count(),
            1,
        )
        self.assertEqual(
            FepPaymentUpdate.query.filter_by(
                status="pending_gym_assistant_apply"
            ).count(),
            1,
        )

    def test_payment_command_advisory_lock_is_stable_and_postgresql_only(self):
        frontdesk_key = fep_payment_command_agent_lock_key("frontdesk_dreamz")
        writer_key = fep_payment_writer_lock_key()
        self.assertEqual(
            frontdesk_key,
            fep_payment_command_agent_lock_key("frontdesk_dreamz"),
        )
        self.assertNotEqual(
            frontdesk_key,
            fep_payment_command_agent_lock_key("ron_laptop"),
        )
        self.assertGreaterEqual(frontdesk_key, -(2**63))
        self.assertLess(frontdesk_key, 2**63)
        self.assertEqual(writer_key, fep_payment_writer_lock_key())
        self.assertNotEqual(writer_key, frontdesk_key)

        with patch.object(db.session, "execute") as execute:
            acquire_fep_payment_command_agent_lock("frontdesk_dreamz")
            acquire_fep_payment_writer_lock()
        execute.assert_not_called()

        postgresql_bind = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql")
        )
        with (
            patch.object(db.session, "get_bind", return_value=postgresql_bind),
            patch.object(db.session, "execute") as execute,
        ):
            acquire_fep_payment_command_agent_lock("frontdesk_dreamz")
        execute.assert_called_once()
        statement, parameters = execute.call_args.args
        self.assertIn("pg_advisory_xact_lock", str(statement))
        self.assertEqual(parameters, {"lock_key": frontdesk_key})

        with (
            patch.object(db.session, "get_bind", return_value=postgresql_bind),
            patch.object(db.session, "execute") as execute,
        ):
            acquire_fep_payment_writer_lock()
        execute.assert_called_once()
        statement, parameters = execute.call_args.args
        self.assertIn("pg_advisory_xact_lock", str(statement))
        self.assertEqual(parameters, {"lock_key": writer_key})

    def test_payment_routes_take_global_then_agent_lock_before_expiry_release(self):
        lock_source = inspect.getsource(hold_fep_payment_writer_lock)
        self.assertLess(
            lock_source.index("acquire_fep_payment_writer_lock"),
            lock_source.index("acquire_fep_payment_command_agent_lock"),
        )
        create_source = inspect.getsource(create_fep_payment_process_command)
        claim_source = inspect.getsource(api_sync_fep_payment_process_commands)
        for source in (create_source, claim_source):
            self.assertLess(
                source.index("hold_fep_payment_writer_lock"),
                source.index("release_expired_fep_payment_process_commands"),
            )
        result_source = inspect.getsource(api_sync_fep_payment_update_result)
        self.assertLess(
            result_source.index("hold_fep_payment_writer_lock"),
            result_source.index("release_expired_fep_payment_claims"),
        )
        release_source = inspect.getsource(
            release_expired_fep_payment_process_commands
        )
        self.assertIn(".update(", release_source)
        self.assertIn("synchronize_session=False", release_source)

    def test_expired_command_release_is_atomic_and_agent_scoped(self):
        now = datetime.now()
        expired = self.add_fep_payment_process_command(
            522,
            status="running",
            claimed_by="frontdesk_dreamz",
            claim_expires_at=now - timedelta(seconds=1),
        )
        fresh = self.add_fep_payment_process_command(
            523,
            status="running",
            claimed_by="frontdesk_dreamz",
            claim_expires_at=now + timedelta(minutes=5),
        )
        other_agent = self.add_fep_payment_process_command(
            524,
            status="running",
            target_agent="ron_laptop",
            claimed_by="ron_laptop",
            claim_expires_at=now - timedelta(seconds=1),
        )

        released = release_expired_fep_payment_process_commands(
            now,
            "frontdesk_dreamz",
        )
        db.session.commit()

        self.assertEqual(released, 1)
        db.session.refresh(expired)
        db.session.refresh(fresh)
        db.session.refresh(other_agent)
        self.assertEqual(expired.status, "pending")
        self.assertEqual(fresh.status, "running")
        self.assertEqual(other_agent.status, "running")

    def test_expired_command_release_survives_later_conflict_rollback(self):
        self.add_pending_fep_payment_update(900, suffix="1")
        expired = self.add_fep_payment_process_command(
            522,
            status="running",
            claimed_by="frontdesk_dreamz",
            claim_expires_at=datetime.now() - timedelta(seconds=1),
        )

        response = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 900},
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("another upload", response.json["error"])
        db.session.expire_all()
        persisted = db.session.get(FepPaymentProcessCommand, expired.id)
        self.assertEqual(persisted.status, "pending")
        self.assertIsNone(persisted.claimed_by)
        self.assertIsNone(persisted.claim_expires_at)
        self.assertIn("claim expired", persisted.error)

    def test_process_request_status_persists_expired_command_release(self):
        expired = self.add_fep_payment_process_command(
            522,
            status="running",
            claimed_by="frontdesk_dreamz",
            claim_expires_at=datetime.now() - timedelta(seconds=1),
        )

        response = self.client.get(
            f"/api/fep/payment-process-request?command_id={expired.id}",
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "pending")
        db.session.expire_all()
        persisted = db.session.get(FepPaymentProcessCommand, expired.id)
        self.assertEqual(persisted.status, "pending")
        self.assertIsNone(persisted.claimed_by)
        self.assertIn("claim expired", persisted.error)

    def test_payment_peek_does_not_renew_process_command_lease(self):
        self.add_pending_fep_payment_update(522, suffix="1")
        self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "upload_id": 522},
            headers=self.fep_headers(),
        )
        claim = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        command = db.session.get(
            FepPaymentProcessCommand,
            claim.json["commands"][0]["id"],
        )
        fixed_expiry = datetime.now() + timedelta(minutes=5)
        command.claim_expires_at = fixed_expiry
        db.session.commit()

        response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        db.session.refresh(command)
        self.assertEqual(command.claim_expires_at, fixed_expiry)

    def test_expired_update_claim_requires_manual_reconciliation(self):
        record = self.add_pending_fep_payment_update(522, suffix="1")
        record.status = "processing_gym_assistant_apply"
        record.claimed_by = "frontdesk_dreamz"
        record.claimed_at = datetime.now() - timedelta(minutes=10)
        record.claim_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()

        response = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz&peek=1",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        next_claim = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["released_expired_claims"], 1)
        self.assertEqual(response.json["updates"], [])
        self.assertEqual(next_claim.json["updates"], [])
        db.session.refresh(record)
        self.assertEqual(record.status, "failed")
        self.assertIn("manual reconciliation", record.error)
        self.assertIsNone(record.claimed_by)

    def test_expired_payment_release_is_atomic_and_agent_upload_scoped(self):
        now = datetime.now()
        matching = self.add_pending_fep_payment_update(522, suffix="1")
        other_upload = self.add_pending_fep_payment_update(900, suffix="2")
        other_agent = self.add_pending_fep_payment_update(
            522,
            suffix="3",
            target_agent="ron_laptop",
        )
        for record, claimed_by in (
            (matching, "frontdesk_dreamz"),
            (other_upload, "frontdesk_dreamz"),
            (other_agent, "ron_laptop"),
        ):
            record.status = "processing_gym_assistant_apply"
            record.claimed_by = claimed_by
            record.claimed_at = now - timedelta(minutes=10)
            record.claim_expires_at = now - timedelta(seconds=1)
        db.session.commit()

        released = release_expired_fep_payment_claims(
            now,
            agent_id="frontdesk_dreamz",
            upload_id=522,
        )
        db.session.commit()

        self.assertEqual(released, 1)
        db.session.refresh(matching)
        db.session.refresh(other_upload)
        db.session.refresh(other_agent)
        self.assertEqual(matching.status, "failed")
        self.assertEqual(other_upload.status, "processing_gym_assistant_apply")
        self.assertEqual(other_agent.status, "processing_gym_assistant_apply")

    def test_stale_expiry_release_cannot_overwrite_applied_payment(self):
        now = datetime.now()
        record = self.add_pending_fep_payment_update(522, suffix="1")
        record.status = "processing_gym_assistant_apply"
        record.claimed_by = "frontdesk_dreamz"
        record.claimed_at = now - timedelta(minutes=10)
        record.claim_expires_at = now - timedelta(seconds=1)
        db.session.commit()
        stale_record = db.session.get(FepPaymentUpdate, record.id)

        db.session.execute(
            FepPaymentUpdate.__table__.update()
            .where(FepPaymentUpdate.id == record.id)
            .values(
                status="applied_to_gym_assistant",
                claimed_by=None,
                claimed_at=None,
                claim_expires_at=None,
                applied_at=now,
            )
        )
        self.assertEqual(
            stale_record.status,
            "processing_gym_assistant_apply",
        )

        released = release_expired_fep_payment_claims(
            now,
            agent_id="frontdesk_dreamz",
            update_id=record.id,
        )
        db.session.commit()

        self.assertEqual(released, 0)
        db.session.refresh(stale_record)
        self.assertEqual(stale_record.status, "applied_to_gym_assistant")
        self.assertIsNotNone(stale_record.applied_at)

    def test_late_result_after_claim_expiry_is_rejected_and_not_applied(self):
        record = self.add_pending_fep_payment_update(522, suffix="1")
        record.status = "processing_gym_assistant_apply"
        record.claimed_by = "frontdesk_dreamz"
        record.claimed_at = datetime.now() - timedelta(minutes=10)
        record.claim_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()

        response = self.client.post(
            f"/api/sync/fep-payment-updates/{record.id}/result",
            json={"status": "applied"},
            headers={
                "X-Sync-Token": "sync-test-token",
                "X-Sync-Agent": "frontdesk_dreamz",
            },
        )

        self.assertEqual(response.status_code, 409)
        db.session.refresh(record)
        self.assertEqual(record.status, "failed")
        self.assertIsNone(record.applied_at)
        self.assertIn("manual reconciliation", record.error)

    def test_fep_payment_process_request_rejects_when_no_pending_updates(self):
        response = self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz"},
            headers=self.fep_headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("no pending", response.json["error"])

    def test_sync_agent_claims_and_completes_payment_process_command(self):
        self.add_direct_debit_member()
        self.client.post("/api/fep/payment-update", json=self.fep_payment_payload(), headers=self.fep_headers())
        self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "frontdesk_dreamz", "limit": 7},
            headers=self.fep_headers(),
        )

        command_response = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(command_response.status_code, 200)
        command = command_response.json["commands"][0]
        self.assertEqual(command["requested_limit"], 7)
        self.assertEqual(command["status"], "running")
        db_command = FepPaymentProcessCommand.query.one()
        self.assertEqual(db_command.status, "running")
        self.assertEqual(db_command.claimed_by, "frontdesk_dreamz")

        result_response = self.client.post(
            f"/api/sync/fep-payment-process-commands/{command['id']}/result",
            json={"status": "completed", "summary": {"received": 1, "applied": 1, "failed": 0, "deferred": 0}},
            headers={"X-Sync-Token": "sync-test-token", "X-Sync-Agent": "frontdesk_dreamz"},
        )

        self.assertEqual(result_response.status_code, 200)
        db.session.refresh(db_command)
        self.assertEqual(db_command.status, "completed")
        self.assertIsNotNone(db_command.completed_at)

    def test_sync_agent_process_command_only_claims_matching_agent(self):
        self.add_direct_debit_member()
        self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="ron_laptop"),
            headers=self.fep_headers(),
        )
        self.client.post(
            "/api/fep/payment-process-request",
            json={"target_agent": "ron_laptop"},
            headers=self.fep_headers(),
        )

        frontdesk_response = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=frontdesk_dreamz",
            headers={"X-Sync-Token": "sync-test-token"},
        )
        laptop_response = self.client.get(
            "/api/sync/fep-payment-process-commands?agent_id=ron_laptop",
            headers={"X-Sync-Token": "sync-test-token"},
        )

        self.assertEqual(frontdesk_response.status_code, 200)
        self.assertEqual(frontdesk_response.json["commands"], [])
        self.assertEqual(laptop_response.status_code, 200)
        self.assertEqual(len(laptop_response.json["commands"]), 1)

    def test_sync_agent_result_rejects_wrong_agent_claim(self):
        self.add_direct_debit_member()
        self.client.post(
            "/api/fep/payment-update",
            json=self.fep_payment_payload(target_agent="ron_laptop"),
            headers=self.fep_headers(),
        )
        update = self.client.get(
            "/api/sync/fep-payment-updates?agent_id=ron_laptop",
            headers={"X-Sync-Token": "sync-test-token"},
        ).json["updates"][0]

        response = self.client.post(
            f"/api/sync/fep-payment-updates/{update['id']}/result",
            json={"status": "applied", "writer": "unit-test"},
            headers={"X-Sync-Token": "sync-test-token", "X-Sync-Agent": "frontdesk_dreamz"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("claimed by Ron laptop", response.json["error"])
        record = FepPaymentUpdate.query.one()
        self.assertEqual(record.status, "processing_gym_assistant_apply")
        self.assertEqual(record.claimed_by, "ron_laptop")


if __name__ == "__main__":
    unittest.main()
