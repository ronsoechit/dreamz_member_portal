from datetime import datetime, timedelta
import hashlib
import importlib.util
import os
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import Member, SyncRun, app, db  # noqa: E402


class FepCurrentMemberPresenceTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["SYNC_API_TOKEN"] = "sync-test-token"
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
        app.config["FEP_API_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    @staticmethod
    def member(member_id):
        return {
            "member_id": str(member_id),
            "name": f"Member, {member_id}",
            "plan_type": "Dreamz 12 months",
            "contract_type": "12-months",
            "billing_status": "ACTIVE",
            "billing_amount": 60.0,
            "is_active": True,
        }

    @staticmethod
    def member_ids_sha256(member_ids):
        canonical_ids = "\n".join(sorted({str(member_id) for member_id in member_ids}))
        return hashlib.sha256(canonical_ids.encode("utf-8")).hexdigest()

    def sync_headers(self):
        return {"X-Sync-Token": "sync-test-token"}

    def fep_headers(self):
        return {"X-FEP-Token": "fep-test-token"}

    def post_sync(self, member_ids, *, member_snapshot=None, warning=None):
        payload = {
            "source": "gymassistant-sync-agent",
            "member_source": r"C:\Gym Assistant 2.6\Members.btx",
            "members": [self.member(member_id) for member_id in member_ids],
        }
        if member_snapshot is not None:
            payload["member_snapshot"] = member_snapshot
        if warning is not None:
            payload["warning"] = warning
        response = self.client.post(
            "/api/sync/members",
            json=payload,
            headers=self.sync_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return db.session.get(SyncRun, response.json["sync_run_id"])

    def complete_snapshot_metadata(self, member_ids, **overrides):
        member_ids = [str(member_id) for member_id in member_ids]
        metadata = {
            "schema_version": 1,
            "scope_complete": True,
            "source_stable_during_read": True,
            "authoritative_for_absence": True,
            "source_count": len(member_ids),
            "sent_count": len(member_ids),
            "unique_count": len(set(member_ids)),
            "member_ids_sha256": self.member_ids_sha256(member_ids),
        }
        metadata.update(overrides)
        return metadata

    def fep_snapshot(self):
        response = self.client.get(
            "/api/fep/member-snapshot",
            headers=self.fep_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json

    def fep_member_ids(self):
        return {
            member["member_id"]
            for member in self.fep_snapshot()["members"]
        }

    def move_sync_run_back(self, sync_run, minutes):
        now = datetime.now()
        sync_run.started_at = now - timedelta(minutes=minutes, seconds=30)
        sync_run.completed_at = now - timedelta(minutes=minutes)
        db.session.commit()

    def test_new_complete_snapshot_replaces_visible_set_without_deleting_history(self):
        first_ids = ["1001", "1002"]
        second_ids = ["1002", "1003"]

        first_run = self.post_sync(
            first_ids,
            member_snapshot=self.complete_snapshot_metadata(first_ids),
        )
        self.assertTrue(first_run.members_snapshot_complete)
        self.assertEqual(self.fep_member_ids(), set(first_ids))

        second_run = self.post_sync(
            second_ids,
            member_snapshot=self.complete_snapshot_metadata(second_ids),
        )

        self.assertTrue(second_run.members_snapshot_complete)
        self.assertEqual(self.fep_member_ids(), set(second_ids))
        self.assertEqual(
            {member.member_id for member in Member.query.order_by(Member.member_id).all()},
            {"1001", "1002", "1003"},
        )
        self.assertIsNotNone(Member.query.filter_by(member_id="1001").one())

    def test_partial_and_explicit_non_authoritative_syncs_preserve_last_complete_set(self):
        reliable_ids = ["2001", "2002"]
        reliable_run = self.post_sync(
            reliable_ids,
            member_snapshot=self.complete_snapshot_metadata(reliable_ids),
        )

        markerless_run = self.post_sync(["2002", "2003"])
        self.assertFalse(markerless_run.members_snapshot_complete)
        markerless_snapshot = self.fep_snapshot()
        self.assertEqual(
            {member["member_id"] for member in markerless_snapshot["members"]},
            set(reliable_ids),
        )
        self.assertEqual(markerless_snapshot["latest_sync"]["id"], reliable_run.id)

        non_authoritative_ids = ["2003", "2004"]
        non_authoritative_run = self.post_sync(
            non_authoritative_ids,
            member_snapshot=self.complete_snapshot_metadata(
                non_authoritative_ids,
                authoritative_for_absence=False,
            ),
        )
        self.assertFalse(non_authoritative_run.members_snapshot_complete)
        non_authoritative_snapshot = self.fep_snapshot()
        self.assertEqual(
            {member["member_id"] for member in non_authoritative_snapshot["members"]},
            set(reliable_ids),
        )
        self.assertEqual(non_authoritative_snapshot["latest_sync"]["id"], reliable_run.id)
        self.assertEqual(
            {member.member_id for member in Member.query.order_by(Member.member_id).all()},
            {"2001", "2002", "2003", "2004"},
        )

    def test_invalid_explicit_metadata_never_replaces_last_complete_set(self):
        reliable_ids = ["3001", "3002"]
        self.post_sync(
            reliable_ids,
            member_snapshot=self.complete_snapshot_metadata(reliable_ids),
        )

        invalid_cases = [
            (
                [],
                self.complete_snapshot_metadata([]),
            ),
            (
                ["3003"],
                self.complete_snapshot_metadata(["3003"], source_count=2),
            ),
            (
                ["3004"],
                self.complete_snapshot_metadata(["3004"], sent_count=2),
            ),
            (
                ["3005"],
                self.complete_snapshot_metadata(["3005"], unique_count=2),
            ),
            (
                ["3006"],
                self.complete_snapshot_metadata(["3006"], member_ids_sha256="0" * 64),
            ),
            (
                ["3007"],
                self.complete_snapshot_metadata(
                    ["3007"],
                    source_stable_during_read=False,
                ),
            ),
        ]

        for member_ids, metadata in invalid_cases:
            with self.subTest(member_ids=member_ids, metadata=metadata):
                sync_run = self.post_sync(member_ids, member_snapshot=metadata)
                self.assertFalse(sync_run.members_snapshot_complete)
                self.assertEqual(self.fep_member_ids(), set(reliable_ids))

    def test_duplicate_member_ids_cannot_be_an_authoritative_snapshot(self):
        reliable_ids = ["4001", "4002"]
        self.post_sync(
            reliable_ids,
            member_snapshot=self.complete_snapshot_metadata(reliable_ids),
        )
        duplicate_ids = ["4003", "4003"]

        sync_run = self.post_sync(
            duplicate_ids,
            member_snapshot=self.complete_snapshot_metadata(duplicate_ids),
        )

        self.assertFalse(sync_run.members_snapshot_complete)
        self.assertEqual(self.fep_member_ids(), set(reliable_ids))
        self.assertIsNotNone(Member.query.filter_by(member_id="4003").one())

    def test_older_overlapping_explicit_sync_cannot_replace_newer_run(self):
        reliable_ids = ["4501", "4502"]
        reliable_run = self.post_sync(
            reliable_ids,
            member_snapshot=self.complete_snapshot_metadata(reliable_ids),
        )

        def start_newer_sync_run():
            db.session.add(
                SyncRun(
                    source="concurrent-test",
                    status="running",
                    members_received=1,
                )
            )
            db.session.commit()

        replacement_ids = ["4502", "4503"]
        with patch(
            "dreamz_portal.acquire_member_sync_advisory_lock",
            side_effect=start_newer_sync_run,
        ):
            older_run = self.post_sync(
                replacement_ids,
                member_snapshot=self.complete_snapshot_metadata(replacement_ids),
            )

        self.assertFalse(older_run.members_snapshot_complete)
        self.assertEqual(older_run.member_snapshot_protocol, "explicit_stale")
        snapshot = self.fep_snapshot()
        self.assertEqual(snapshot["latest_sync"]["id"], reliable_run.id)
        self.assertEqual(
            {member["member_id"] for member in snapshot["members"]},
            set(reliable_ids),
        )

    def test_three_consistent_spaced_legacy_syncs_can_promote_current_set(self):
        legacy_ids = [str(5000 + index) for index in range(120)]

        first_run = self.post_sync(legacy_ids)
        self.assertFalse(first_run.members_snapshot_complete)
        self.move_sync_run_back(first_run, 12)

        second_run = self.post_sync(legacy_ids)
        self.assertFalse(second_run.members_snapshot_complete)
        self.move_sync_run_back(second_run, 6)

        third_run = self.post_sync(legacy_ids)

        self.assertTrue(third_run.members_snapshot_complete)
        snapshot = self.fep_snapshot()
        self.assertEqual(snapshot["latest_sync"]["id"], third_run.id)
        self.assertEqual(snapshot["member_count"], len(legacy_ids))
        self.assertEqual(
            {member["member_id"] for member in snapshot["members"]},
            set(legacy_ids),
        )

    def test_three_highly_overlapping_legacy_syncs_promote_latest_exact_set(self):
        first_ids = [str(8000 + index) for index in range(200)]
        second_ids = first_ids + ["8200"]
        third_ids = second_ids + ["8201"]

        first_run = self.post_sync(first_ids)
        self.move_sync_run_back(first_run, 12)
        second_run = self.post_sync(second_ids)
        self.move_sync_run_back(second_run, 6)
        third_run = self.post_sync(third_ids)

        self.assertTrue(third_run.members_snapshot_complete)
        snapshot = self.fep_snapshot()
        self.assertEqual(snapshot["latest_sync"]["id"], third_run.id)
        self.assertEqual(snapshot["member_count"], len(third_ids))
        self.assertEqual(
            {member["member_id"] for member in snapshot["members"]},
            set(third_ids),
        )

    def test_small_legacy_snapshot_cannot_bypass_recent_high_watermark(self):
        db.session.add(
            SyncRun(
                source="gymassistant-sync-agent",
                status="success",
                started_at=datetime.now() - timedelta(minutes=20),
                completed_at=datetime.now() - timedelta(minutes=19),
                members_received=1000,
            )
        )
        db.session.commit()
        legacy_ids = [str(6000 + index) for index in range(120)]

        first_run = self.post_sync(legacy_ids)
        self.move_sync_run_back(first_run, 12)
        second_run = self.post_sync(legacy_ids)
        self.move_sync_run_back(second_run, 6)
        third_run = self.post_sync(legacy_ids)

        self.assertFalse(third_run.members_snapshot_complete)
        self.assertIn("high-water mark", third_run.member_snapshot_reason)

    def test_sync_payload_cannot_overwrite_internal_snapshot_marker(self):
        reliable_ids = ["7001", "7002"]
        reliable_run = self.post_sync(
            reliable_ids,
            member_snapshot=self.complete_snapshot_metadata(reliable_ids),
        )
        payload = {
            "source": "gymassistant-sync-agent",
            "member_source": r"C:\Gym Assistant 2.6\Members.btx",
            "member_snapshot": {
                "authoritative_for_absence": False,
                "source_count": 1,
                "sent_count": 1,
                "unique_count": 1,
                "member_ids_sha256": self.member_ids_sha256(["7002"]),
            },
            "members": [
                {
                    **self.member("7002"),
                    "gym_snapshot_run_id": 999999,
                }
            ],
        }

        response = self.client.post(
            "/api/sync/members",
            json=payload,
            headers=self.sync_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Member.query.filter_by(member_id="7002").one().gym_snapshot_run_id,
            reliable_run.id,
        )
        self.assertEqual(self.fep_member_ids(), set(reliable_ids))


if __name__ == "__main__":
    unittest.main()
