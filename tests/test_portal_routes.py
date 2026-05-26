from datetime import date, timedelta
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from cancellation_policy import evaluate_cancellation_policy  # noqa: E402
from dreamz_portal import CancellationRequest, CoachProfile, EmailLog, Member, MemberDocument, MemberLoginCode, app, cancellation_message, db  # noqa: E402


class FakeS3Body:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunks(self):
        return iter(self._chunks)


class PortalRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.original_attachments_root = app.config.get("GYM_ASSISTANT_ATTACHMENTS_ROOT")
        self.original_document_cache_root = app.config.get("DOCUMENT_CACHE_ROOT")
        self.original_photos_root = app.config.get("GYM_ASSISTANT_PHOTOS_ROOT")
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        app.config["GYM_ASSISTANT_ATTACHMENTS_ROOT"] = self.original_attachments_root
        app.config["DOCUMENT_CACHE_ROOT"] = self.original_document_cache_root
        app.config["GYM_ASSISTANT_PHOTOS_ROOT"] = self.original_photos_root

    def add_member(self, member_id="1206", **overrides):
        data = {
            "member_id": member_id,
            "name": "Example Member",
            "email": "member@example.com",
            "plan_type": "no contract 1 month",
            "contract_type": "No-Contract",
            "billing_amount": 80.0,
            "balance": 0.0,
            "last_payment_amount": 80.0,
        }
        data.update(overrides)
        member = Member(**data)
        db.session.add(member)
        db.session.commit()
        return member

    def add_document(self, member_id="1206", **overrides):
        data = {
            "member_id": member_id,
            "document_type": "group_pt",
            "title": "Group PT / Personal Training",
            "path": "contracts/1206_contract.pdf",
            "source_filename": "Group PT 2022-04-27.pdf",
            "display_order": 0,
        }
        data.update(overrides)
        document = MemberDocument(**data)
        db.session.add(document)
        db.session.commit()
        return document

    def login_as(self, member_id):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf-token"
            sess["member_id"] = member_id

    def csrf_form_data(self, **data):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf-token"
        return {"csrf_token": "test-csrf-token", **data}

    def get_login_csrf_token(self):
        response = self.client.get("/login")
        body = response.get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_login_page_shows_email_code_login(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Email address", body)
        self.assertIn("Send login code", body)
        self.assertIn("data-loading-form", body)
        self.assertIn("Sending code...", body)
        self.assertIn("button.disabled = true", body)
        self.assertNotIn("Password", body)
        self.assertNotIn("Birthdate", body)

    def test_login_page_shows_language_choices(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("English", body)
        self.assertIn("Nederlands", body)
        self.assertIn("Papiamentu", body)
        self.assertIn("Español", body)

    def test_language_choice_is_stored_in_session(self):
        response = self.client.get("/language?lang=es&next=/login")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["language"], "es")

        response = self.client.get("/login")
        self.assertIn("Acceso de Miembro", response.get_data(as_text=True))

    def test_dutch_language_choice_translates_login(self):
        self.client.get("/language?lang=nl&next=/login")

        response = self.client.get("/login")

        self.assertIn("Leden Login", response.get_data(as_text=True))
        self.assertIn("E-mailadres", response.get_data(as_text=True))

    def test_login_with_valid_email_code_redirects_to_dashboard(self):
        self.add_member(member_id="1206", email="member@example.com")
        token = self.get_login_csrf_token()

        response = self.client.post("/login", data={"step": "email", "email": "member@example.com", "csrf_token": token})

        self.assertEqual(response.status_code, 302)
        login_code = MemberLoginCode.query.filter_by(email="member@example.com").one()
        with self.client.session_transaction() as sess:
            code = sess["dev_login_code"]
            sess["_csrf_token"] = "test-csrf-token"

        response = self.client.post("/login", data={"step": "code", "code": code, "csrf_token": "test-csrf-token"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?id=1206", response.headers["Location"])
        self.assertIsNotNone(db.session.get(MemberLoginCode, login_code.id).used_at)

    def test_repeated_login_code_request_does_not_send_multiple_codes(self):
        self.add_member(member_id="1206", email="member@example.com")
        token = self.get_login_csrf_token()

        first = self.client.post("/login", data={"step": "email", "email": "member@example.com", "csrf_token": token})
        second = self.client.post("/login", data={"step": "email", "email": "member@example.com", "csrf_token": token})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(MemberLoginCode.query.filter_by(email="member@example.com").count(), 1)
        self.assertEqual(EmailLog.query.filter_by(to_addresses="member@example.com").count(), 1)

    def test_login_code_sent_message_uses_success_style(self):
        self.add_member(member_id="1206", email="member@example.com")
        token = self.get_login_csrf_token()

        response = self.client.post(
            "/login",
            data={"step": "email", "email": "member@example.com", "csrf_token": token},
            follow_redirects=True,
        )

        body = response.get_data(as_text=True)
        self.assertIn("If this email is registered, we sent a login code.", body)
        self.assertIn("text-green-200", body)

    def test_unknown_email_message_uses_error_style(self):
        token = self.get_login_csrf_token()

        response = self.client.post(
            "/login",
            data={"step": "email", "email": "unknown@example.com", "csrf_token": token},
            follow_redirects=True,
        )

        body = response.get_data(as_text=True)
        self.assertIn("We could not verify this login. Please contact Dreamz Fitness.", body)
        self.assertIn("text-red-200", body)

    def test_member_dashboard_shows_logout_link(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.client.get("/language?lang=pap&next=/login")
        self.login_as("13659")

        response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('/logout"', body)
        self.assertIn("Sali", body)

    def test_member_dashboard_links_to_my_coach(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("/coach", body)
        self.assertIn("Your personal Dreamz trainer", body)

    def test_member_dashboard_shows_ready_coach_copy_when_profile_complete(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(
            CoachProfile(
                member_id="13659",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=4,
                session_minutes=60,
                training_place="dreamz_gym",
                height_cm=180,
                weight_kg=85,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="local food",
                allergies="none",
            )
        )
        db.session.commit()
        self.login_as("13659")

        response = self.client.get("/dashboard?id=13659")

        body = response.get_data(as_text=True)
        self.assertIn("Your Dreamz plan is ready", body)
        self.assertIn("View My Coach", body)
        self.assertNotIn("Set your goals, training rhythm", body)

    def test_coach_page_requires_member_login(self):
        response = self.client.get("/coach")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_coach_profile_can_be_saved(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days="4",
                session_minutes="60",
                training_place="dreamz_gym",
                height_cm="180",
                weight_kg="85.5",
                injuries="No injuries",
                nutrition_goal="muscle_gain",
                dietary_preferences="Local food",
                allergies="None",
            ),
        )

        self.assertEqual(response.status_code, 302)
        profile = CoachProfile.query.filter_by(member_id="13659").one()
        self.assertEqual(profile.primary_goal, "build_muscle")
        self.assertEqual(profile.training_days, 4)
        self.assertEqual(profile.weight_kg, 85.5)

        response = self.client.get("/coach")
        body = response.get_data(as_text=True)
        self.assertIn("Coach profile", body)
        self.assertIn("Review your Dreamz guidance below", body)
        self.assertNotIn("Complete your profile step by step.", body)
        self.assertIn("data-edit-coach", body)
        self.assertIn('id="coach-wizard" method="post" class="hidden space-y-5"', body)
        self.assertIn("Your Dreamz coach plan", body)
        self.assertIn("Training week", body)
        self.assertIn("Nutrition focus", body)
        self.assertIn("Personal notes", body)
        self.assertIn("Session 1", body)
        self.assertIn("Sets", body)
        self.assertIn("Reps", body)
        self.assertIn("Start session", body)
        self.assertIn("Watch demo", body)
        self.assertIn("data-session-tab", body)
        self.assertIn("data-exercise-toggle", body)
        self.assertIn("Use a controlled weight", body)
        self.assertIn("Build muscle", body)

    def test_coach_profile_requires_all_fields(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days="4",
                session_minutes="60",
                training_place="dreamz_gym",
                height_cm="180",
                weight_kg="85.5",
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="",
                allergies="none",
            ),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachProfile.query.filter_by(member_id="13659").count(), 0)
        self.assertIn("Please complete every coach question.", response.get_data(as_text=True))

    def test_member_logout_clears_session(self):
        self.add_member(member_id="13659")
        self.login_as("13659")

        response = self.client.get("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with self.client.session_transaction() as sess:
            self.assertNotIn("member_id", sess)

    def test_login_code_email_uses_selected_language(self):
        self.add_member(member_id="1206", name="Ron Soechit", email="member@example.com")
        self.client.get("/language?lang=pap&next=/login")
        token = self.get_login_csrf_token()

        response = self.client.post(
            "/login",
            data={"step": "email", "email": "member@example.com", "csrf_token": token},
        )

        self.assertEqual(response.status_code, 302)
        email = EmailLog.query.one()
        self.assertIn("kodigo di login", email.subject)
        self.assertIn("Estima Ron Soechit", email.body)
        self.assertIn("portal di miembro", email.html_body)

    def test_login_with_unknown_email_is_neutral(self):
        token = self.get_login_csrf_token()

        response = self.client.post("/login", data={"step": "email", "email": "unknown@example.com", "csrf_token": token})

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn("pending_login_email", sess)

    def test_login_rejects_invalid_code(self):
        self.add_member(member_id="1206", email="member@example.com")
        token = self.get_login_csrf_token()
        self.client.post("/login", data={"step": "email", "email": "member@example.com", "csrf_token": token})
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf-token"

        response = self.client.post("/login", data={"step": "code", "code": "000000", "csrf_token": "test-csrf-token"})

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn("member_id", sess)

    def test_login_rejects_missing_csrf_token(self):
        self.add_member(member_id="1206")

        response = self.client.post("/login", data={"step": "email", "email": "member@example.com"})

        self.assertEqual(response.status_code, 400)

    def test_dashboard_requires_matching_session_member_id(self):
        self.add_member(member_id="1206")

        response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_dashboard_shows_available_document_view_links(self):
        self.add_member(
            member_id="1206",
            form_path="forms/1206_signup_form.pdf",
            contract_path="contracts/1206_contract.pdf",
            mandate_path="mandates/1206_mandate.pdf",
        )
        self.login_as("1206")

        response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Signup Form", body)
        self.assertIn("/documents/signup-form", body)
        self.assertIn("/documents/contract", body)
        self.assertIn("/documents/mandate", body)
        self.assertIn(">View</a>", body)
        self.assertNotIn('target="_blank"', body)

    def test_document_viewer_requires_login(self):
        response = self.client.get("/documents/contract")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_document_viewer_embeds_available_document(self):
        self.add_member(member_id="1206", contract_path="contracts/1206_contract.pdf")
        self.login_as("1206")

        response = self.client.get("/documents/contract")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Contract", body)
        self.assertIn("/documents/contract/file#toolbar=0", body)
        self.assertIn("/documents/contract/file?download=1", body)
        self.assertIn("/dashboard?id=1206", body)

    def test_dashboard_shows_member_document_records(self):
        self.add_member(member_id="1206")
        document = self.add_document(member_id="1206")
        self.login_as("1206")

        response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Group PT / Personal Training", body)
        self.assertIn("Group PT 2022-04-27.pdf", body)
        self.assertIn(f"/documents/item/{document.id}", body)
        self.assertNotIn('target="_blank"', body)

    def test_dashboard_groups_repeated_document_types(self):
        self.add_member(member_id="80")
        first = self.add_document(
            member_id="80",
            document_type="signup_form",
            title="Signup Form",
            source_filename="inscrip form 2024-01-08.pdf",
        )
        second = self.add_document(
            member_id="80",
            document_type="signup_form",
            title="Signup Form",
            source_filename="inscrip form 2025-01-03.pdf",
            display_order=1,
        )
        contract = self.add_document(
            member_id="80",
            document_type="contract",
            title="Contract",
            source_filename="Contract 2025-01-03.pdf",
            display_order=2,
        )
        self.login_as("80")

        response = self.client.get("/dashboard?id=80")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Signup Form (2):", body)
        self.assertEqual(body.count("Signup Form"), 1)
        self.assertIn("inscrip form 2024-01-08.pdf", body)
        self.assertIn("inscrip form 2025-01-03.pdf", body)
        self.assertIn("Contract:", body)
        self.assertIn(f"/documents/item/{first.id}", body)
        self.assertIn(f"/documents/item/{second.id}", body)
        self.assertIn(f"/documents/item/{contract.id}", body)

    def test_member_document_file_serves_pdf_for_logged_in_owner(self):
        self.add_member(member_id="1206")
        document = self.add_document(member_id="1206", path="contracts/1206_contract.pdf")
        self.login_as("1206")

        response = self.client.get(f"/documents/item/{document.id}/file")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")

    def test_member_document_file_serves_s3_pdf_for_logged_in_owner(self):
        self.add_member(member_id="1206")
        document = self.add_document(member_id="1206", path="s3://dreamz-test/portal/Data/Attachments/0001206/contract.pdf")
        self.login_as("1206")

        with patch("dreamz_portal.open_s3_object", return_value=FakeS3Body([b"%PDF s3"])):
            response = self.client.get(f"/documents/item/{document.id}/file")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertEqual(response.get_data(), b"%PDF s3")

    def test_member_document_file_rejects_other_member(self):
        self.add_member(member_id="1206")
        self.add_member(member_id="2204")
        document = self.add_document(member_id="2204", path="contracts/1206_contract.pdf")
        self.login_as("1206")

        response = self.client.get(f"/documents/item/{document.id}/file")

        self.assertEqual(response.status_code, 404)

    def test_dashboard_shows_member_photo_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            photos_root = Path(tmp)
            photo = photos_root / "0001206.jpg"
            photo.write_bytes(b"fake-jpg")
            app.config["GYM_ASSISTANT_PHOTOS_ROOT"] = str(photos_root)
            self.add_member(member_id="1206", photo_path=str(photo))
            self.login_as("1206")

            response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/member-photo/1206", response.get_data(as_text=True))

    def test_dashboard_shows_s3_member_photo_when_available(self):
        self.add_member(member_id="1206", photo_path="s3://dreamz-test/portal/Data/Pictures/0001206.jpg")
        self.login_as("1206")

        response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/member-photo/1206", response.get_data(as_text=True))

    def test_member_photo_serves_image_for_logged_in_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            photos_root = Path(tmp)
            photo = photos_root / "0001206.jpg"
            photo.write_bytes(b"fake-jpg")
            app.config["GYM_ASSISTANT_PHOTOS_ROOT"] = str(photos_root)
            self.add_member(member_id="1206", photo_path=str(photo))
            self.login_as("1206")

            response = self.client.get("/member-photo/1206")
            response.close()

        self.assertEqual(response.status_code, 200)

    def test_member_photo_serves_s3_image_for_logged_in_owner(self):
        self.add_member(member_id="1206", photo_path="s3://dreamz-test/portal/Data/Pictures/0001206.jpg")
        self.login_as("1206")

        with patch("dreamz_portal.open_s3_object", return_value=FakeS3Body([b"photo"])):
            response = self.client.get("/member-photo/1206")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b"photo")

    def test_member_photo_rejects_other_member(self):
        self.add_member(member_id="1206")
        self.add_member(member_id="2204", photo_path=r"D:\Data\Pictures\0002204.jpg")
        self.login_as("1206")

        response = self.client.get("/member-photo/2204")

        self.assertEqual(response.status_code, 404)

    def test_dashboard_warns_when_payment_data_is_stale(self):
        self.add_member(
            member_id="1206",
            last_payment=date(2025, 3, 29),
            next_payment=date(2025, 5, 1),
            balance=0.0,
        )
        self.login_as("1206")

        response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Payment data may be stale", body)
        self.assertIn("payment information may not be up to date", body)

    def test_document_file_serves_pdf_for_logged_in_member(self):
        self.add_member(member_id="1206", contract_path="contracts/1206_contract.pdf")
        self.login_as("1206")

        response = self.client.get("/documents/contract/file")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")

    def test_document_file_serves_allowed_external_pdf_for_logged_in_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "0001206" / "CNTR + DD 2022-09-19.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-1.4")
            app.config["GYM_ASSISTANT_ATTACHMENTS_ROOT"] = str(root)
            self.add_member(member_id="1206", contract_path=str(pdf))
            self.login_as("1206")

            response = self.client.get("/documents/contract/file")
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")

    def test_document_file_serves_relative_generated_pdf_for_logged_in_member(self):
        with tempfile.TemporaryDirectory(dir=Path(app.instance_path)) as tmp:
            generated_root = Path(tmp)
            pdf = generated_root / "0001206" / "contract.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.4")
            app.config["DOCUMENT_CACHE_ROOT"] = str(generated_root)
            self.add_member(member_id="1206", contract_path=os.path.relpath(pdf, Path.cwd()))
            self.login_as("1206")

            response = self.client.get("/documents/contract/file")
            response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")

    def test_document_viewer_404_when_document_not_available(self):
        self.add_member(member_id="1206")
        self.login_as("1206")

        response = self.client.get("/documents/contract")

        self.assertEqual(response.status_code, 404)

    def test_cancel_requires_matching_session_member_id(self):
        self.add_member(member_id="1206")

        with patch("dreamz_portal.send_cancel_email") as send_mail:
            response = self.client.post("/cancel", data=self.csrf_form_data(member_id="1206"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        send_mail.assert_not_called()

    def test_cancel_rejects_missing_csrf_token(self):
        self.add_member(member_id="1206")
        self.login_as("1206")

        response = self.client.post("/cancel", data={"member_id": "1206"})

        self.assertEqual(response.status_code, 400)

    def test_blocked_cancellation_message_starts_with_next_window(self):
        policy = evaluate_cancellation_policy(
            today=date(2026, 5, 24),
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            contract_begin=date(2022, 9, 14),
            contract_end=date(2023, 9, 14),
        )

        message = cancellation_message(policy)

        self.assertTrue(
            message.startswith(
                "The next cancellation window is from 15 August 2026 through 24 August 2026."
            )
        )
        self.assertIn("renews automatically under the same conditions", message)

    def test_blocked_cancellation_message_can_be_dutch(self):
        policy = evaluate_cancellation_policy(
            today=date(2026, 5, 24),
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            contract_begin=date(2022, 9, 14),
            contract_end=date(2023, 9, 14),
        )

        message = cancellation_message(policy, language="nl")

        self.assertTrue(
            message.startswith(
                "Het volgende opzegvenster is van 15 augustus 2026 tot en met 24 augustus 2026."
            )
        )
        self.assertIn("automatisch verlengd onder dezelfde voorwaarden", message)

    def test_cancel_in_open_fixed_term_window_sends_email(self):
        today = date.today()
        self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=335),
            end_date=today + timedelta(days=30),
            signup_date=today - timedelta(days=335),
        )
        self.login_as("1206")

        with patch("dreamz_portal.send_cancel_email") as send_mail:
            response = self.client.post(
                "/cancel",
                data=self.csrf_form_data(member_id="1206", reason="Moving away"),
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?id=1206", response.headers["Location"])
        send_mail.assert_called_once()
        request_record = CancellationRequest.query.filter_by(member_id="1206").one()
        self.assertEqual(request_record.status, "accepted")
        self.assertEqual(request_record.reason, "Moving away")
        self.assertEqual(request_record.policy_status, "allowed_in_window")
        self.assertEqual(request_record.mail_status, "sent")
        self.assertEqual(request_record.term_months, 12)
        self.assertEqual(request_record.current_term_end, today + timedelta(days=30))

    def test_cancel_other_reason_stores_custom_text(self):
        today = date.today()
        self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=335),
            end_date=today + timedelta(days=30),
            signup_date=today - timedelta(days=335),
        )
        self.login_as("1206")

        with patch("dreamz_portal.send_cancel_email", return_value="sent"):
            response = self.client.post(
                "/cancel",
                data=self.csrf_form_data(
                    member_id="1206",
                    reason="Other",
                    other_reason="Moving to another island",
                ),
            )

        self.assertEqual(response.status_code, 302)
        request_record = CancellationRequest.query.filter_by(member_id="1206").one()
        self.assertEqual(request_record.reason, "Moving to another island")

    def test_cancel_requires_reason(self):
        today = date.today()
        self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=335),
            end_date=today + timedelta(days=30),
            signup_date=today - timedelta(days=335),
        )
        self.login_as("1206")

        with patch("dreamz_portal.send_cancel_email") as send_mail:
            response = self.client.post("/cancel", data=self.csrf_form_data(member_id="1206"))

        self.assertEqual(response.status_code, 302)
        send_mail.assert_not_called()
        self.assertEqual(CancellationRequest.query.filter_by(member_id="1206").count(), 0)

    def test_dashboard_shows_pending_cancellation_after_submission(self):
        today = date.today()
        member = self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=335),
            end_date=today + timedelta(days=30),
            signup_date=today - timedelta(days=335),
        )
        db.session.add(CancellationRequest(
            member_id=member.member_id,
            status="accepted",
            reason="Moving away",
            policy_status="allowed_in_window",
            admin_status="new",
            member_name=member.name,
            member_email=member.email,
        ))
        db.session.commit()
        self.login_as("1206")

        response = self.client.get("/dashboard?id=1206")

        body = response.get_data(as_text=True)
        self.assertIn("Cancellation request received", body)
        self.assertIn("final only after you receive confirmation by email", body)
        self.assertNotIn("I want to cancel my contract", body)

    def test_staff_membership_hides_cancellation_policy(self):
        self.add_member(
            member_id="13659",
            name="Ron Soechit",
            plan_type="Medewerker",
            contract_type="No-Contract",
            signup_date=date(2022, 10, 19),
        )
        self.login_as("13659")

        response = self.client.get("/dashboard?id=13659")

        body = response.get_data(as_text=True)
        self.assertNotIn("Cancellation Policy", body)
        self.assertNotIn("I want to cancel my contract", body)

    def test_short_pass_hides_cancellation_policy(self):
        self.add_member(
            member_id="34844",
            name="Gerard V Donzelaar",
            plan_type="2 WEEKS PASS",
            contract_type="No-Contract",
            signup_date=date(2026, 5, 25),
            start_date=date(2026, 5, 25),
        )
        self.login_as("34844")

        response = self.client.get("/dashboard?id=34844")

        body = response.get_data(as_text=True)
        self.assertNotIn("Cancellation Policy", body)
        self.assertNotIn("Cancellation request is available now", body)
        self.assertNotIn("I want to cancel my contract", body)

    def test_non_contract_member_hides_customer_cancellation_policy(self):
        self.add_member(
            member_id="80",
            name="Sharon Gonzalez",
            plan_type="OLB Bedrijfsport",
            contract_type="No-Contract",
            signup_date=date(2011, 11, 3),
            start_date=date(2015, 7, 9),
        )
        self.login_as("80")

        response = self.client.get("/dashboard?id=80")

        body = response.get_data(as_text=True)
        self.assertNotIn("Cancellation Policy", body)
        self.assertNotIn("Cancellation request is available now", body)
        self.assertNotIn("I want to cancel my contract", body)

    def test_staff_membership_cannot_submit_cancellation(self):
        self.add_member(
            member_id="13659",
            plan_type="Medewerker",
            contract_type="No-Contract",
        )
        self.login_as("13659")

        response = self.client.post(
            "/cancel",
            data=self.csrf_form_data(member_id="13659", reason="Moving away"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CancellationRequest.query.filter_by(member_id="13659").count(), 0)

    def test_dashboard_open_balance_message_is_explicit(self):
        self.add_member(
            member_id="13659",
            plan_type="Medewerker",
            contract_type="No-Contract",
            balance=63.50,
            next_payment=date.today() + timedelta(days=7),
        )
        self.login_as("13659")

        response = self.client.get("/dashboard?id=13659")

        body = response.get_data(as_text=True)
        self.assertIn("Outstanding balance due", body)
        self.assertIn("$63.50", body)
        self.assertIn("Please pay this before", body)

    def test_cancel_before_fixed_term_window_notifies_admin(self):
        today = date.today()
        self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=305),
            end_date=today + timedelta(days=60),
            signup_date=today - timedelta(days=305),
        )
        self.login_as("1206")

        with patch("dreamz_portal.send_cancel_email") as send_mail:
            response = self.client.post(
                "/cancel",
                data=self.csrf_form_data(member_id="1206", reason="Moving away"),
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?id=1206", response.headers["Location"])
        send_mail.assert_called_once()
        request_record = CancellationRequest.query.filter_by(member_id="1206").one()
        self.assertEqual(request_record.status, "blocked")
        self.assertEqual(request_record.policy_status, "blocked_too_early")
        self.assertEqual(request_record.mail_status, "sent")

    def test_cancel_after_fixed_term_window_notifies_admin(self):
        today = date.today()
        self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=345),
            end_date=today + timedelta(days=20),
            signup_date=today - timedelta(days=345),
        )
        self.login_as("1206")

        with patch("dreamz_portal.send_cancel_email") as send_mail:
            response = self.client.post(
                "/cancel",
                data=self.csrf_form_data(member_id="1206", reason="Moving away"),
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?id=1206", response.headers["Location"])
        send_mail.assert_called_once()
        request_record = CancellationRequest.query.filter_by(member_id="1206").one()
        self.assertEqual(request_record.status, "blocked")
        self.assertEqual(request_record.policy_status, "blocked_window_closed")
        self.assertEqual(request_record.mail_status, "sent")

    def test_cancel_records_mail_failure_after_accepting_request(self):
        today = date.today()
        self.add_member(
            member_id="1206",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
            start_date=today - timedelta(days=335),
            end_date=today + timedelta(days=30),
            signup_date=today - timedelta(days=335),
        )
        self.login_as("1206")

        with patch("dreamz_portal.send_cancel_email", side_effect=RuntimeError("SMTP offline")):
            response = self.client.post(
                "/cancel",
                data=self.csrf_form_data(member_id="1206", reason="Other"),
            )

        self.assertEqual(response.status_code, 302)
        request_record = CancellationRequest.query.filter_by(member_id="1206").one()
        self.assertEqual(request_record.status, "accepted")
        self.assertEqual(request_record.mail_status, "failed")
        self.assertIn("SMTP offline", request_record.mail_error)


if __name__ == "__main__":
    unittest.main()
