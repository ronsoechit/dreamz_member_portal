from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
from io import BytesIO
import re
import tempfile
import unittest
from unittest.mock import patch
from werkzeug.security import generate_password_hash


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from cancellation_policy import evaluate_cancellation_policy  # noqa: E402
from translations import LANGUAGES, TRANSLATIONS  # noqa: E402
from dreamz_portal import AppSetting, AgreementCategory, CancellationConfirmation, CancellationRequest, CancellationWindow, CoachActivityLog, CoachInteraction, CoachPlan, CoachProfile, CoachProgressEntry, CoachWorkoutExerciseLog, CoachWorkoutSession, COACH_PLAN_SCHEMA_VERSION, DigitalSignatureAuditTrail, DigitalSignatureRecord, EmailLog, FeatureAccessRule, GroupClassOccurrence, GroupClassSchedule, GroupClassType, LegalDocument, LegalDocumentVersion, LegalTranslation, Member, MemberAgreementAcceptance, MemberClassAttendance, MemberClassPlan, MemberClassPreference, MemberDocument, MemberLoginCode, MemberSignedDocument, MembershipApplication, MembershipApplicationAuditEvent, MembershipApplicationDocument, MembershipApplicationStatus, MembershipApplicationStep, PricingCategory, PricingItem, RequiredAgreementRule, ScheduleChangeNotification, SignedPdfRecord, app, cancellation_message, coach_context_summary, coach_plan_for_member, coach_profile_completion, db, member_access_profile, next_date_for_group_class, pricing_item_access_tags, pricing_visibility_list, seed_feature_access_rules, seed_group_class_schedule, seed_pricing_catalog  # noqa: E402


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
        self.original_coach_upload_root = app.config.get("COACH_UPLOAD_ROOT")
        self.original_coach_force_local_uploads = app.config.get("COACH_FORCE_LOCAL_UPLOADS")
        self.original_coach_ai_mode = app.config.get("COACH_AI_MODE")
        self.original_openai_api_key = app.config.get("OPENAI_API_KEY")
        self.coach_upload_dir = tempfile.TemporaryDirectory()
        app.config["COACH_UPLOAD_ROOT"] = self.coach_upload_dir.name
        app.config["COACH_FORCE_LOCAL_UPLOADS"] = True
        app.config["COACH_AI_MODE"] = "fallback"
        app.config["OPENAI_API_KEY"] = ""
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
        app.config["COACH_UPLOAD_ROOT"] = self.original_coach_upload_root
        app.config["COACH_FORCE_LOCAL_UPLOADS"] = self.original_coach_force_local_uploads
        app.config["COACH_AI_MODE"] = self.original_coach_ai_mode
        app.config["OPENAI_API_KEY"] = self.original_openai_api_key
        self.coach_upload_dir.cleanup()

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

    def login_staff(self, role="admin", username="ron"):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-csrf-token"
            sess["staff_role"] = role
            sess["staff_username"] = username

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

    def test_login_page_shows_password_and_email_code_login(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Email address", body)
        self.assertIn("Password", body)
        self.assertIn("Sign in", body)
        self.assertIn("Send login code", body)
        self.assertIn("data-loading-form", body)
        self.assertIn("Sending code...", body)
        self.assertIn("button.disabled = true", body)
        self.assertNotIn("Birthdate", body)

    def test_translation_catalog_has_exact_four_language_key_parity(self):
        self.assertEqual(set(LANGUAGES), {"en", "nl", "pap", "es"})
        key_sets = {language: set(values) for language, values in TRANSLATIONS.items()}
        expected_keys = key_sets["en"]

        for language in LANGUAGES:
            self.assertEqual(key_sets[language], expected_keys, language)

    def test_group_class_member_page_renders_in_all_languages(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        seed_group_class_schedule()
        self.login_as("13659")

        for language in LANGUAGES:
            with self.client.session_transaction() as sess:
                sess["language"] = language
            response = self.client.get("/group-classes")
            self.assertEqual(response.status_code, 200)
            body = response.get_data(as_text=True)
            self.assertIn(TRANSLATIONS[language]["group_classes_title"], body)
            self.assertIn(TRANSLATIONS[language]["group_class_preferences"], body)

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

    def test_first_code_login_requires_password_setup(self):
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
        self.assertIn("/set-password", response.headers["Location"])
        self.assertIsNotNone(db.session.get(MemberLoginCode, login_code.id).used_at)

    def test_member_can_create_password_after_code_login(self):
        member = self.add_member(member_id="1206", email="member@example.com")
        self.login_as("1206")

        response = self.client.post(
            "/set-password",
            data={"password": "strong-pass-123", "password_confirm": "strong-pass-123", "csrf_token": "test-csrf-token"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?id=1206", response.headers["Location"])
        self.assertTrue(Member.query.get(member.id).password_hash)

    def test_member_can_login_with_password(self):
        self.add_member(
            member_id="1206",
            email="member@example.com",
            password_hash=generate_password_hash("strong-pass-123"),
            password_set_at=datetime.now(),
        )
        token = self.get_login_csrf_token()

        response = self.client.post(
            "/login",
            data={
                "step": "password",
                "email": "member@example.com",
                "password": "strong-pass-123",
                "csrf_token": token,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?id=1206", response.headers["Location"])

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

    def test_member_account_shows_logout_link(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.client.get("/language?lang=pap&next=/login")
        self.login_as("13659")

        response = self.client.get("/account")

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
                sex="male",
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
        self.assertIn("Continue your Dreamz training", body)
        self.assertIn("Start next workout", body)
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
                sex="male",
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
        self.assertIn("Your training cockpit", body)
        self.assertIn("Training week", body)
        self.assertIn("Training calendar", body)
        self.assertIn("Log extra activity", body)
        self.assertIn("Nutrition focus", body)
        self.assertIn("Progress", body)
        self.assertIn("Personal notes", body)
        self.assertIn("Training history", body)
        self.assertIn("Coach conversation", body)
        self.assertIn("Session 1", body)
        self.assertIn("Sets", body)
        self.assertIn("Reps", body)
        self.assertIn("Start session", body)
        self.assertIn("Watch demo", body)
        self.assertIn("Warm-up", body)
        self.assertIn("Finish session", body)
        self.assertIn("Mark this exercise as done before continuing.", body)
        self.assertIn("Bonaire-friendly basics", body)
        self.assertIn("data-save-url", body)
        self.assertIn("data-activity-log-form", body)
        self.assertIn("data-session-tab", body)
        self.assertIn("data-exercise-toggle", body)
        self.assertIn("Use a controlled weight", body)
        self.assertIn("Build muscle", body)

    def test_coach_page_uses_stored_personal_plan(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(
            CoachProfile(
                member_id="13659",
                sex="male",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=2,
                session_minutes=45,
                training_place="dreamz_gym",
                height_cm=165,
                weight_kg=68,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="local food",
                allergies="none",
            )
        )
        db.session.add(
            CoachPlan(
                member_id="13659",
                language="en",
                source="openai",
                plan_version=COACH_PLAN_SCHEMA_VERSION,
                plan_json=json.dumps(
                    [
                        {
                            "title": "Personal training week",
                            "sessions": [
                                {
                                    "number": 1,
                                    "focus": "custom glute and upper strength",
                                    "minutes": 45,
                                    "warmup": "7 minutes easy bike and mobility",
                                    "main": "Train with clean control",
                                    "cooldown": "Stretch hips and chest",
                                    "exercises": [
                                        {
                                            "name": "Custom leg press",
                                            "equipment": "Machine",
                                            "sets": "3",
                                            "reps": "10-12",
                                            "rest": "90 sec",
                                            "load": "Use a controlled load.",
                                            "cue": "Press evenly through both feet.",
                                        }
                                    ],
                                }
                            ],
                        },
                        {"title": "Personal nutrition", "items": ["Use Bonaire-friendly protein meals."]},
                        {"title": "Personal notes", "items": ["Keep meals simple and repeatable."]},
                    ]
                ),
            )
        )
        db.session.commit()
        self.login_as("13659")

        response = self.client.get("/coach")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("custom glute and upper strength", body)
        self.assertIn("Custom leg press", body)
        self.assertIn("Use Bonaire-friendly protein meals.", body)

    def test_saving_coach_profile_clears_existing_plan(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(CoachPlan(member_id="13659", plan_json="[]"))
        db.session.commit()
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="build_muscle",
                sex="male",
                experience_level="intermediate",
                training_days="2",
                session_minutes="45",
                training_place="dreamz_gym",
                height_cm="165",
                weight_kg="68",
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="none",
                allergies="none",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CoachPlan.query.filter_by(member_id="13659").count(), 0)

    def test_pregnancy_profile_fields_are_saved_and_contextualized(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="get_fitter",
                sex="female",
                experience_level="beginner",
                training_days="2",
                session_minutes="45",
                training_place="dreamz_gym",
                height_cm="165",
                weight_kg="70",
                injuries="none",
                nutrition_goal="healthier",
                dietary_preferences="local food",
                allergies="none",
                pregnancy_status="pregnant",
                gestational_weeks="18",
                expected_due_date="2026-10-15",
                pre_pregnancy_weight_kg="65",
                multiple_pregnancy="no",
                provider_cleared_exercise="unknown",
                provider_restrictions="low impact only",
                pregnancy_symptoms=["pelvic_pain"],
                pregnancy_consent="yes",
            ),
        )

        self.assertEqual(response.status_code, 302)
        profile = CoachProfile.query.filter_by(member_id="13659").one()
        self.assertEqual(profile.pregnancy_status, "pregnant")
        self.assertEqual(profile.gestational_weeks, 18)
        self.assertEqual(profile.pre_pregnancy_weight_kg, 65)
        self.assertIn("pelvic_pain", profile.pregnancy_symptoms)

        page = self.client.get("/coach").get_data(as_text=True)
        self.assertIn("Pregnant", page)
        self.assertIn("Medical check advised", page)
        self.assertIn("Medical clearance first", page)
        self.assertIn("Review guidance", page)
        self.assertNotIn("<button type=\"button\" data-start-session", page)

    def test_male_coach_profile_ignores_pregnancy_fields(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="get_fitter",
                sex="male",
                experience_level="beginner",
                training_days="2",
                session_minutes="45",
                training_place="dreamz_gym",
                height_cm="180",
                weight_kg="82",
                injuries="none",
                nutrition_goal="healthier",
                dietary_preferences="local food",
                allergies="none",
                pregnancy_status="pregnant",
                gestational_weeks="18",
                multiple_pregnancy="no",
                provider_cleared_exercise="unknown",
                pregnancy_symptoms=["pelvic_pain"],
                pregnancy_consent="yes",
            ),
        )

        self.assertEqual(response.status_code, 302)
        profile = CoachProfile.query.filter_by(member_id="13659").one()
        self.assertEqual(profile.sex, "male")
        self.assertEqual(profile.pregnancy_status, "not_pregnant")
        self.assertIsNone(profile.gestational_weeks)
        self.assertIsNone(profile.pregnancy_symptoms)
        self.assertFalse(profile.pregnancy_consent)
        self.assertEqual(coach_profile_completion(profile), 100)

        page = self.client.get("/coach").get_data(as_text=True)
        self.assertNotIn("Medical clearance first", page)
        self.assertNotIn("Medical check advised", page)

    def test_female_coach_profile_requires_pregnancy_status(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="get_fitter",
                sex="female",
                experience_level="beginner",
                training_days="2",
                session_minutes="45",
                training_place="dreamz_gym",
                height_cm="165",
                weight_kg="70",
                injuries="none",
                nutrition_goal="healthier",
                dietary_preferences="local food",
                allergies="none",
            ),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachProfile.query.filter_by(member_id="13659").count(), 0)
        self.assertIn("Please complete every coach question", response.get_data(as_text=True))

    def test_pregnancy_profile_requires_consent_and_weeks(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="get_fitter",
                sex="female",
                experience_level="beginner",
                training_days="2",
                session_minutes="45",
                training_place="dreamz_gym",
                height_cm="165",
                weight_kg="70",
                injuries="none",
                nutrition_goal="healthier",
                dietary_preferences="local food",
                allergies="none",
                pregnancy_status="pregnant",
                gestational_weeks="",
                multiple_pregnancy="no",
                provider_cleared_exercise="yes",
            ),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachProfile.query.filter_by(member_id="13659").count(), 0)
        self.assertIn("Please complete the pregnancy safety questions", response.get_data(as_text=True))

    def test_coach_workout_log_can_be_saved(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(CoachPlan(member_id="13659", plan_json="[]"))
        db.session.commit()
        self.login_as("13659")
        with self.client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "csrf-test-token"

        response = self.client.post(
            "/coach/workout-log",
            json={
                "sessionNumber": 1,
                "focus": "lower body muscle building",
                "minutes": 45,
                "exercises": [
                    {
                        "name": "Leg press",
                        "equipment": "Machine",
                        "sets": "3",
                        "reps": "10-12",
                        "rest": "90 sec",
                        "weightUsed": "50",
                        "repsCompleted": "12",
                        "done": True,
                    },
                    {
                        "name": "Hip thrust",
                        "equipment": "Machine or barbell",
                        "sets": "3",
                        "reps": "10-12",
                        "rest": "90 sec",
                        "weightUsed": "40",
                        "repsCompleted": "10",
                        "done": True,
                    },
                ],
            },
            headers={"X-CSRF-Token": "csrf-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("coach_reply", response.get_json())
        self.assertEqual(CoachWorkoutSession.query.filter_by(member_id="13659").count(), 1)
        self.assertEqual(CoachWorkoutExerciseLog.query.filter_by(member_id="13659").count(), 2)
        self.assertEqual(CoachInteraction.query.filter_by(member_id="13659", category="workout_feedback").count(), 1)
        self.assertEqual(CoachPlan.query.filter_by(member_id="13659").count(), 0)
        log = CoachWorkoutExerciseLog.query.filter_by(exercise_name="Leg press").one()
        self.assertEqual(log.weight_used, "50")
        self.assertEqual(log.reps_completed, "12")

    def test_coach_extra_activity_log_can_be_saved(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(CoachPlan(member_id="13659", plan_json="[]"))
        db.session.commit()
        self.login_as("13659")
        with self.client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "csrf-test-token"

        response = self.client.post(
            "/coach/activity-log",
            json={
                "activityType": "run",
                "activityDate": date.today().isoformat(),
                "durationMinutes": 30,
                "intensity": "medium",
                "notes": "Outdoor run",
            },
            headers={"X-CSRF-Token": "csrf-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachActivityLog.query.filter_by(member_id="13659").count(), 1)
        self.assertEqual(CoachInteraction.query.filter_by(member_id="13659", category="activity_log").count(), 1)
        self.assertEqual(CoachPlan.query.filter_by(member_id="13659").count(), 0)
        activity = CoachActivityLog.query.filter_by(member_id="13659").one()
        self.assertEqual(activity.activity_type, "run")
        self.assertEqual(activity.duration_minutes, 30)

    def test_coach_progress_entry_can_be_saved(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(
            CoachProfile(
                member_id="13659",
                sex="male",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=2,
                session_minutes=45,
                training_place="dreamz_gym",
                height_cm=165,
                weight_kg=68,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="none",
                allergies="none",
            )
        )
        db.session.add(CoachPlan(member_id="13659", plan_json="[]"))
        db.session.commit()
        self.login_as("13659")

        response = self.client.post(
            "/coach/progress",
            data=self.csrf_form_data(
                entry_type="before",
                weight_kg="84.5",
                notes="Starting point",
                photo=(BytesIO(b"fake-image-bytes"), "before.jpg"),
            ),
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/coach?view=progress", response.headers["Location"])
        progress = CoachProgressEntry.query.filter_by(member_id="13659").one()
        self.assertEqual(progress.entry_type, "before")
        self.assertEqual(progress.weight_kg, 84.5)
        self.assertTrue(Path(progress.photo_path).exists())
        self.assertEqual(CoachInteraction.query.filter_by(member_id="13659", category="progress_checkin").count(), 1)
        self.assertEqual(CoachPlan.query.filter_by(member_id="13659").count(), 0)
        profile = CoachProfile.query.filter_by(member_id="13659").one()
        self.assertEqual(profile.weight_kg, 84.5)

        photo_response = self.client.get(f"/coach/progress-photo/{progress.id}")
        self.assertEqual(photo_response.status_code, 200)
        self.assertEqual(photo_response.get_data(), b"fake-image-bytes")

        page_response = self.client.get("/coach")
        body = page_response.get_data(as_text=True)
        self.assertIn("Starting point", body)
        self.assertIn("84.5 kg", body)

    def test_coach_workout_log_requires_completed_exercises(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        with self.client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "csrf-test-token"

        response = self.client.post(
            "/coach/workout-log",
            json={
                "sessionNumber": 1,
                "exercises": [{"name": "Leg press", "done": False}],
            },
            headers={"X-CSRF-Token": "csrf-test-token"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(CoachWorkoutSession.query.count(), 0)

    def test_coach_question_can_be_answered_and_logged(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(
            CoachProfile(
                member_id="13659",
                sex="male",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=2,
                session_minutes=45,
                training_place="dreamz_gym",
                height_cm=165,
                weight_kg=68,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="none",
                allergies="none",
            )
        )
        db.session.commit()
        self.login_as("13659")
        with self.client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "csrf-test-token"

        response = self.client.post(
            "/coach/message",
            json={"message": "Leg press felt easy. What should I do?"},
            headers={"X-CSRF-Token": "csrf-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "success")
        self.assertIn("reply", body)
        self.assertEqual(CoachInteraction.query.filter_by(member_id="13659").count(), 2)

    def test_coach_conversation_is_newest_first_and_collapsed(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(
            CoachProfile(
                member_id="13659",
                sex="male",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=2,
                session_minutes=45,
                training_place="dreamz_gym",
                height_cm=165,
                weight_kg=68,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="none",
                allergies="none",
            )
        )
        db.session.add(
            CoachInteraction(
                member_id="13659",
                actor="member",
                message="Older question about training.",
                created_at=datetime(2026, 5, 26, 17, 0),
            )
        )
        db.session.add(
            CoachInteraction(
                member_id="13659",
                actor="coach",
                message="Newest answer about training.",
                created_at=datetime(2026, 5, 26, 21, 0),
            )
        )
        db.session.commit()
        self.login_as("13659")

        response = self.client.get("/coach")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertLess(body.index("Newest answer about training."), body.index("Older question about training."))
        self.assertIn("data-coach-message-card", body)
        self.assertIn("<details", body)

    def test_admin_can_reset_member_coach_data(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        progress_photo = Path(self.coach_upload_dir.name) / "coach-progress" / "13659" / "before.jpg"
        progress_photo.parent.mkdir(parents=True, exist_ok=True)
        progress_photo.write_bytes(b"photo")
        workout = CoachWorkoutSession(
            member_id="13659",
            session_number=1,
            focus="upper body",
            planned_minutes=45,
            completed_at=datetime(2026, 5, 26, 17, 0),
        )
        db.session.add_all(
            [
                CoachProfile(
                    member_id="13659",
                    sex="male",
                    primary_goal="build_muscle",
                    experience_level="intermediate",
                    training_days=2,
                    session_minutes=45,
                    training_place="dreamz_gym",
                    height_cm=165,
                    weight_kg=68,
                    injuries="none",
                    nutrition_goal="muscle_gain",
                    dietary_preferences="none",
                    allergies="none",
                ),
                CoachPlan(member_id="13659", plan_json=json.dumps([{"sessions": []}])),
                CoachInteraction(member_id="13659", actor="member", message="Question"),
                CoachActivityLog(
                    member_id="13659",
                    activity_type="run",
                    activity_date=date(2026, 5, 26),
                    duration_minutes=20,
                ),
                CoachProgressEntry(member_id="13659", photo_path=str(progress_photo), weight_kg=68),
                workout,
            ]
        )
        db.session.flush()
        db.session.add(
            CoachWorkoutExerciseLog(
                workout_session_id=workout.id,
                member_id="13659",
                exercise_order=1,
                exercise_name="Leg press",
                completed=True,
            )
        )
        db.session.commit()
        self.login_staff(role="admin")

        response = self.client.post(
            "/staff/members/13659/coach/reset",
            data=self.csrf_form_data(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/staff/members/13659", response.headers["Location"])
        self.assertEqual(CoachProfile.query.filter_by(member_id="13659").count(), 0)
        self.assertEqual(CoachPlan.query.filter_by(member_id="13659").count(), 0)
        self.assertEqual(CoachInteraction.query.filter_by(member_id="13659").count(), 0)
        self.assertEqual(CoachActivityLog.query.filter_by(member_id="13659").count(), 0)
        self.assertEqual(CoachProgressEntry.query.filter_by(member_id="13659").count(), 0)
        self.assertEqual(CoachWorkoutSession.query.filter_by(member_id="13659").count(), 0)
        self.assertEqual(CoachWorkoutExerciseLog.query.filter_by(member_id="13659").count(), 0)
        self.assertFalse(progress_photo.exists())
        self.assertIsNotNone(Member.query.filter_by(member_id="13659").first())

    def test_manager_cannot_reset_member_coach_data(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(
            CoachProfile(
                member_id="13659",
                sex="male",
                primary_goal="build_muscle",
                experience_level="intermediate",
                training_days=2,
                session_minutes=45,
                training_place="dreamz_gym",
                height_cm=165,
                weight_kg=68,
                injuries="none",
                nutrition_goal="muscle_gain",
                dietary_preferences="none",
                allergies="none",
            )
        )
        db.session.commit()
        self.login_staff(role="manager", username="christel")

        response = self.client.post(
            "/staff/members/13659/coach/reset",
            data=self.csrf_form_data(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(CoachProfile.query.filter_by(member_id="13659").count(), 1)

    def test_coach_profile_requires_all_fields(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")

        response = self.client.post(
            "/coach",
            data=self.csrf_form_data(
                primary_goal="build_muscle",
                sex="male",
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

    def test_account_shows_available_document_view_links(self):
        self.add_member(
            member_id="1206",
            form_path="forms/1206_signup_form.pdf",
            contract_path="contracts/1206_contract.pdf",
            mandate_path="mandates/1206_mandate.pdf",
        )
        self.login_as("1206")

        response = self.client.get("/account")

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
        self.assertIn("/account", body)

    def test_account_shows_member_document_records(self):
        self.add_member(member_id="1206")
        document = self.add_document(member_id="1206")
        self.login_as("1206")

        response = self.client.get("/account")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Group PT / Personal Training", body)
        self.assertIn("Group PT 2022-04-27.pdf", body)
        self.assertIn(f"/documents/item/{document.id}", body)
        self.assertNotIn('target="_blank"', body)

    def test_account_groups_repeated_document_types(self):
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

        response = self.client.get("/account")

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

    def test_dashboard_does_not_surface_payment_alert_without_open_balance(self):
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
        self.assertNotIn("payment-alert-card", body)
        self.assertNotIn("Payment data may be stale", body)
        self.assertNotIn("View payment", body)

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
        self.assertIn("/account", response.headers["Location"])
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

    def test_account_shows_pending_cancellation_after_submission(self):
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

        response = self.client.get("/account")

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

    def test_dashboard_open_balance_is_compact_below_training_focus(self):
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
        self.assertNotIn("payment-alert-card", body)
        self.assertNotIn("Outstanding balance due", body)
        self.assertLess(body.index("home-primary-card"), body.index("Open gym balance"))
        self.assertIn("Open gym balance", body)
        self.assertIn("$63.50", body)
        self.assertIn("Pay at front desk", body)
        self.assertIn("/account?section=gym-balance", body)
        self.assertIn("View balance", body)

    def test_member_nav_shows_account_badge_only_for_open_balance(self):
        self.add_member(member_id="13659", balance=67.50)
        self.login_as("13659")

        response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("account-notification-badge", body)
        self.assertIn('aria-label="Account, 1 account notification"', body)
        self.assertIn('href="/account?section=gym-balance"', body)
        self.assertIn('aria-hidden="true">1</span>', body)
        self.assertNotIn("Account1", re.sub(r"\s+", "", body))

        self.add_member(member_id="1206", balance=0.0)
        self.login_as("1206")

        response = self.client.get("/dashboard?id=1206")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("account-notification-badge", response.get_data(as_text=True))

    def test_view_balance_opens_account_balance_section(self):
        self.add_member(
            member_id="13659",
            balance=67.50,
            next_payment=date(2026, 5, 31),
        )
        self.login_as("13659")

        response = self.client.get("/account?section=gym-balance")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('id="gym-balance"', body)
        self.assertIn("account-target-highlight", body)
        self.assertIn("Open gym balance", body)
        self.assertIn("Gym purchases balance", body)
        self.assertIn("$67.50", body)
        self.assertIn("31 May 2026", body)
        self.assertIn("Pay at front desk", body)
        self.assertIn("Please settle this at the front desk", body)
        self.assertNotIn("Pay now", body)

        legacy_response = self.client.get("/account?section=balance")
        legacy_body = legacy_response.get_data(as_text=True)
        self.assertEqual(legacy_response.status_code, 200)
        self.assertIn('id="gym-balance"', legacy_body)
        self.assertIn("account-target-highlight", legacy_body)

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
        self.assertIn("/account", response.headers["Location"])
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
        self.assertIn("/account", response.headers["Location"])
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

    def test_group_class_schedule_seed_creates_initial_weekly_data(self):
        schedule = seed_group_class_schedule()

        self.assertEqual(schedule.name, "Dreamz Fitness Group Class Schedule")
        self.assertEqual(schedule.timezone, "America/Kralendijk")
        self.assertEqual(schedule.last_updated_from_pdf, date(2026, 5, 18))
        self.assertEqual(GroupClassType.query.count(), 10)
        self.assertEqual(GroupClassOccurrence.query.count(), 24)

        reserved = GroupClassType.query.filter_by(name="RESERVED").one()
        self.assertFalse(reserved.default_bookable)
        self.assertFalse(reserved.default_publish)

        reserved_occurrences = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "RESERVED")
            .all()
        )
        self.assertEqual(len(reserved_occurrences), 2)
        self.assertTrue(all(occurrence.blocks_room for occurrence in reserved_occurrences))
        self.assertTrue(all(not occurrence.is_bookable for occurrence in reserved_occurrences))
        self.assertTrue(all(not occurrence.is_published for occurrence in reserved_occurrences))

        pilates = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "PILATES")
            .one()
        )
        self.assertEqual(pilates.day_of_week, 5)
        self.assertEqual(pilates.start_time.strftime("%H:%M"), "08:00")
        self.assertEqual(pilates.room, "AEROBICS ROOM")
        self.assertEqual(pilates.note, "NEW")

    def test_group_class_schedule_seed_is_idempotent_and_preserves_admin_edits(self):
        seed_group_class_schedule()
        occurrence = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYPUMP", GroupClassOccurrence.day_of_week == 0)
            .order_by(GroupClassOccurrence.start_time.asc())
            .first()
        )
        occurrence.room = "MAIN ROOM"
        occurrence.capacity = 18
        db.session.commit()

        seed_group_class_schedule()

        self.assertEqual(GroupClassType.query.count(), 10)
        self.assertEqual(GroupClassOccurrence.query.count(), 24)
        edited = db.session.get(GroupClassOccurrence, occurrence.id)
        self.assertEqual(edited.room, "MAIN ROOM")
        self.assertEqual(edited.capacity, 18)

    def test_pricing_catalog_seed_creates_initial_data(self):
        seed_pricing_catalog()

        self.assertEqual(PricingCategory.query.count(), 9)
        self.assertEqual(PricingItem.query.count(), 22)
        self.assertEqual(
            AppSetting.query.filter_by(key="pricing_catalog_global_rule").one().value,
            "All prices and fees are non-negotiable.",
        )
        no_contract = PricingItem.query.filter_by(seed_key="membership-no-contract-1-month").one()
        self.assertEqual(no_contract.price_amount, 80)
        self.assertEqual(no_contract.currency, "USD")
        self.assertEqual(no_contract.billing_interval, "per_month")
        self.assertEqual(pricing_visibility_list(no_contract), ["public", "members"])
        self.assertFalse(no_contract.online_payment_available)
        self.assertTrue(no_contract.requires_front_desk_handling)
        self.assertIn("front desk", " ".join(json.loads(no_contract.terms)).lower())

    def test_pricing_catalog_seed_preserves_admin_edits(self):
        seed_pricing_catalog()
        no_contract = PricingItem.query.filter_by(seed_key="membership-no-contract-1-month").one()
        no_contract.price_amount = 88
        no_contract.name = "Custom monthly membership"
        no_contract.visibility = "members"
        db.session.commit()

        seed_pricing_catalog()

        self.assertEqual(PricingItem.query.count(), 22)
        edited = PricingItem.query.filter_by(seed_key="membership-no-contract-1-month").one()
        self.assertEqual(edited.price_amount, 88)
        self.assertEqual(edited.name, "Custom monthly membership")
        self.assertEqual(edited.visibility, "members")

    def test_pricing_catalog_seed_sets_b2b_and_mcb_rules(self):
        seed_pricing_catalog()

        external = PricingItem.query.filter_by(seed_key="b2b-external-personal-trainer-package").one()
        self.assertEqual(external.category_key, "external_trainer_b2b")
        self.assertEqual(external.price_amount, 250)
        self.assertEqual(pricing_visibility_list(external), ["public_business", "staff_only"])
        self.assertFalse(external.member_eligible)
        external_terms = " ".join(json.loads(external.terms))
        self.assertIn("not a membership product", external_terms)
        self.assertIn("responsible for their own clients", external_terms)

        mcb = PricingItem.query.filter_by(seed_key="mcb-direct-debit-12-month-contract").one()
        mcb_terms = " ".join(json.loads(mcb.terms))
        self.assertIn("Current MCB Bank Bonaire accounts only", mcb_terms)
        self.assertIn("Direct Debit only", mcb_terms)
        self.assertIn("No exceptions", mcb_terms)

    def test_agreements_applications_and_signing_models_persist_core_records(self):
        category = AgreementCategory(
            key="contract_renewal_rules",
            name="Contract & Renewal Rules",
            description="Contract, renewal and cancellation legal content.",
        )
        document = LegalDocument(
            document_type="membership_contract_6_months",
            title="6-Month Membership Contract",
            category_key="contract_renewal_rules",
            required_for=json.dumps({"contract_term": "6_months"}),
            legal_review_needed=True,
        )
        db.session.add_all([category, document])
        db.session.flush()

        version = LegalDocumentVersion(
            document_id=document.id,
            version="2026-05-27-draft",
            effective_from=date(2026, 5, 27),
            source_language="en",
            full_legal_text="Six month contract terms.",
            short_summary="Six month contract.",
            plain_language_summary="You commit to six months.",
            pdf_template_key="membership_contract_6_months",
            legal_review_status="draft",
        )
        db.session.add(version)
        db.session.flush()
        db.session.add_all([
            LegalTranslation(
                version_id=version.id,
                language=language,
                title=f"Contract {language}",
                short_summary="Draft summary",
                plain_language_summary="Draft plain language summary",
                full_legal_text="Draft legal translation.",
                translation_status="draft",
            )
            for language in LANGUAGES
        ])

        application = MembershipApplication(
            applicant_first_name="Ron",
            applicant_last_name="Soechit",
            date_of_birth=date(1990, 1, 1),
            email="ron@example.com",
            phone="+5997000000",
            emergency_contact_first_name="Emergency",
            emergency_contact_last_name="Contact",
            emergency_contact_relationship="Family",
            emergency_contact_phone="+5997000001",
            selected_membership_type="6 months contract",
            selected_contract_term="6_months",
            selected_payment_method="mcb_direct_debit_monthly",
            mcb_account_holder_name="Ron Soechit",
            mcb_account_number="123456789",
            mcb_account_type="current",
            direct_debit_confirmed_current_account=True,
            status="submitted",
            language="en",
            submitted_at=datetime(2026, 5, 27, 10, 0),
        )
        db.session.add(application)
        db.session.flush()

        signature = DigitalSignatureRecord(
            application_id=application.id,
            full_legal_name="Ron Soechit",
            email="ron@example.com",
            phone="+5997000000",
            date_of_birth=date(1990, 1, 1),
            signature_text_name="Ron Soechit",
            verification_method="email_link",
            verification_reference="email-token",
            audit_reference_number="DS-20260527-0001",
        )
        db.session.add(signature)
        db.session.flush()

        db.session.add_all([
            RequiredAgreementRule(
                applies_to_contract_term="6_months",
                applies_to_payment_method="mcb_direct_debit_monthly",
                required_legal_document_types=json.dumps(["membership_contract_6_months", "direct_debit_mandate"]),
            ),
            MembershipApplicationStep(application_id=application.id, step_key="required_agreements", status="completed"),
            MembershipApplicationDocument(
                application_id=application.id,
                legal_document_version_id=version.id,
                document_type="membership_contract_6_months",
                status="signed",
                language="en",
            ),
            MembershipApplicationStatus(application_id=application.id, status="submitted", note="Application submitted."),
            MembershipApplicationAuditEvent(application_id=application.id, event_type="application_submitted", actor_type="applicant"),
            DigitalSignatureAuditTrail(signature_record_id=signature.id, event_type="signature_created", message="Signed electronically."),
            MemberAgreementAcceptance(
                member_id="13659",
                legal_document_version_id=version.id,
                language_accepted="en",
                acceptance_method="digital_signature",
                application_id=application.id,
                signature_record_id=signature.id,
                accepted_text_snapshot="Six month contract terms.",
                accepted_text_hash="hash-001",
                pdf_hash="pdf-hash-001",
            ),
            MemberSignedDocument(
                member_id="13659",
                application_id=application.id,
                document_type="membership_contract_6_months",
                file_url="/documents/signed/contract.pdf",
                pdf_hash="pdf-hash-001",
                signed_at=datetime(2026, 5, 27, 10, 5),
                language="en",
                version=version.version,
                status="signed",
            ),
            SignedPdfRecord(
                application_id=application.id,
                member_id="13659",
                document_type="membership_contract_6_months",
                legal_document_version_id=version.id,
                language="en",
                pdf_url="/documents/signed/contract.pdf",
                pdf_hash="pdf-hash-001",
                audit_reference_number="DS-20260527-0001",
                status="generated",
            ),
            CancellationWindow(
                member_id="13659",
                current_term_start_date=date(2026, 1, 1),
                current_term_end_date=date(2026, 7, 1),
                window_open_date=date(2026, 6, 1),
                window_close_date=date(2026, 6, 11),
            ),
        ])
        cancellation = CancellationRequest(
            member_id="13659",
            contract_id="contract-001",
            membership_id="membership-001",
            status="submitted",
            reason="Moving away",
            current_term_start=date(2026, 1, 1),
            current_term_end=date(2026, 7, 1),
            cancellation_window_open_date=date(2026, 6, 1),
            cancellation_window_close_date=date(2026, 6, 11),
            confirmation_number="CAN-20260527-0001",
            pdf_receipt_url="/documents/cancellations/receipt.pdf",
        )
        db.session.add(cancellation)
        db.session.flush()
        db.session.add(CancellationConfirmation(
            cancellation_request_id=cancellation.id,
            confirmation_number="CAN-20260527-0001",
            pdf_receipt_url="/documents/cancellations/receipt.pdf",
            pdf_hash="cancel-pdf-hash",
            language="en",
        ))
        db.session.commit()

        self.assertEqual(LegalTranslation.query.filter_by(version_id=version.id).count(), 4)
        self.assertEqual(MembershipApplication.query.filter_by(status="submitted").count(), 1)
        self.assertEqual(MemberAgreementAcceptance.query.filter_by(member_id="13659").count(), 1)
        self.assertEqual(MemberSignedDocument.query.filter_by(status="signed").count(), 1)
        self.assertEqual(SignedPdfRecord.query.filter_by(audit_reference_number="DS-20260527-0001").count(), 1)
        self.assertEqual(CancellationConfirmation.query.filter_by(confirmation_number="CAN-20260527-0001").count(), 1)

    def test_public_pricing_page_shows_public_catalog_without_staff_only_items(self):
        seed_pricing_catalog()
        db.session.add(PricingItem(
            name="Internal Staff Test Product",
            category_key="fees_other",
            price_amount=999,
            currency="USD",
            billing_interval="one_time",
            visibility="staff_only",
            is_active=True,
            sort_order=999,
            terms=json.dumps(["Internal only"]),
        ))
        db.session.commit()

        response = self.client.get("/pricing")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Dreamz Fitness Pricing", body)
        self.assertIn("No contract / 1 month", body)
        self.assertIn("1 Day Pass", body)
        self.assertIn("Delfins Resort Guests", body)
        self.assertIn("First-time registration fee", body)
        self.assertIn("External Personal Trainer Package", body)
        self.assertIn("This is a business package for external personal trainers", body)
        self.assertIn("All prices and fees are non-negotiable.", body)
        self.assertIn("Contact front desk", body)
        self.assertNotIn("Internal Staff Test Product", body)
        self.assertNotIn("Pay now", body)

    def test_public_pricing_page_renders_in_all_supported_languages(self):
        seed_pricing_catalog()
        for language in LANGUAGES:
            with self.subTest(language=language):
                with self.client.session_transaction() as sess:
                    sess["language"] = language
                response = self.client.get("/pricing")
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn("No contract / 1 month", body)
                self.assertIn(TRANSLATIONS[language]["public_pricing_title"], body)
                self.assertIn(TRANSLATIONS[language]["contact_front_desk"], body)

    def test_member_pricing_options_require_login(self):
        response = self.client.get("/membership-options")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_member_pricing_options_show_relevant_member_catalog(self):
        self.add_member(
            member_id="13659",
            name="Ron Soechit",
            plan_type="contract Dreamz 6 months",
            contract_type="6-months",
            billing_amount=70,
            balance=67.50,
        )
        self.login_as("13659")
        seed_pricing_catalog()

        response = self.client.get("/membership-options")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Membership options", body)
        self.assertIn("contract Dreamz 6 months", body)
        self.assertIn("No contract / 1 month", body)
        self.assertIn("6 months contract - MCB Direct Debit", body)
        self.assertIn("Add-on Group PT 5x a week", body)
        self.assertIn("First-time registration fee", body)
        self.assertIn("Membership changes and payments are currently handled", body)
        self.assertIn("/account?section=gym-balance", body)
        self.assertIn("/pricing", body)
        self.assertNotIn("External Personal Trainer Package", body)
        self.assertNotIn("Pay now", body)

    def test_account_links_to_member_pricing_options(self):
        self.add_member(member_id="13659")
        self.login_as("13659")

        response = self.client.get("/account")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Membership options", body)
        self.assertIn("/membership-options", body)
        self.assertLess(body.index("Membership options"), body.index("Documents"))
        self.assertLess(body.index("Membership options"), body.index("Password and security"))
        self.assertIn("No documents available yet.", body)
        self.assertNotIn("Documents 0", body)

    def test_feature_access_rule_seed_creates_premium_readiness_foundation(self):
        seed_feature_access_rules()

        levels = {rule.access_level for rule in FeatureAccessRule.query.all()}
        self.assertEqual(
            levels,
            {
                "public_basic",
                "member",
                "contract_member",
                "no_contract_member",
                "staff",
                "admin",
                "internal_test",
                "premium_future",
            },
        )
        self.assertTrue(
            FeatureAccessRule.query.filter_by(
                key="premium_future_features",
                access_level="premium_future",
                is_future_ready=True,
            ).one()
        )

        seed_feature_access_rules()

        self.assertEqual(FeatureAccessRule.query.count(), 8)

    def test_member_access_profile_distinguishes_contract_and_no_contract(self):
        contract_member = self.add_member(
            member_id="13659",
            plan_type="contract Dreamz 12 m",
            contract_type="12-months",
        )
        no_contract_member = self.add_member(
            member_id="1206",
            plan_type="no contract 1 month",
            contract_type="No-Contract",
        )

        self.assertEqual(member_access_profile(contract_member), ["contract_member", "member"])
        self.assertEqual(member_access_profile(no_contract_member), ["member", "no_contract_member"])

    def test_pricing_item_access_tags_do_not_treat_b2b_package_as_member_upgrade(self):
        seed_pricing_catalog()
        external = PricingItem.query.filter_by(seed_key="b2b-external-personal-trainer-package").one()
        contract = PricingItem.query.filter_by(seed_key="membership-6-month-contract").one()

        self.assertEqual(pricing_item_access_tags(external), ["public_basic", "staff"])
        self.assertIn("member", pricing_item_access_tags(contract))
        self.assertIn("contract_member", pricing_item_access_tags(contract))

    def test_pricing_catalog_final_qa_visibility_and_frontdesk_copy(self):
        self.add_member(member_id="13659", plan_type="no contract 1 month", contract_type="No-Contract")
        seed_pricing_catalog()
        db.session.add(PricingItem(
            name="Staff-only premium experiment",
            category_key="fees_other",
            price_amount=123,
            currency="USD",
            billing_interval="one_time",
            visibility="staff_only",
            is_active=True,
            member_eligible=False,
            sort_order=1000,
            terms=json.dumps(["Internal test only"]),
        ))
        db.session.commit()

        public_response = self.client.get("/pricing")
        self.login_as("13659")
        member_response = self.client.get("/membership-options")
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
        admin_response = self.client.get("/staff/pricing-products")

        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(member_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        public_body = public_response.get_data(as_text=True)
        member_body = member_response.get_data(as_text=True)
        admin_body = admin_response.get_data(as_text=True)
        self.assertIn("External Personal Trainer Package", public_body)
        self.assertNotIn("External Personal Trainer Package", member_body)
        self.assertNotIn("Staff-only premium experiment", public_body)
        self.assertNotIn("Staff-only premium experiment", member_body)
        self.assertIn("Staff-only premium experiment", admin_body)
        self.assertIn("Contact front desk", public_body)
        self.assertIn("Membership changes and payments are currently handled", member_body)
        self.assertNotIn("Pay now", public_body + member_body + admin_body)

    def test_group_class_member_plan_attendance_and_notifications_models(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        seed_group_class_schedule()
        bodypump = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYPUMP")
            .first()
        )
        preference = MemberClassPreference(
            member_id="13659",
            class_type_id=bodypump.class_type_id,
            preferred_classes_per_week=2,
            plan_mode="replace_or_supplement",
            is_favorite=True,
        )
        plan = MemberClassPlan(
            member_id="13659",
            occurrence_id=bodypump.id,
            class_date=date(2026, 6, 1),
            status="planned",
        )
        db.session.add_all([preference, plan])
        db.session.commit()

        attendance = MemberClassAttendance(
            member_id="13659",
            plan_id=plan.id,
            occurrence_id=bodypump.id,
            class_date=plan.class_date,
            source="member",
        )
        notification = ScheduleChangeNotification(
            member_id="13659",
            occurrence_id=bodypump.id,
            plan_id=plan.id,
            change_type="class_time_changed",
            message_key="group_class_time_changed",
            message="BODYPUMP on Monday has moved to 20:00.",
            old_value="18:00",
            new_value="20:00",
        )
        db.session.add_all([attendance, notification])
        db.session.commit()

        self.assertEqual(MemberClassPreference.query.filter_by(member_id="13659").count(), 1)
        self.assertEqual(MemberClassPlan.query.filter_by(member_id="13659").count(), 1)
        self.assertEqual(MemberClassAttendance.query.filter_by(member_id="13659").count(), 1)
        self.assertEqual(ScheduleChangeNotification.query.filter_by(member_id="13659").count(), 1)

    def test_member_can_view_group_classes_and_save_preferences(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        seed_group_class_schedule()
        bodypump = GroupClassType.query.filter_by(name="BODYPUMP").one()

        response = self.client.get("/group-classes")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Group Classes", body)
        self.assertIn("BODYPUMP", body)
        self.assertNotIn("RESERVED", body)

        response = self.client.post(
            "/group-classes/preferences",
            data=self.csrf_form_data(
                preferred_classes_per_week="2",
                plan_mode="replace_or_supplement",
                favorite_class_type_id=[str(bodypump.id)],
            ),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        aggregate = MemberClassPreference.query.filter_by(member_id="13659", class_type_id=None).one()
        self.assertEqual(aggregate.preferred_classes_per_week, 2)
        self.assertEqual(aggregate.plan_mode, "replace_or_supplement")
        self.assertTrue(MemberClassPreference.query.filter_by(member_id="13659", class_type_id=bodypump.id).one().is_favorite)

    def test_member_can_plan_attend_and_remove_group_classes(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        seed_group_class_schedule()
        bodypump = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYPUMP", GroupClassOccurrence.is_bookable.is_(True))
            .first()
        )
        class_date = next_date_for_group_class(bodypump.day_of_week)

        response = self.client.post(
            "/group-classes/plan",
            data=self.csrf_form_data(occurrence_id=str(bodypump.id), class_date=class_date.isoformat()),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        plan = MemberClassPlan.query.filter_by(member_id="13659", occurrence_id=bodypump.id, class_date=class_date).one()
        self.assertEqual(plan.status, "planned")

        response = self.client.post(
            "/group-classes/attendance",
            data=self.csrf_form_data(plan_id=str(plan.id), occurrence_id=str(bodypump.id), class_date=class_date.isoformat()),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.get(MemberClassPlan, plan.id).status, "attended")
        self.assertEqual(MemberClassAttendance.query.filter_by(member_id="13659", occurrence_id=bodypump.id).count(), 1)

        second_class = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "ZUMBA", GroupClassOccurrence.is_bookable.is_(True))
            .first()
        )
        second_date = next_date_for_group_class(second_class.day_of_week)
        removable = MemberClassPlan(member_id="13659", occurrence_id=second_class.id, class_date=second_date)
        db.session.add(removable)
        db.session.commit()

        response = self.client.post(
            f"/group-classes/plan/{removable.id}/remove",
            data=self.csrf_form_data(),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(db.session.get(MemberClassPlan, removable.id))

    def test_dashboard_and_progress_include_group_class_activity(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        seed_group_class_schedule()
        bodypump = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYPUMP", GroupClassOccurrence.is_bookable.is_(True))
            .first()
        )
        today = date.today()
        db.session.add(MemberClassAttendance(
            member_id="13659",
            occurrence_id=bodypump.id,
            class_date=today,
        ))
        db.session.commit()

        dashboard_response = self.client.get("/dashboard?id=13659")
        progress_response = self.client.get("/progress")

        self.assertEqual(dashboard_response.status_code, 200)
        self.assertIn("Today at Dreamz", dashboard_response.get_data(as_text=True))
        self.assertEqual(progress_response.status_code, 200)
        progress_body = progress_response.get_data(as_text=True)
        self.assertIn("Classes this week", progress_body)
        self.assertIn(">1</strong>", progress_body)

    def test_dashboard_group_classes_split_upcoming_and_earlier_by_portal_time(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        seed_group_class_schedule()
        local_now = datetime(2026, 5, 27, 13, 48, tzinfo=timezone(timedelta(hours=-4)))

        with patch("dreamz_portal.current_portal_datetime", return_value=local_now):
            response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Upcoming classes today", body)
        self.assertIn("18:00", body)
        self.assertIn("ZUMBA", body)
        self.assertIn("Upcoming", body)
        self.assertIn("Earlier today", body)
        self.assertIn("BODYPUMP", body)
        self.assertIn("Ended", body)
        self.assertLess(body.index("Upcoming classes today"), body.index("Earlier today"))

    def test_dashboard_group_classes_marks_live_class(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        seed_group_class_schedule()
        local_now = datetime(2026, 5, 27, 8, 30, tzinfo=timezone(timedelta(hours=-4)))

        with patch("dreamz_portal.current_portal_datetime", return_value=local_now):
            response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("BODYPUMP", body)
        self.assertIn("Live now", body)
        self.assertIn("YOGA", body)
        self.assertIn("Upcoming", body)

    def test_dashboard_group_classes_no_more_classes_after_schedule_ends(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        self.login_as("13659")
        seed_group_class_schedule()
        local_now = datetime(2026, 5, 30, 12, 30, tzinfo=timezone(timedelta(hours=-4)))

        with patch("dreamz_portal.current_portal_datetime", return_value=local_now):
            response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("No more classes today", body)
        self.assertIn("View full schedule", body)
        self.assertIn("Earlier today", body)
        self.assertIn("Ended", body)
        self.assertNotIn("Upcoming classes today", body)

    def test_dashboard_keeps_member_home_focused_and_navigation_compact(self):
        self.add_member(member_id="13659", name="Ron Soechit", balance=0)
        self.login_as("13659")
        seed_group_class_schedule()

        response = self.client.get("/dashboard?id=13659")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        nav_order = [body.index(label) for label in ["Dashboard", "My Coach", "Progress", "Account"]]
        self.assertEqual(nav_order, sorted(nav_order))
        self.assertIn("Today at Dreamz", body)
        self.assertIn("My Coach", body)
        self.assertNotIn("Documents 0", body)
        self.assertNotIn("Password and security", body)
        self.assertNotIn("Submit cancellation request", body)

    def test_coach_context_includes_group_classes_without_male_pregnancy_context(self):
        member = self.add_member(member_id="13659", name="Ron Soechit")
        profile = CoachProfile(
            member_id="13659",
            primary_goal="build_muscle",
            experience_level="intermediate",
            training_days=4,
            session_minutes=45,
            training_place="dreamz_gym",
            height_cm=180,
            weight_kg=82,
            injuries="none",
            nutrition_goal="muscle_gain",
            dietary_preferences="local food",
            allergies="none",
            sex="male",
            pregnancy_status="not_pregnant",
        )
        db.session.add(profile)
        seed_group_class_schedule()
        bodypump = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYPUMP", GroupClassOccurrence.is_bookable.is_(True))
            .first()
        )
        class_date = next_date_for_group_class(bodypump.day_of_week)
        db.session.add(MemberClassPlan(member_id="13659", occurrence_id=bodypump.id, class_date=class_date))
        db.session.commit()

        context = coach_context_summary(member, profile)

        self.assertIn("planned_group_classes_this_week", context)
        self.assertIn("BODYPUMP", context)
        self.assertIn("strength_load=high", context)
        self.assertIn("biological_sex=male", context)
        self.assertNotIn("pregnancy_safety_level", context)
        self.assertNotIn("pregnancy_status=not_pregnant", context)

    def test_coach_context_includes_pregnancy_class_safety_for_pregnant_female(self):
        member = self.add_member(member_id="13659", name="Ron Soechit")
        profile = CoachProfile(
            member_id="13659",
            primary_goal="health",
            experience_level="beginner",
            training_days=3,
            session_minutes=45,
            training_place="dreamz_gym",
            height_cm=165,
            weight_kg=74,
            injuries="none",
            nutrition_goal="healthier",
            dietary_preferences="local food",
            allergies="none",
            sex="female",
            pregnancy_status="pregnant",
            gestational_weeks=18,
            multiple_pregnancy="no",
            provider_cleared_exercise="unknown",
            pregnancy_consent=True,
        )
        db.session.add(profile)
        seed_group_class_schedule()
        combat = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYCOMBAT", GroupClassOccurrence.is_bookable.is_(True))
            .first()
        )
        db.session.add(MemberClassPlan(member_id="13659", occurrence_id=combat.id, class_date=next_date_for_group_class(combat.day_of_week)))
        db.session.commit()

        context = coach_context_summary(member, profile)

        self.assertIn("biological_sex=female, pregnancy_status=pregnant", context)
        self.assertIn("BODYCOMBAT", context)
        self.assertIn("pregnancy_safety_level=not_recommended_or_requires_modification", context)

    def test_coach_plan_prompt_context_records_group_class_load(self):
        member = self.add_member(member_id="13659", name="Ron Soechit")
        profile = CoachProfile(
            member_id="13659",
            primary_goal="get_fitter",
            experience_level="intermediate",
            training_days=3,
            session_minutes=45,
            training_place="dreamz_gym",
            height_cm=178,
            weight_kg=80,
            injuries="none",
            nutrition_goal="healthier",
            dietary_preferences="local food",
            allergies="none",
            sex="male",
            pregnancy_status="not_pregnant",
        )
        db.session.add(profile)
        seed_group_class_schedule()
        spinning = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "SPINNING", GroupClassOccurrence.is_bookable.is_(True))
            .first()
        )
        class_date = next_date_for_group_class(spinning.day_of_week)
        db.session.add(MemberClassPlan(member_id="13659", occurrence_id=spinning.id, class_date=class_date))
        db.session.add(MemberClassAttendance(member_id="13659", occurrence_id=spinning.id, class_date=class_date))
        db.session.commit()

        plan = coach_plan_for_member(member, profile, language="en", force=True)
        record = CoachPlan.query.filter_by(member_id="13659").one()

        self.assertTrue(plan)
        self.assertIn("SPINNING", record.prompt_context)
        self.assertIn("cardio_load=high", record.prompt_context)
        self.assertIn("attended_group_classes_this_week", record.prompt_context)
        self.assertNotIn("pregnancy_safety_level", record.prompt_context)


if __name__ == "__main__":
    unittest.main()
