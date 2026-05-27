import calendar
import html
import json
import os
import platform
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

if os.name == "nt":
    platform.machine = lambda: (
        os.getenv("PROCESSOR_ARCHITEW6432")
        or os.getenv("PROCESSOR_ARCHITECTURE")
        or "AMD64"
    )

from flask import (
    Flask, render_template, request, abort,
    redirect, url_for, flash, session, Response, send_file, send_from_directory, jsonify
)

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from dateutil.relativedelta import relativedelta
from datetime import datetime, date, timedelta   # ← bestaande regel uitbreiden
from datetime import timezone
from cancellation_policy import evaluate_cancellation_policy
from ga_fields import GA_FIELDS
from translations import (
    LANGUAGES,
    LANGUAGE_FLAGS,
    MONTH_NAMES,
    DEFAULT_LANGUAGE,
    normalize_language,
    translate,
)
from storage_backend import is_s3_uri, open_s3_object, parse_s3_uri, s3_bucket_name, s3_client, s3_download_name, s3_object_exists, upload_bytes_to_s3

import csv
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from io import StringIO
from pathlib import Path
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


def normalize_database_uri(database_uri, default_database_path):
    if not database_uri:
        return f"sqlite:///{default_database_path}"
    if not database_uri.startswith("sqlite:///"):
        return database_uri

    sqlite_path = database_uri.removeprefix("sqlite:///")
    if sqlite_path == ":memory:" or os.path.isabs(sqlite_path):
        return database_uri

    absolute_path = os.path.abspath(sqlite_path).replace("\\", "/")
    return f"sqlite:///{absolute_path}"


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

os.makedirs(app.instance_path, exist_ok=True)
default_database_path = os.path.join(app.instance_path, "app.db").replace("\\", "/")
app.config["SQLALCHEMY_DATABASE_URI"] = normalize_database_uri(
    os.getenv("DATABASE_URL"),
    default_database_path,
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["STAFF_TOKEN"] = os.getenv("STAFF_TOKEN")
app.config["SYNC_API_TOKEN"] = os.getenv("SYNC_API_TOKEN")
app.config["STAFF_ADMIN_USERNAME"] = os.getenv("STAFF_ADMIN_USERNAME", "ron")
app.config["STAFF_ADMIN_PASSWORD"] = os.getenv("STAFF_ADMIN_PASSWORD", "dreamz-admin-dev")
app.config["STAFF_MANAGER_USERNAME"] = os.getenv("STAFF_MANAGER_USERNAME", "manager")
app.config["STAFF_MANAGER_PASSWORD"] = os.getenv("STAFF_MANAGER_PASSWORD", "dreamz-manager-dev")
app.config["STAFF_ADMIN_EMAIL"] = os.getenv("STAFF_ADMIN_EMAIL", "ron@dreamzfitness.com")
app.config["FEP_MANAGER_URL"] = os.getenv("FEP_MANAGER_URL", "https://dreamz-fep.onrender.com/login")
app.config["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "")
app.config["COACH_AI_MODE"] = os.getenv("COACH_AI_MODE") or ("openai" if app.config["OPENAI_API_KEY"] else "fallback")
app.config["OPENAI_MODEL"] = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
app.config["EMAIL_DELIVERY_MODE"] = os.getenv("EMAIL_DELIVERY_MODE", "log")
app.config["MEMBER_LOGIN_CODE_TTL_MINUTES"] = int(os.getenv("MEMBER_LOGIN_CODE_TTL_MINUTES", "15"))
app.config["DIRECT_DEBIT_DAY"] = int(os.getenv("DIRECT_DEBIT_DAY", "28"))
app.config["SESSION_COOKIE_NAME"] = "dreamz_member_portal_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["GYM_ASSISTANT_ATTACHMENTS_ROOT"] = os.getenv(
    "GYM_ASSISTANT_ATTACHMENTS_ROOT",
    r"D:\Dreamz Fitness\Gym Assistant 2.6\Data\Attachments",
)
app.config["GYM_ASSISTANT_PHOTOS_ROOT"] = os.getenv(
    "GYM_ASSISTANT_PHOTOS_ROOT",
    r"D:\Dreamz Fitness\Gym Assistant 2.6\Data\Pictures",
)
app.config["DOCUMENT_CACHE_ROOT"] = os.getenv(
    "DOCUMENT_CACHE_ROOT",
    os.path.join(app.instance_path, "generated_documents"),
)
app.config["COACH_UPLOAD_ROOT"] = os.getenv(
    "COACH_UPLOAD_ROOT",
    os.path.join(app.instance_path, "coach_uploads"),
)
app.config["COACH_FORCE_LOCAL_UPLOADS"] = os.getenv("COACH_FORCE_LOCAL_UPLOADS", "").lower() in ("1", "true", "yes")
db = SQLAlchemy(app)

DOCUMENT_TYPES = {
    "signup-form": {
        "field": "form_path",
        "label": "Signup Form",
        "filename": "signup-form.pdf",
    },
    "contract": {
        "field": "contract_path",
        "label": "Contract",
        "filename": "contract.pdf",
    },
    "mandate": {
        "field": "mandate_path",
        "label": "Direct Debit Mandate",
        "filename": "direct-debit-mandate.pdf",
    },
}

AUDIT_ISSUE_LABELS = {
    "missing_email": "Missing Email",
    "missing_phone": "Missing Phone",
    "missing_photo": "Missing Photo",
    "missing_signup_form": "Missing Signup Form",
    "missing_contract": "Missing Contract",
    "missing_direct_debit_mandate": "Missing Direct Debit Mandate",
    "contract_dates_incomplete": "Contract Dates Incomplete",
    "missing_payment_data": "Missing Payment Data",
    "stale_payment_data": "Stale Payment Data",
    "overdue": "Overdue",
    "due_today": "Due Today",
    "balance_open": "Balance Open",
}
AUDIT_PAGE_SIZE = 500
SYNC_PAGE_SIZE = 20

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, unique=True, nullable=False)
    name = db.Column(db.String)
    email = db.Column(db.String)
    phone = db.Column(db.String)
    birthdate = db.Column(db.Date)
    plan_type = db.Column(db.String)
    contract_type = db.Column(db.String)
    billing_status = db.Column(db.String)
    billing_option = db.Column(db.String)
    billing_type = db.Column(db.String)
    start_date = db.Column(db.Date)
    start_date_raw = db.Column(db.String)
    start_date_marker = db.Column(db.String)
    end_date = db.Column(db.Date)
    end_date_raw = db.Column(db.String)
    end_date_marker = db.Column(db.String)
    signup_date = db.Column(db.Date)
    signup_date_raw = db.Column(db.String)
    signup_date_marker = db.Column(db.String)
    last_payment = db.Column(db.Date)
    last_payment_raw = db.Column(db.String)
    last_payment_marker = db.Column(db.String)
    next_payment = db.Column(db.Date)
    last_payment_amount = db.Column(db.Float)
    balance = db.Column(db.Float)
    is_active = db.Column(db.Boolean)
    form_path = db.Column(db.String)
    contract_path = db.Column(db.String)
    mandate_path = db.Column(db.String)
    cancellation_policy = db.Column(db.Text)
    billing_amount   = db.Column(db.Float)
    due_date         = db.Column(db.Date)
    due_date_raw     = db.Column(db.String)
    due_date_marker  = db.Column(db.String)
    mobile           = db.Column(db.String)
    visits           = db.Column(db.Integer)
    photo_path       = db.Column(db.String)


class MemberDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    document_type = db.Column(db.String, nullable=False, index=True)
    title = db.Column(db.String, nullable=False)
    path = db.Column(db.String, nullable=False)
    source_filename = db.Column(db.String)
    display_order = db.Column(db.Integer, default=0, nullable=False)


class CancellationRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    requested_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    status = db.Column(db.String, nullable=False)
    reason = db.Column(db.Text)

    policy_status = db.Column(db.String)
    policy_reason = db.Column(db.Text)
    term_months = db.Column(db.Integer)
    current_term_start = db.Column(db.Date)
    current_term_end = db.Column(db.Date)
    window_open = db.Column(db.Date)
    window_close_exclusive = db.Column(db.Date)
    last_request_date = db.Column(db.Date)
    next_window_open = db.Column(db.Date)
    next_window_last_request_date = db.Column(db.Date)

    mail_status = db.Column(db.String, default="not_sent", nullable=False)
    mail_error = db.Column(db.Text)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    notification_to = db.Column(db.Text)
    notification_cc = db.Column(db.Text)
    notification_bcc = db.Column(db.Text)

    member_name = db.Column(db.String)
    member_email = db.Column(db.String)
    plan_type = db.Column(db.String)
    contract_type = db.Column(db.String)
    admin_status = db.Column(db.String, default="new", nullable=False)
    handled_by = db.Column(db.String)
    handled_at = db.Column(db.DateTime)
    staff_note = db.Column(db.Text)
    confirmed_at = db.Column(db.DateTime)
    confirmation_subject = db.Column(db.String)
    confirmation_body = db.Column(db.Text)
    last_paid_date = db.Column(db.Date)
    access_until = db.Column(db.Date)


class StaffUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String, unique=True, nullable=False)
    role = db.Column(db.String, nullable=False)
    email = db.Column(db.String)
    password_hash = db.Column(db.String, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True, nullable=False)
    value = db.Column(db.Text)


class EmailLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    delivery_mode = db.Column(db.String, nullable=False)
    status = db.Column(db.String, default="pending", nullable=False)
    to_addresses = db.Column(db.Text)
    cc_addresses = db.Column(db.Text)
    bcc_addresses = db.Column(db.Text)
    subject = db.Column(db.String)
    body = db.Column(db.Text)
    html_body = db.Column(db.Text)
    error = db.Column(db.Text)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.String)


class SyncRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String)
    started_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    completed_at = db.Column(db.DateTime)
    status = db.Column(db.String, default="received", nullable=False)
    members_received = db.Column(db.Integer, default=0, nullable=False)
    members_new = db.Column(db.Integer, default=0, nullable=False)
    members_updated = db.Column(db.Integer, default=0, nullable=False)
    documents_received = db.Column(db.Integer, default=0, nullable=False)
    change_summary = db.Column(db.Text)
    error = db.Column(db.Text)


class MemberLoginCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    email = db.Column(db.String, nullable=False, index=True)
    code_hash = db.Column(db.String, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    attempts = db.Column(db.Integer, default=0, nullable=False)


class CoachProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, unique=True, nullable=False, index=True)
    primary_goal = db.Column(db.String)
    experience_level = db.Column(db.String)
    training_days = db.Column(db.Integer)
    session_minutes = db.Column(db.Integer)
    training_place = db.Column(db.String)
    home_equipment = db.Column(db.Text)
    height_cm = db.Column(db.Float)
    weight_kg = db.Column(db.Float)
    injuries = db.Column(db.Text)
    nutrition_goal = db.Column(db.String)
    dietary_preferences = db.Column(db.Text)
    allergies = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class CoachWorkoutSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    session_number = db.Column(db.Integer)
    focus = db.Column(db.String)
    planned_minutes = db.Column(db.Integer)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    started_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    completed_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class CoachActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    activity_type = db.Column(db.String, nullable=False)
    activity_date = db.Column(db.Date, nullable=False, index=True)
    duration_minutes = db.Column(db.Integer)
    intensity = db.Column(db.String)
    notes = db.Column(db.Text)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class CoachProgressEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    entry_type = db.Column(db.String, default="progress", nullable=False)
    weight_kg = db.Column(db.Float)
    photo_path = db.Column(db.String)
    photo_mimetype = db.Column(db.String)
    notes = db.Column(db.Text)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class CoachWorkoutExerciseLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workout_session_id = db.Column(db.Integer, db.ForeignKey("coach_workout_session.id"), nullable=False, index=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    exercise_order = db.Column(db.Integer, nullable=False)
    exercise_name = db.Column(db.String, nullable=False)
    equipment = db.Column(db.String)
    planned_sets = db.Column(db.String)
    planned_reps = db.Column(db.String)
    planned_rest = db.Column(db.String)
    weight_used = db.Column(db.String)
    reps_completed = db.Column(db.String)
    completed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class CoachInteraction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    actor = db.Column(db.String, nullable=False)
    category = db.Column(db.String, default="conversation", nullable=False, index=True)
    source = db.Column(db.String, default="portal", nullable=False)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    message = db.Column(db.Text, nullable=False)
    context_summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class CoachPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, unique=True, nullable=False, index=True)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    source = db.Column(db.String, default="fallback")
    plan_version = db.Column(db.String, default="2026-05-26b")
    plan_json = db.Column(db.Text, nullable=False)
    prompt_context = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


DEFAULT_SETTINGS = {
    "admin_email": "ron@dreamzfitness.com",
    "notification_to": "ron@dreamzfitness.com",
    "notification_cc": "",
    "always_cc_admin": "1",
}

DEFAULT_PORTAL_TIMEZONE_OFFSET_HOURS = -4
STALE_SYNC_RUN_MINUTES = 15
LOGIN_CODE_RESEND_COOLDOWN_SECONDS = 60
COACH_PLAN_SCHEMA_VERSION = "2026-05-26b"


def portal_timezone():
    offset_hours = int(os.getenv("PORTAL_TIMEZONE_OFFSET_HOURS", DEFAULT_PORTAL_TIMEZONE_OFFSET_HOURS))
    return timezone(timedelta(hours=offset_hours), name="Dreamz local time")


def local_datetime(value):
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(portal_timezone())


def ensure_sqlite_model_column(table_name, column_name, column_definition):
    if db.engine.dialect.name != "sqlite":
        return

    from sqlalchemy import text

    rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    if any(row[1] == column_name for row in rows):
        return
    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"))
    db.session.commit()


def ensure_model_column(table_name, column_name, column_definition):
    from sqlalchemy import text

    if db.engine.dialect.name == "sqlite":
        ensure_sqlite_model_column(table_name, column_name, column_definition)
        return
    if db.engine.dialect.name == "postgresql":
        db.session.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_definition}"
        ))
        db.session.commit()


def ensure_runtime_schema():
    if app.config.get("_RUNTIME_SCHEMA_READY") and not app.config.get("TESTING"):
        return

    db.create_all()
    ensure_sqlite_model_column("cancellation_request", "notification_to", "TEXT")
    ensure_sqlite_model_column("cancellation_request", "notification_cc", "TEXT")
    ensure_model_column("cancellation_request", "notification_bcc", "TEXT")
    ensure_sqlite_model_column("cancellation_request", "admin_status", "VARCHAR DEFAULT 'new' NOT NULL")
    ensure_sqlite_model_column("cancellation_request", "handled_by", "VARCHAR")
    ensure_sqlite_model_column("cancellation_request", "handled_at", "DATETIME")
    ensure_sqlite_model_column("cancellation_request", "staff_note", "TEXT")
    ensure_sqlite_model_column("cancellation_request", "confirmed_at", "DATETIME")
    ensure_sqlite_model_column("cancellation_request", "confirmation_subject", "VARCHAR")
    ensure_sqlite_model_column("cancellation_request", "confirmation_body", "TEXT")
    ensure_sqlite_model_column("cancellation_request", "last_paid_date", "DATE")
    ensure_sqlite_model_column("cancellation_request", "access_until", "DATE")
    ensure_model_column("cancellation_request", "language", "VARCHAR")
    ensure_sqlite_model_column("email_log", "html_body", "TEXT")
    ensure_model_column("email_log", "bcc_addresses", "TEXT")
    ensure_sqlite_model_column("email_log", "reviewed_at", "DATETIME")
    ensure_sqlite_model_column("email_log", "reviewed_by", "VARCHAR")
    ensure_model_column("sync_run", "change_summary", "TEXT")
    ensure_model_column("coach_profile", "home_equipment", "TEXT")
    ensure_model_column("coach_plan", "plan_version", "VARCHAR")
    db.create_all()
    seed_default_settings()
    seed_default_staff_users()
    app.config["_RUNTIME_SCHEMA_READY"] = True


def setting_value(key, default=None):
    setting = AppSetting.query.filter_by(key=key).first()
    if setting is None or setting.value in (None, ""):
        return default
    return setting.value


def set_setting_value(key, value):
    setting = AppSetting.query.filter_by(key=key).first()
    if not setting:
        setting = AppSetting(key=key)
        db.session.add(setting)
    setting.value = value


def seed_default_settings():
    for key, value in DEFAULT_SETTINGS.items():
        if not AppSetting.query.filter_by(key=key).first():
            db.session.add(AppSetting(key=key, value=value))
    db.session.commit()


def seed_default_staff_users():
    defaults = [
        {
            "username": app.config["STAFF_ADMIN_USERNAME"],
            "password": app.config["STAFF_ADMIN_PASSWORD"],
            "role": "admin",
            "email": app.config["STAFF_ADMIN_EMAIL"],
        },
        {
            "username": app.config["STAFF_MANAGER_USERNAME"],
            "password": app.config["STAFF_MANAGER_PASSWORD"],
            "role": "manager",
            "email": "",
        },
    ]
    for data in defaults:
        if StaffUser.query.filter_by(username=data["username"]).first():
            continue
        user = StaffUser(
            username=data["username"],
            role=data["role"],
            email=data["email"],
            password_hash=generate_password_hash(data["password"]),
            is_active=True,
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()


@app.before_request
def prepare_runtime_schema():
    ensure_runtime_schema()


# lijst met labels in de volgorde van je export
GA_LABELS = [
    "Member Number", "Member Name", "Membership Type",
    "Billing Amount", "Due Date", "Contract Begin", "Contract End",
    "Signup Date", "Last Pd Date", "Last Pd Amount",
    "Mobile", "Email", "Current Balance",
]

def grouped_fields(member):
    """Return three dicts for the template: info, subscription, payment."""
    fld = {key: getattr(member, key, None) for _,_,key,_ in GA_FIELDS}

    info = {
        "Member ID":  fld["member_id"],
        "Name":       fld["name"],
        "Email":      fld["email"]  or "Not available",
        "Phone":      member.phone  or "Not available",
        "Mobile":     fld["mobile"] or "Not available",
        "Visits":     fld["visits"] or "Not available",
    }

    sub = {
        "Plan Type":      fld["plan_type"],
        "Contract Type":  member.contract_type,
        "Signup Date":    fld["signup_date"],
        "Contract Begin": fld["start_date"],
        "Contract End":   ("Automatic Renewal"
                           if member.contract_type in ("6-months","12-months")
                           else fld["end_date"]),
        "Due Date":       fld["due_date"],
    }

    pay = {
        "Billing Amount":      fld["billing_amount"]   or 0,
        "Last Payment":        fld["last_payment"],
        "Last Paid Amount":    fld["last_payment_amount"] or 0,
        "Next Payment":        compute_next_payment(member),
        "Outstanding Balance": fld["balance"] or 0,
    }
    return info, sub, pay

def compute_next_payment(m):
    """Return the next automatic collection date."""
    today = date.today()

    # 0) if Gym Assistant already supplied a date → trust that and exit
    if m.next_payment:
        return m.next_payment

    # a) contract members (6- or 12-months): always the 28-th
    if m.contract_type in ("6-months", "12-months"):
        np = date(today.year, today.month, 28)
        if np <= today:                    # already past this month
            np += relativedelta(months=1)  # → jump to next month
        return np

    # b) flex members: 30 days after last payment, else after signup
    anchor = m.last_payment or m.signup_date or today
    return anchor + timedelta(days=30)

def _legacy_renewal_window_unused(member):
    """
    Geeft tuple (renewal_date, open_date, close_date).
      • Alleen voor 6- of 12-maands contracten met signup_date.
      • open_date  = 30 d vóór renewal
      • close_date =  9 d vóór renewal  (dus 3 weken venster)
    Anders (None, None, None).
    """
    if member.contract_type not in ("6-months", "12-months") or not member.signup_date:
        return None, None, None

    months = 6 if member.contract_type.startswith("6") else 12
    renew  = member.signup_date
    today  = date.today()
    while renew <= today:
        renew += relativedelta(months=months)

    open_date  = renew - timedelta(days=30)
    close_date = renew - timedelta(days=9)
    return renew, open_date, close_date

def cancellation_policy_for_member(member, today=None):
    return evaluate_cancellation_policy(
        today=today or date.today(),
        plan_type=member.plan_type,
        contract_type=member.contract_type,
        contract_begin=member.start_date,
        contract_end=member.end_date,
        signup_date=member.signup_date,
    )

def fmt_policy_date(value, language=DEFAULT_LANGUAGE):
    if not value:
        return None
    language = normalize_language(language)
    month_names = MONTH_NAMES.get(language, MONTH_NAMES[DEFAULT_LANGUAGE])
    return f"{value.day:02d} {month_names[value.month - 1]} {value.year}"

def translated_text(key, language=DEFAULT_LANGUAGE, **values):
    text = translate(key, language)
    return text.format(**values) if values else text


DOCUMENT_TRANSLATION_KEYS = {
    "signup_form": "document_type_signup_form",
    "contract": "document_type_contract",
    "direct_debit_mandate": "document_type_direct_debit_mandate",
    "combined_contract_mandate": "document_type_combined_contract_mandate",
    "group_pt": "document_type_group_pt",
    "cancellation": "document_type_cancellation",
    "id_document": "document_type_id_document",
    "waiver": "document_type_waiver",
    "receipt": "document_type_receipt",
    "other": "document_type_other",
}


DOCUMENT_GROUP_ORDER = {
    "signup_form": 10,
    "contract": 20,
    "direct_debit_mandate": 30,
    "combined_contract_mandate": 35,
    "group_pt": 40,
    "cancellation": 50,
    "id_document": 60,
    "waiver": 70,
    "receipt": 80,
    "other": 90,
}


def translated_document_title(document_type, language=DEFAULT_LANGUAGE):
    key = DOCUMENT_TRANSLATION_KEYS.get(document_type, DOCUMENT_TRANSLATION_KEYS["other"])
    return translated_text(key, language)


def translated_document_explanation(document_type, language=DEFAULT_LANGUAGE):
    key = f"document_explanation_{document_type}"
    return translated_text(key, language)


def route_document_type_key(document_type):
    route_map = {
        "signup-form": "signup_form",
        "contract": "contract",
        "mandate": "direct_debit_mandate",
    }
    return route_map.get(document_type, document_type)


DATE_SYNC_FIELDS = {
    "birthdate",
    "start_date",
    "end_date",
    "signup_date",
    "last_payment",
    "next_payment",
    "due_date",
}


def parse_sync_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def normalize_sync_member_data(raw_member):
    columns = {column.name for column in Member.__table__.columns}
    normalized = {}
    for key, value in (raw_member or {}).items():
        if key not in columns or key == "id":
            continue
        if key in DATE_SYNC_FIELDS:
            normalized[key] = parse_sync_date(value)
        else:
            normalized[key] = value
    member_id = str(normalized.get("member_id") or "").strip()
    if not member_id:
        raise ValueError("Sync member is missing member_id.")
    normalized["member_id"] = member_id
    return normalized


SYNC_CHANGE_FIELDS = [
    "name",
    "email",
    "phone",
    "mobile",
    "plan_type",
    "contract_type",
    "billing_status",
    "billing_option",
    "billing_type",
    "billing_amount",
    "balance",
    "last_payment",
    "last_payment_amount",
    "next_payment",
    "due_date",
    "start_date",
    "end_date",
    "signup_date",
    "photo_path",
    "is_active",
]

SYNC_CHANGE_LABELS = {
    "name": "Name",
    "email": "Email",
    "phone": "Phone",
    "mobile": "Mobile",
    "plan_type": "Plan",
    "contract_type": "Contract type",
    "billing_status": "Billing status",
    "billing_option": "Billing option",
    "billing_type": "Billing type",
    "billing_amount": "Billing amount",
    "balance": "Balance",
    "last_payment": "Last payment",
    "last_payment_amount": "Last paid amount",
    "next_payment": "Next payment",
    "due_date": "Due date",
    "start_date": "Contract begin",
    "end_date": "Contract end",
    "signup_date": "Signup date",
    "photo_path": "Photo",
    "is_active": "Active",
}


def change_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 2)
    return value


def member_change_summary(member_id, name=None, email=None, plan_type=None, changes=None):
    return {
        "member_id": str(member_id),
        "name": name or "",
        "email": email or "",
        "plan_type": plan_type or "",
        "changes": changes or [],
    }


def member_field_changes(existing, member_data):
    changes = []
    for field in SYNC_CHANGE_FIELDS:
        if field not in member_data:
            continue
        before = change_value(getattr(existing, field, None))
        after = change_value(member_data.get(field))
        if before != after:
            changes.append({
                "field": field,
                "label": SYNC_CHANGE_LABELS.get(field, field),
                "old": before,
                "new": after,
            })
    return changes


def document_signature(record):
    return {
        "document_type": record.get("document_type") or "other",
        "title": record.get("title") or "Other Document",
        "path": record.get("path") or "",
        "source_filename": record.get("source_filename") or "",
    }


def existing_document_signatures(member_id):
    return [
        document_signature({
            "document_type": document.document_type,
            "title": document.title,
            "path": document.path,
            "source_filename": document.source_filename,
        })
        for document in MemberDocument.query.filter_by(member_id=member_id).order_by(MemberDocument.display_order).all()
    ]


def sync_change_summary_for_template(sync_run):
    if not sync_run.change_summary:
        return {"new_members": [], "changed_members": [], "document_changes": []}
    try:
        return json.loads(sync_run.change_summary)
    except (TypeError, json.JSONDecodeError):
        return {"new_members": [], "changed_members": [], "document_changes": []}


def sync_runs_for_local_date(selected_date):
    return [
        run
        for run in SyncRun.query.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).all()
        if run.started_at and local_datetime(run.started_at).date() == selected_date
    ]


def daily_sync_changes(selected_date):
    runs = sync_runs_for_local_date(selected_date)
    new_members = {}
    changed_members = {}
    document_changes = {}

    for run in runs:
        summary = sync_change_summary_for_template(run)
        run_time = format_date(run.started_at, "%H:%M")
        for item in summary.get("new_members", []):
            member_id = str(item.get("member_id") or "")
            if not member_id:
                continue
            new_members.setdefault(member_id, {**item, "run_time": run_time})

        for item in summary.get("changed_members", []):
            member_id = str(item.get("member_id") or "")
            if not member_id:
                continue
            entry = changed_members.setdefault(member_id, {
                "member_id": member_id,
                "name": item.get("name") or "",
                "email": item.get("email") or "",
                "plan_type": item.get("plan_type") or "",
                "changes": [],
            })
            for change in item.get("changes", []):
                entry["changes"].append({**change, "run_time": run_time})

        for item in summary.get("document_changes", []):
            member_id = str(item.get("member_id") or "")
            if not member_id:
                continue
            entry = document_changes.setdefault(member_id, {**item, "run_times": []})
            entry["run_times"].append(run_time)
            entry["old_count"] = item.get("old_count", entry.get("old_count", 0))
            entry["new_count"] = item.get("new_count", entry.get("new_count", 0))
            if item.get("documents"):
                entry["documents"] = item.get("documents")

    return {
        "runs": runs,
        "new_members": sorted(new_members.values(), key=lambda item: item.get("name") or item.get("member_id") or ""),
        "changed_members": sorted(changed_members.values(), key=lambda item: item.get("name") or item.get("member_id") or ""),
        "document_changes": sorted(document_changes.values(), key=lambda item: item.get("name") or item.get("member_id") or ""),
    }


def mark_stale_sync_runs(now=None):
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=STALE_SYNC_RUN_MINUTES)
    stale_runs = SyncRun.query.filter(
        SyncRun.status == "running",
        SyncRun.completed_at.is_(None),
        SyncRun.started_at < cutoff,
    ).all()
    for sync_run in stale_runs:
        sync_run.status = "interrupted"
        sync_run.completed_at = now
        sync_run.error = (
            f"Sync did not complete within {STALE_SYNC_RUN_MINUTES} minutes. "
            "The next scheduled run can continue normally."
        )
    if stale_runs:
        db.session.commit()
    return stale_runs


def apply_sync_payload(payload):
    members = payload.get("members") or []
    documents_by_member = payload.get("documents") or {}
    source = payload.get("source") or "sync-agent"
    warning = (payload.get("warning") or "").strip() or None
    sync_run = SyncRun(
        source=source,
        status="running",
        members_received=len(members),
        documents_received=sum(len(records or []) for records in documents_by_member.values()),
        error=warning,
    )
    db.session.add(sync_run)
    db.session.commit()

    new = updated = 0
    change_summary = {
        "new_members": [],
        "changed_members": [],
        "document_changes": [],
    }
    try:
        for raw_member in members:
            member_data = normalize_sync_member_data(raw_member)
            existing = Member.query.filter_by(member_id=member_data["member_id"]).first()
            if existing:
                changes = member_field_changes(existing, member_data)
                if changes:
                    updated += 1
                    change_summary["changed_members"].append(member_change_summary(
                        existing.member_id,
                        name=member_data.get("name") or existing.name,
                        email=member_data.get("email") or existing.email,
                        plan_type=member_data.get("plan_type") or existing.plan_type,
                        changes=changes,
                    ))
                for key, value in member_data.items():
                    setattr(existing, key, value)
            else:
                db.session.add(Member(**member_data))
                new += 1
                change_summary["new_members"].append(member_change_summary(
                    member_data["member_id"],
                    name=member_data.get("name"),
                    email=member_data.get("email"),
                    plan_type=member_data.get("plan_type"),
                ))

            document_records = documents_by_member.get(member_data["member_id"])
            if document_records is not None:
                before_documents = existing_document_signatures(member_data["member_id"])
                after_documents = [document_signature(record) for record in document_records]
                if before_documents != after_documents:
                    change_summary["document_changes"].append({
                        "member_id": member_data["member_id"],
                        "name": member_data.get("name") or "",
                        "old_count": len(before_documents),
                        "new_count": len(after_documents),
                        "documents": after_documents,
                    })
                    MemberDocument.query.filter_by(member_id=member_data["member_id"]).delete()
                    for display_order, record in enumerate(document_records):
                        db.session.add(MemberDocument(
                            member_id=member_data["member_id"],
                            document_type=record.get("document_type") or "other",
                            title=record.get("title") or "Other Document",
                            path=record.get("path") or "",
                            source_filename=record.get("source_filename"),
                            display_order=display_order,
                        ))

        sync_run.status = "success"
        sync_run.completed_at = datetime.now()
        sync_run.members_new = new
        sync_run.members_updated = updated
        sync_run.change_summary = json.dumps(change_summary)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        sync_run = db.session.get(SyncRun, sync_run.id)
        if sync_run:
            sync_run.status = "failed"
            sync_run.completed_at = datetime.now()
            sync_run.error = str(exc)
            db.session.commit()
        raise

    return sync_run


def display_member_name(name):
    if not name:
        return ""

    parts = [part.strip() for part in name.split(",", 1)]
    if len(parts) == 2 and all(parts):
        last_name, first_names = parts
        return f"{first_names} {last_name}"

    return name


def cancellation_message(policy, language=DEFAULT_LANGUAGE):
    summary, detail = cancellation_message_parts(policy, language=language)
    return " ".join(part for part in (summary, detail) if part)


def cancellation_message_parts(policy, language=DEFAULT_LANGUAGE):
    language = normalize_language(language)

    if policy.status == "not_applicable_short_pass":
        return (
            translated_text("policy_short_pass_summary", language),
            translated_text("policy_short_pass_detail", language),
        )

    if policy.status in {"allowed_no_fixed_term", "not_applicable_non_contract"}:
        return (
            translated_text("policy_no_fixed_summary", language),
            translated_text("policy_no_fixed_detail", language),
        )

    if policy.status == "blocked_missing_contract_dates":
        return (
            translated_text("policy_missing_summary", language),
            translated_text("policy_missing_detail", language),
        )

    period = str(policy.term_months)

    term_end = fmt_policy_date(policy.current_term_end, language)
    window_open = fmt_policy_date(policy.window_open, language)
    last_request = fmt_policy_date(policy.last_request_date, language)

    base = translated_text(
        "policy_contract_detail",
        language,
        period=period,
        term_end=term_end,
    )

    if policy.status == "allowed_in_window":
        return (
            translated_text(
                "policy_open_summary",
                language,
                window_open=window_open,
                last_request=last_request,
            ),
            base,
        )
    if policy.status == "blocked_too_early":
        next_open = fmt_policy_date(policy.next_window_open or policy.window_open, language)
        next_last = fmt_policy_date(policy.next_window_last_request_date or policy.last_request_date, language)
        return (
            translated_text(
                "policy_too_early_summary",
                language,
                next_open=next_open,
                next_last=next_last,
            ),
            base,
        )
    if policy.status == "blocked_window_closed":
        next_open = fmt_policy_date(policy.next_window_open, language)
        next_last = fmt_policy_date(policy.next_window_last_request_date, language)
        return (
            translated_text(
                "policy_closed_summary",
                language,
                next_open=next_open,
                next_last=next_last,
            ),
            translated_text(
                "policy_closed_detail",
                language,
                window_open=window_open,
                last_request=last_request,
                base=base,
            ),
        )

    return policy.reason, ""

def create_cancellation_request(member, policy, reason, status, mail_status="not_sent", mail_error=None, language=None):
    request_record = CancellationRequest(
        member_id=member.member_id,
        status=status,
        reason=reason or None,
        language=normalize_language(language),
        policy_status=policy.status,
        policy_reason=policy.reason,
        term_months=policy.term_months,
        current_term_start=policy.current_term_start,
        current_term_end=policy.current_term_end,
        window_open=policy.window_open,
        window_close_exclusive=policy.window_close_exclusive,
        last_request_date=policy.last_request_date,
        next_window_open=policy.next_window_open,
        next_window_last_request_date=policy.next_window_last_request_date,
        mail_status=mail_status,
        mail_error=mail_error,
        member_name=member.name,
        member_email=member.email,
        plan_type=member.plan_type,
        contract_type=member.contract_type,
        admin_status="new",
    )
    db.session.add(request_record)
    return request_record


def active_cancellation_request_for_member(member):
    return (
        CancellationRequest.query
        .filter(
            CancellationRequest.member_id == member.member_id,
            CancellationRequest.status == "accepted",
            CancellationRequest.admin_status.in_(["new", "reviewed", "processed"]),
        )
        .order_by(CancellationRequest.requested_at.desc())
        .first()
    )


def open_cancellation_count():
    return CancellationRequest.query.filter(
        CancellationRequest.status == "accepted",
        CancellationRequest.admin_status.in_(["new", "reviewed"]),
    ).count()


def open_email_log_count():
    return EmailLog.query.filter(email_log_review_required_filter()).count()


def email_log_review_required_filter():
    return db.and_(
        EmailLog.reviewed_at.is_(None),
        db.or_(
            EmailLog.status == "failed",
            EmailLog.subject.ilike("%cancellation%"),
        ),
    )


def email_log_requires_review(email):
    if not email or email.reviewed_at:
        return False
    subject = (email.subject or "").lower()
    return email.status == "failed" or "cancellation" in subject


def cancellation_request_member_message(request_record, language=DEFAULT_LANGUAGE):
    if not request_record:
        return None
    if request_record.admin_status == "processed":
        return {
            "title": translated_text("cancellation_confirmed_title", language),
            "body": translated_text("cancellation_confirmed_body", language),
            "tone": "confirmed",
        }
    return {
        "title": translated_text("cancellation_request_received_title", language),
        "body": translated_text("cancellation_request_received_body", language),
        "tone": "pending",
    }


def date_with_clamped_day(year, month, day):
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def month_end(value):
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def final_payment_date_for_cancellation(request_date):
    debit_day = int(app.config.get("DIRECT_DEBIT_DAY", 28))
    current_month_debit = date_with_clamped_day(request_date.year, request_date.month, debit_day)
    if request_date <= current_month_debit:
        return current_month_debit

    next_month = request_date + relativedelta(months=1)
    return date_with_clamped_day(next_month.year, next_month.month, debit_day)


def cancellation_access_until_from_final_payment(final_payment_date):
    if not final_payment_date:
        return None
    return month_end(final_payment_date + relativedelta(months=1))


def cancellation_access_until(member, request_record=None):
    if request_record and getattr(request_record, "access_until", None):
        return request_record.access_until

    request_date = None
    if request_record and request_record.requested_at:
        request_date = request_record.requested_at.date()
    if request_date:
        return cancellation_access_until_from_final_payment(
            final_payment_date_for_cancellation(request_date)
        )

    return member.next_payment or member.due_date or getattr(request_record, "current_term_end", None)


def format_email_date(value, language=DEFAULT_LANGUAGE):
    if isinstance(value, datetime):
        value = value.date()
    return fmt_policy_date(value, language) or translated_text("not_available", language)


def build_cancellation_confirmation(member, request_record):
    language = normalize_language(getattr(request_record, "language", None))
    request_date = request_record.requested_at.date() if request_record.requested_at else None
    last_paid_date = (
        final_payment_date_for_cancellation(request_date)
        if request_date
        else member.last_payment
    )
    access_until = (
        cancellation_access_until_from_final_payment(last_paid_date)
        if request_date
        else cancellation_access_until(member, request_record)
    )
    member_name = display_member_name(member.name)
    subject = translated_text("email_cancel_confirmed_subject", language)
    intro = translated_text("email_cancel_confirmed_intro", language, name=member_name)
    note = translated_text("email_cancel_confirmed_note", language)
    signoff = translated_text("email_signoff", language)
    not_provided = translated_text("not_provided", language)
    body = f"""{intro}

{translated_text("member_id", language)}: {member.member_id}
{translated_text("membership_type", language)}: {member.plan_type or translated_text("not_available", language)}
{translated_text("email_request_date", language)}: {format_email_date(request_record.requested_at, language)}
{translated_text("email_final_payment_date", language)}: {format_email_date(last_paid_date, language)}
{translated_text("access_until", language)}: {format_email_date(access_until, language)}

{note}

{translated_text("reason", language)}:
{request_record.reason or not_provided}

{signoff}
Dreamz Fitness
"""
    html_body = email_html_layout(
        translated_text("cancellation_confirmed_title", language),
        intro,
        rows=[
            (translated_text("member_id", language), member.member_id),
            (translated_text("membership_type", language), member.plan_type or translated_text("not_available", language)),
            (translated_text("email_request_date", language), format_email_date(request_record.requested_at, language)),
            (translated_text("email_final_payment_date", language), format_email_date(last_paid_date, language)),
            (translated_text("access_until", language), format_email_date(access_until, language)),
            (translated_text("reason", language), request_record.reason or not_provided),
        ],
        note=note,
        signoff=signoff,
        tone="success",
    )
    return subject, body, html_body, last_paid_date, access_until


def current_member_or_redirect():
    member_id = session.get("member_id")
    if not member_id:
        flash("Please log in first.")
        return None, redirect(url_for("login"))

    member = Member.query.filter_by(member_id=member_id).first()
    if not member:
        session.pop("member_id", None)
        flash("Please log in first.")
        return None, redirect(url_for("login"))

    return member, None


COACH_GOALS = ["lose_weight", "build_muscle", "get_fitter", "strength", "health"]
COACH_EXPERIENCE_LEVELS = ["beginner", "intermediate", "advanced"]
COACH_TRAINING_DAYS = [2, 3, 4, 5, 6]
COACH_SESSION_MINUTES = [30, 45, 60, 75, 90]
COACH_TRAINING_PLACES = ["dreamz_gym", "home", "both"]
COACH_NUTRITION_GOALS = ["fat_loss", "muscle_gain", "maintenance", "healthier"]
COACH_EXERCISE_LIBRARY = {
    "leg_press": ("machine", "3", "10-12", "90 sec", "moderate"),
    "goblet_squat": ("dumbbell", "3", "8-10", "90 sec", "moderate"),
    "chest_press": ("machine", "3", "8-12", "90 sec", "moderate"),
    "lat_pulldown": ("machine", "3", "10-12", "75 sec", "moderate"),
    "seated_row": ("machine", "3", "10-12", "75 sec", "moderate"),
    "shoulder_press": ("machine", "2-3", "8-10", "75 sec", "light_moderate"),
    "treadmill_intervals": ("cardio", "6", "1 min work / 1 min easy", "as needed", "controlled"),
    "plank": ("bodyweight", "3", "30-45 sec", "60 sec", "controlled"),
    "cable_woodchop": ("cable", "3", "10 each side", "60 sec", "light_moderate"),
    "hip_thrust": ("machine_or_bar", "3", "10-12", "90 sec", "moderate"),
    "dumbbell_rdl": ("dumbbell", "3", "8-10", "90 sec", "moderate"),
    "incline_walk": ("cardio", "1", "12-20 min", "as needed", "comfortable"),
}
COACH_FOCUS_EXERCISES = {
    "full_body_strength": ["leg_press", "chest_press", "seated_row"],
    "full_body_conditioning": ["goblet_squat", "lat_pulldown", "incline_walk"],
    "upper_core": ["chest_press", "lat_pulldown", "plank"],
    "lower_conditioning": ["leg_press", "dumbbell_rdl", "treadmill_intervals"],
    "lower_body": ["leg_press", "dumbbell_rdl", "hip_thrust"],
    "upper_body": ["chest_press", "lat_pulldown", "shoulder_press"],
    "conditioning_core": ["treadmill_intervals", "plank", "cable_woodchop"],
    "mobility_recovery": ["incline_walk", "plank", "cable_woodchop"],
    "muscle_lower": ["leg_press", "hip_thrust", "dumbbell_rdl"],
    "muscle_upper": ["chest_press", "lat_pulldown", "seated_row"],
    "strength_basics": ["leg_press", "chest_press", "lat_pulldown"],
}


def coach_profile_for_member(member):
    return CoachProfile.query.filter_by(member_id=member.member_id).first()


def parse_optional_float(value):
    value = str(value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_optional_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coach_label(key, value, language=None):
    if not value:
        return translated_text("not_available", language or current_language())
    return translated_text(f"coach_{key}_{value}", language or current_language())


def coach_profile_completion(profile):
    if not profile:
        return 0
    fields = [
        profile.primary_goal,
        profile.experience_level,
        profile.training_days,
        profile.session_minutes,
        profile.training_place,
        profile.home_equipment if profile.training_place in ("home", "both") else "not applicable",
        profile.height_cm,
        profile.weight_kg,
        profile.injuries,
        profile.nutrition_goal,
        profile.dietary_preferences,
        profile.allergies,
    ]
    return round((sum(1 for field in fields if field not in (None, "")) / len(fields)) * 100)


def coach_profile_missing_fields(profile_data):
    required_fields = [
        "primary_goal",
        "experience_level",
        "training_days",
        "session_minutes",
        "training_place",
        "height_cm",
        "weight_kg",
        "injuries",
        "nutrition_goal",
        "dietary_preferences",
        "allergies",
    ]
    if profile_data.get("training_place") in ("home", "both"):
        required_fields.append("home_equipment")
    return [field for field in required_fields if profile_data.get(field) in (None, "")]


def coach_starter_guidance(profile, language=None):
    language = language or current_language()
    if not profile:
        return []

    training_days = profile.training_days or 3
    session_minutes = profile.session_minutes or 45
    goal_key = profile.primary_goal or "get_fitter"
    nutrition_key = profile.nutrition_goal or "healthier"
    return [
        {
            "title": translated_text("coach_guidance_training_title", language),
            "body": translated_text(
                "coach_guidance_training_body",
                language,
                days=training_days,
                minutes=session_minutes,
                goal=coach_label("goal", goal_key, language).lower(),
            ),
        },
        {
            "title": translated_text("coach_guidance_nutrition_title", language),
            "body": translated_text(
                "coach_guidance_nutrition_body",
                language,
                goal=coach_label("nutrition", nutrition_key, language).lower(),
            ),
        },
        {
            "title": translated_text("coach_guidance_safety_title", language),
            "body": translated_text("coach_guidance_safety_body", language),
        },
    ]


def coach_training_focuses(profile):
    days = profile.training_days or 3
    goal = profile.primary_goal or "get_fitter"
    if days <= 2:
        focuses = ["full_body_strength", "full_body_conditioning"]
    elif days == 3:
        focuses = ["full_body_strength", "upper_core", "lower_conditioning"]
    elif days == 4:
        focuses = ["lower_body", "upper_body", "full_body_strength", "conditioning_core"]
    else:
        focuses = ["lower_body", "upper_body", "conditioning_core", "full_body_strength", "mobility_recovery"]

    if goal == "build_muscle":
        focuses[0] = "muscle_lower"
        if len(focuses) > 1:
            focuses[1] = "muscle_upper"
    elif goal == "lose_weight":
        focuses[-1] = "conditioning_core"
    elif goal == "strength":
        focuses[0] = "strength_basics"

    return focuses[:days]


def coach_personal_plan(profile, language=None):
    language = language or current_language()
    if not profile:
        return None

    session_minutes = profile.session_minutes or 45
    sessions = []
    for index, focus_key in enumerate(coach_training_focuses(profile), start=1):
        exercises = []
        for exercise_key in COACH_FOCUS_EXERCISES.get(focus_key, COACH_FOCUS_EXERCISES["full_body_strength"]):
            equipment_key, sets, reps, rest, load_key = COACH_EXERCISE_LIBRARY[exercise_key]
            exercises.append(
                {
                    "name": translated_text(f"coach_exercise_{exercise_key}", language),
                    "equipment": translated_text(f"coach_equipment_{equipment_key}", language),
                    "sets": sets,
                    "reps": reps,
                    "rest": rest,
                    "load": translated_text(f"coach_load_{load_key}", language),
                    "cue": translated_text(f"coach_cue_{exercise_key}", language),
                }
            )
        sessions.append(
            {
                "number": index,
                "focus": translated_text(f"coach_focus_{focus_key}", language),
                "minutes": session_minutes,
                "warmup": translated_text("coach_session_warmup", language),
                "main": translated_text("coach_session_main", language),
                "cooldown": translated_text("coach_session_cooldown", language),
                "exercises": exercises,
            }
        )

    nutrition_items = [
        translated_text(
            f"coach_plan_nutrition_{profile.nutrition_goal or 'healthier'}",
            language,
        )
    ]
    if profile.weight_kg:
        protein_low = round(profile.weight_kg * 1.6)
        protein_high = round(profile.weight_kg * 2.0)
        nutrition_items.append(
            translated_text(
                "coach_plan_protein_range",
                language,
                low=protein_low,
                high=protein_high,
            )
        )
    nutrition_items.append(translated_text("coach_plan_meal_structure", language))
    nutrition_items.append(translated_text("coach_plan_budget_bonaire", language))
    nutrition_items.append(translated_text("coach_plan_local_simple", language))

    habits_items = [
        translated_text("coach_plan_train_at", language, place=coach_label("place", profile.training_place, language)),
        translated_text("coach_plan_home_equipment", language, equipment=profile.home_equipment or translated_text("not_available", language)),
        translated_text("coach_plan_preferences", language, preferences=profile.dietary_preferences or translated_text("not_available", language)),
        translated_text("coach_plan_limitations", language, limitations=profile.injuries or translated_text("not_available", language)),
        translated_text("coach_plan_allergies", language, allergies=profile.allergies or translated_text("not_available", language)),
    ]

    return [
        {
            "title": translated_text("coach_plan_training_title", language),
            "sessions": sessions,
        },
        {
            "title": translated_text("coach_plan_nutrition_title", language),
            "items": nutrition_items,
        },
        {
            "title": translated_text("coach_plan_notes_title", language),
            "items": habits_items,
        },
    ]


def clean_coach_plan_text(value, default="", limit=700):
    text = str(value or "").strip()
    if not text:
        text = str(default or "").strip()
    return text[:limit]


def clean_coach_plan_items(items, fallback_items, limit=6):
    cleaned = []
    for item in items if isinstance(items, list) else []:
        text = clean_coach_plan_text(item, limit=500)
        if text:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    if cleaned:
        return cleaned
    return list(fallback_items or [])[:limit]


def extract_json_object(text):
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return None


def normalize_ai_coach_plan(data, fallback_plan, language=None):
    language = language or current_language()
    fallback_plan = fallback_plan or []
    if not isinstance(data, dict) or not fallback_plan:
        return fallback_plan

    fallback_training = fallback_plan[0] if fallback_plan else {"sessions": []}
    fallback_sessions = fallback_training.get("sessions", [])
    ai_training = data.get("training") if isinstance(data.get("training"), dict) else {}
    ai_sessions = ai_training.get("sessions") if isinstance(ai_training.get("sessions"), list) else []
    sessions = []

    for index, fallback_session in enumerate(fallback_sessions):
        source_session = ai_sessions[index] if index < len(ai_sessions) and isinstance(ai_sessions[index], dict) else {}
        fallback_exercises = fallback_session.get("exercises", [])
        source_exercises = source_session.get("exercises") if isinstance(source_session.get("exercises"), list) else []
        exercises = []

        for exercise_index, fallback_exercise in enumerate(fallback_exercises):
            source_exercise = (
                source_exercises[exercise_index]
                if exercise_index < len(source_exercises) and isinstance(source_exercises[exercise_index], dict)
                else {}
            )
            exercises.append(
                {
                    "name": clean_coach_plan_text(source_exercise.get("name"), fallback_exercise.get("name"), 120),
                    "equipment": clean_coach_plan_text(source_exercise.get("equipment"), fallback_exercise.get("equipment"), 120),
                    "sets": clean_coach_plan_text(source_exercise.get("sets"), fallback_exercise.get("sets"), 40),
                    "reps": clean_coach_plan_text(source_exercise.get("reps"), fallback_exercise.get("reps"), 80),
                    "rest": clean_coach_plan_text(source_exercise.get("rest"), fallback_exercise.get("rest"), 80),
                    "load": clean_coach_plan_text(source_exercise.get("load"), fallback_exercise.get("load"), 300),
                    "cue": clean_coach_plan_text(source_exercise.get("cue"), fallback_exercise.get("cue"), 300),
                }
            )

        for source_exercise in source_exercises[len(exercises):6]:
            if not isinstance(source_exercise, dict) or not source_exercise.get("name"):
                continue
            exercises.append(
                {
                    "name": clean_coach_plan_text(source_exercise.get("name"), limit=120),
                    "equipment": clean_coach_plan_text(source_exercise.get("equipment"), translated_text("not_available", language), 120),
                    "sets": clean_coach_plan_text(source_exercise.get("sets"), "3", 40),
                    "reps": clean_coach_plan_text(source_exercise.get("reps"), "8-12", 80),
                    "rest": clean_coach_plan_text(source_exercise.get("rest"), "60-90 sec", 80),
                    "load": clean_coach_plan_text(source_exercise.get("load"), translated_text("coach_load_moderate", language), 300),
                    "cue": clean_coach_plan_text(source_exercise.get("cue"), translated_text("coach_session_main", language), 300),
                }
            )

        sessions.append(
            {
                "number": index + 1,
                "focus": clean_coach_plan_text(source_session.get("focus"), fallback_session.get("focus"), 160),
                "minutes": parse_optional_int(source_session.get("minutes")) or fallback_session.get("minutes"),
                "warmup": clean_coach_plan_text(source_session.get("warmup"), fallback_session.get("warmup"), 300),
                "main": clean_coach_plan_text(source_session.get("main"), fallback_session.get("main"), 300),
                "cooldown": clean_coach_plan_text(source_session.get("cooldown"), fallback_session.get("cooldown"), 300),
                "exercises": exercises,
            }
        )

    fallback_nutrition = fallback_plan[1] if len(fallback_plan) > 1 else {"items": []}
    fallback_notes = fallback_plan[2] if len(fallback_plan) > 2 else {"items": []}
    ai_nutrition = data.get("nutrition") if isinstance(data.get("nutrition"), dict) else {}
    ai_notes = data.get("notes") if isinstance(data.get("notes"), dict) else {}

    return [
        {
            "title": clean_coach_plan_text(ai_training.get("title"), fallback_training.get("title"), 140),
            "sessions": sessions,
        },
        {
            "title": clean_coach_plan_text(ai_nutrition.get("title"), fallback_nutrition.get("title"), 140),
            "items": clean_coach_plan_items(ai_nutrition.get("items"), fallback_nutrition.get("items"), limit=7),
        },
        {
            "title": clean_coach_plan_text(ai_notes.get("title"), fallback_notes.get("title"), 140),
            "items": clean_coach_plan_items(ai_notes.get("items"), fallback_notes.get("items"), limit=6),
        },
    ]


def stored_coach_plan(record):
    if not record:
        return None
    try:
        plan = json.loads(record.plan_json)
    except (TypeError, ValueError):
        return None
    if isinstance(plan, list) and plan and isinstance(plan[0], dict):
        return plan
    return None


def generate_openai_coach_plan(member, profile, fallback_plan, language=None):
    language = language or current_language()
    api_key = app.config.get("OPENAI_API_KEY")
    mode = app.config.get("COACH_AI_MODE", "fallback")
    if not api_key or mode != "openai":
        return None, None

    recent_workouts = "\n".join(recent_coach_workout_summary(member.member_id)) or "No previous workouts logged."
    context = coach_context_summary(member, profile)
    prompt = (
        "You create the Dreamz Fitness member coach plan. Return ONLY valid JSON, no markdown and no prose outside JSON. "
        "Do not mention AI. Personalize the plan to the profile, goal, experience, schedule, injuries, available home equipment, preferences and recent workout history. "
        "Use safe, practical exercise selection for Dreamz Fitness or the selected training place. "
        "If training place is both, split or clearly adapt the sessions for Dreamz Fitness and home. If training place is home, use only the listed home equipment; if none is listed, use bodyweight and simple household-safe options. "
        "Nutrition must be realistic for Bonaire: budget-aware, common supermarket foods, simple repeatable meals, enough protein. Allergies and foods to avoid are strict constraints: never suggest those foods or close substitutes. "
        "For injuries or medical limitations, adjust exercise choices and intensity conservatively. "
        "Use the member selected language for every visible value. "
        "The number of sessions must match the fallback sessions. Each session should fit the requested minutes including warm-up and cool-down. "
        "JSON schema: {"
        "\"training\":{\"title\":\"...\",\"sessions\":[{\"focus\":\"...\",\"minutes\":45,\"warmup\":\"...\",\"main\":\"...\",\"cooldown\":\"...\","
        "\"exercises\":[{\"name\":\"...\",\"equipment\":\"...\",\"sets\":\"...\",\"reps\":\"...\",\"rest\":\"...\",\"load\":\"...\",\"cue\":\"...\"}]}]},"
        "\"nutrition\":{\"title\":\"...\",\"items\":[\"...\"]},"
        "\"notes\":{\"title\":\"...\",\"items\":[\"...\"]}"
        "}.\n\n"
        f"Language: {language}\n"
        f"Member/profile: {context}\n"
        f"Recent workouts:\n{recent_workouts}\n"
        f"Fallback structure to personalize and improve:\n{json.dumps(fallback_plan, ensure_ascii=False)}"
    )

    try:
        request = Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps({
                "model": app.config.get("OPENAI_MODEL", "gpt-4.1-mini"),
                "input": prompt,
                "max_output_tokens": 2600,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=35) as response:
            output_text = openai_text_from_response(json.loads(response.read().decode("utf-8")))
        parsed = extract_json_object(output_text)
        if parsed:
            return normalize_ai_coach_plan(parsed, fallback_plan, language), context
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        pass
    return None, context


def coach_plan_for_member(member, profile, language=None, force=False):
    language = language or current_language()
    if not member or not profile:
        return None

    fallback_plan = coach_personal_plan(profile, language=language)
    record = CoachPlan.query.filter_by(member_id=member.member_id).first()
    if record and not force and record.language == language and record.plan_version == COACH_PLAN_SCHEMA_VERSION:
        stored = stored_coach_plan(record)
        if stored:
            return stored

    generated_plan, context = generate_openai_coach_plan(member, profile, fallback_plan, language=language)
    plan = generated_plan or fallback_plan
    source = "openai" if generated_plan else "fallback"
    if not plan:
        return None

    if not record:
        record = CoachPlan(member_id=member.member_id)
    record.language = language
    record.source = source
    record.plan_version = COACH_PLAN_SCHEMA_VERSION
    record.plan_json = json.dumps(plan, ensure_ascii=False)
    record.prompt_context = context or coach_context_summary(member, profile)
    record.updated_at = datetime.now()
    db.session.add(record)
    db.session.commit()
    return plan


def recent_coach_workout_summary(member_id, limit=3):
    sessions = (
        CoachWorkoutSession.query
        .filter_by(member_id=member_id)
        .order_by(CoachWorkoutSession.completed_at.desc(), CoachWorkoutSession.id.desc())
        .limit(limit)
        .all()
    )
    summaries = []
    for workout in sessions:
        logs = (
            CoachWorkoutExerciseLog.query
            .filter_by(workout_session_id=workout.id)
            .order_by(CoachWorkoutExerciseLog.exercise_order.asc())
            .all()
        )
        exercise_text = ", ".join(
            f"{log.exercise_name}: {log.weight_used or '-'} x {log.reps_completed or '-'}"
            for log in logs
        )
        summaries.append(f"{workout.completed_at.date()}: session {workout.session_number or '-'} {workout.focus or ''}; {exercise_text}")
    activities = (
        CoachActivityLog.query
        .filter_by(member_id=member_id)
        .order_by(CoachActivityLog.activity_date.desc(), CoachActivityLog.id.desc())
        .limit(limit)
        .all()
    )
    for activity in activities:
        summaries.append(
            f"{activity.activity_date}: extra activity {activity.activity_type}, "
            f"{activity.duration_minutes or '-'} min, intensity={activity.intensity or '-'}, notes={activity.notes or '-'}"
        )
    progress_entries = (
        CoachProgressEntry.query
        .filter_by(member_id=member_id)
        .order_by(CoachProgressEntry.created_at.desc(), CoachProgressEntry.id.desc())
        .limit(limit)
        .all()
    )
    for entry in progress_entries:
        summaries.append(
            f"{entry.created_at.date()}: progress check-in {entry.entry_type}, "
            f"weight={entry.weight_kg or '-'} kg, photo={'yes' if entry.photo_path else 'no'}, notes={entry.notes or '-'}"
        )
    return summaries


def coach_workout_history(member_id, limit=6):
    sessions = (
        CoachWorkoutSession.query
        .filter_by(member_id=member_id)
        .order_by(CoachWorkoutSession.completed_at.desc(), CoachWorkoutSession.id.desc())
        .limit(limit)
        .all()
    )
    history = []
    for workout in sessions:
        logs = (
            CoachWorkoutExerciseLog.query
            .filter_by(workout_session_id=workout.id)
            .order_by(CoachWorkoutExerciseLog.exercise_order.asc())
            .all()
        )
        history.append(
            {
                "workout": workout,
                "logs": logs,
                "completed_count": sum(1 for log in logs if log.completed),
            }
        )
    return history


def coach_data_counts(member_id):
    session_ids = [
        row[0]
        for row in db.session.query(CoachWorkoutSession.id)
        .filter_by(member_id=member_id)
        .all()
    ]
    exercise_count = 0
    if session_ids:
        exercise_count = (
            CoachWorkoutExerciseLog.query
            .filter(CoachWorkoutExerciseLog.workout_session_id.in_(session_ids))
            .count()
        )
    counts = {
        "profile": CoachProfile.query.filter_by(member_id=member_id).count(),
        "plans": CoachPlan.query.filter_by(member_id=member_id).count(),
        "interactions": CoachInteraction.query.filter_by(member_id=member_id).count(),
        "activities": CoachActivityLog.query.filter_by(member_id=member_id).count(),
        "progress": CoachProgressEntry.query.filter_by(member_id=member_id).count(),
        "workouts": CoachWorkoutSession.query.filter_by(member_id=member_id).count(),
        "exercises": exercise_count,
    }
    counts["total"] = sum(counts.values())
    return counts


def delete_coach_progress_photo(photo_path):
    if not photo_path:
        return
    if is_s3_uri(photo_path):
        parsed = parse_s3_uri(photo_path)
        if not parsed:
            return
        bucket, key = parsed
        try:
            s3_client().delete_object(Bucket=bucket, Key=key)
        except Exception:
            app.logger.exception("Could not delete coach progress photo %s", photo_path)
        return

    root = Path(app.config["COACH_UPLOAD_ROOT"]).resolve()
    path = Path(photo_path).resolve()
    if path.exists() and path.is_file() and is_path_under_root(path, root):
        path.unlink()


def reset_member_coach_data(member_id):
    counts = coach_data_counts(member_id)
    progress_entries = CoachProgressEntry.query.filter_by(member_id=member_id).all()
    for entry in progress_entries:
        delete_coach_progress_photo(entry.photo_path)

    session_ids = [
        row[0]
        for row in db.session.query(CoachWorkoutSession.id)
        .filter_by(member_id=member_id)
        .all()
    ]
    if session_ids:
        (
            CoachWorkoutExerciseLog.query
            .filter(CoachWorkoutExerciseLog.workout_session_id.in_(session_ids))
            .delete(synchronize_session=False)
        )
    CoachWorkoutSession.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    CoachActivityLog.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    CoachProgressEntry.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    CoachInteraction.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    CoachPlan.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    CoachProfile.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    db.session.commit()
    return counts


def coach_progress_history(member_id, limit=12):
    return (
        CoachProgressEntry.query
        .filter_by(member_id=member_id)
        .order_by(CoachProgressEntry.created_at.desc(), CoachProgressEntry.id.desc())
        .limit(limit)
        .all()
    )


def coach_latest_workout(member_id):
    history = coach_workout_history(member_id, limit=1)
    return history[0] if history else None


def coach_week_schedule_offsets(training_days):
    if training_days <= 2:
        return [0, 3]
    if training_days == 3:
        return [0, 2, 4]
    if training_days == 4:
        return [0, 1, 3, 5]
    if training_days == 5:
        return [0, 1, 2, 4, 5]
    return [0, 1, 2, 3, 4, 5]


def coach_week_calendar(member_id, profile, plan, language=None):
    language = language or current_language()
    today = local_datetime(datetime.now()).date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    sessions = (plan[0].get("sessions") if plan else []) or []
    offsets = coach_week_schedule_offsets(profile.training_days or len(sessions) or 2)
    planned_by_date = {}
    for index, session_plan in enumerate(sessions):
        offset = offsets[index % len(offsets)]
        planned_by_date.setdefault(week_start + timedelta(days=offset), []).append(session_plan)

    workouts = (
        CoachWorkoutSession.query
        .filter_by(member_id=member_id)
        .filter(CoachWorkoutSession.completed_at >= datetime.combine(week_start, datetime.min.time()))
        .filter(CoachWorkoutSession.completed_at <= datetime.combine(week_end, datetime.max.time()))
        .order_by(CoachWorkoutSession.completed_at.asc(), CoachWorkoutSession.id.asc())
        .all()
    )
    activities = (
        CoachActivityLog.query
        .filter_by(member_id=member_id)
        .filter(CoachActivityLog.activity_date >= week_start)
        .filter(CoachActivityLog.activity_date <= week_end)
        .order_by(CoachActivityLog.activity_date.asc(), CoachActivityLog.id.asc())
        .all()
    )

    workouts_by_date = {}
    completed_session_numbers = set()
    for workout in workouts:
        local_day = local_datetime(workout.completed_at).date()
        workouts_by_date.setdefault(local_day, []).append(workout)
        if workout.session_number:
            completed_session_numbers.add(workout.session_number)

    activities_by_date = {}
    for activity in activities:
        activities_by_date.setdefault(activity.activity_date, []).append(activity)

    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        day_workouts = workouts_by_date.get(day, [])
        day_activities = activities_by_date.get(day, [])
        planned_sessions = planned_by_date.get(day, [])
        if day_workouts:
            status = "done"
        elif day_activities:
            status = "activity"
        elif planned_sessions:
            status = "planned"
        else:
            status = "rest"
        days.append(
            {
                "date": day,
                "is_today": day == today,
                "status": status,
                "planned_sessions": planned_sessions,
                "workouts": day_workouts,
                "activities": day_activities,
            }
        )

    return {
        "week_start": week_start,
        "week_end": week_end,
        "today_iso": today.isoformat(),
        "days": days,
        "completed_session_numbers": completed_session_numbers,
    }


def coach_next_session_context(profile, member_id, language=None):
    language = language or current_language()
    if not profile:
        return None
    member = Member.query.filter_by(member_id=member_id).first()
    plan = coach_plan_for_member(member, profile, language=language) if member else coach_personal_plan(profile, language=language)
    if not plan or not plan[0].get("sessions"):
        return None
    sessions = plan[0]["sessions"]
    latest = (
        CoachWorkoutSession.query
        .filter_by(member_id=member_id)
        .order_by(CoachWorkoutSession.completed_at.desc(), CoachWorkoutSession.id.desc())
        .first()
    )
    if latest and latest.session_number:
        next_index = latest.session_number % len(sessions)
    else:
        next_index = 0
    next_session = sessions[next_index]
    return {
        "session": next_session,
        "latest": latest,
        "count": CoachWorkoutSession.query.filter_by(member_id=member_id).count(),
    }


def coach_recent_interactions(member_id, limit=8):
    return (
        CoachInteraction.query
        .filter_by(member_id=member_id)
        .order_by(CoachInteraction.created_at.desc(), CoachInteraction.id.desc())
        .limit(limit)
        .all()
    )


def coach_context_summary(member, profile):
    if not profile:
        return f"Member {member.member_id}: no coach profile yet."
    latest_progress = (
        CoachProgressEntry.query
        .filter_by(member_id=member.member_id)
        .order_by(CoachProgressEntry.created_at.desc(), CoachProgressEntry.id.desc())
        .first()
    )
    progress_text = ""
    if latest_progress:
        progress_text = (
            f", latest_progress_type={latest_progress.entry_type}, "
            f"latest_progress_weight={latest_progress.weight_kg or 'unknown'}, "
            f"latest_progress_notes={latest_progress.notes or 'none'}, "
            f"latest_progress_photo={'yes' if latest_progress.photo_path else 'no'}"
        )
    return (
        f"Member {member.member_id}, goal={profile.primary_goal}, experience={profile.experience_level}, "
        f"days={profile.training_days}, minutes={profile.session_minutes}, place={profile.training_place}, "
        f"height={profile.height_cm}, weight={profile.weight_kg}, injuries={profile.injuries or 'none'}, "
        f"nutrition={profile.nutrition_goal}, food={profile.dietary_preferences or 'none'}, "
        f"allergies={profile.allergies or 'none'}, home_equipment={profile.home_equipment or 'none'}"
        f"{progress_text}"
    )


def save_coach_interaction(member_id, actor, message, category="conversation", source="portal", context_summary=None, language=None):
    interaction = CoachInteraction(
        member_id=member_id,
        actor=actor,
        category=category,
        source=source,
        language=language or current_language(),
        message=(message or "").strip(),
        context_summary=context_summary,
    )
    db.session.add(interaction)
    return interaction


def fallback_coach_reply(member, profile, user_message=None, workout_logs=None, language=None):
    language = language or current_language()
    goal = coach_label("goal", profile.primary_goal, language).lower() if profile else translated_text("coach_goal_get_fitter", language).lower()
    if workout_logs:
        completed = [log for log in workout_logs if log.completed]
        heavy_sets = [log for log in completed if log.weight_used]
        if heavy_sets:
            return translated_text("coach_auto_feedback_with_load", language, goal=goal)
        return translated_text("coach_auto_feedback_basic", language, goal=goal)
    if user_message:
        return translated_text("coach_question_fallback_answer", language, goal=goal)
    return translated_text("coach_auto_feedback_basic", language, goal=goal)


def openai_text_from_response(data):
    if isinstance(data, dict) and isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    parts = []
    for item in data.get("output", []) if isinstance(data, dict) else []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(content, dict):
                text = content.get("text") or content.get("output_text")
                if text:
                    parts.append(str(text))
    return "\n".join(parts).strip()


def generate_coach_reply(member, profile, user_message=None, workout_logs=None, category="conversation"):
    language = current_language()
    context = coach_context_summary(member, profile)
    recent_workouts = "\n".join(recent_coach_workout_summary(member.member_id)) or "No previous workouts logged."
    workout_text = ""
    if workout_logs:
        workout_text = "\n".join(
            f"- {log.exercise_name}: planned {log.planned_sets} sets x {log.planned_reps}, actual {log.weight_used or '-'} x {log.reps_completed or '-'}"
            for log in workout_logs
        )
    prompt = (
        "You are the Dreamz Fitness digital coach and part of the Dreamz coaching service. "
        "Reply in the member's selected language. Give direct, practical coaching that uses the profile, recent workouts and current plan. "
        "Be specific about next training actions, weights/reps progression, food choices, recovery and what to log next. "
        "Do not say you are an AI. Do not refer routine questions to Dreamz staff. "
        "For serious red flags such as chest pain, fainting, severe injury, severe dizziness or medical emergencies, tell the member to stop and seek qualified medical help. "
        "Use Bonaire-friendly, realistic and budget-aware training and nutrition advice. "
        "Format the reply with short sections and concrete next steps.\n\n"
        f"Language: {language}\n"
        f"Profile: {context}\n"
        f"Recent workouts:\n{recent_workouts}\n"
        f"Current workout:\n{workout_text or 'N/A'}\n"
        f"Member question:\n{user_message or 'Give short feedback after this completed workout.'}"
    )
    api_key = app.config.get("OPENAI_API_KEY")
    mode = app.config.get("COACH_AI_MODE", "fallback")
    if api_key and mode == "openai":
        try:
            request = Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps({
                    "model": app.config.get("OPENAI_MODEL", "gpt-4.1-mini"),
                    "input": prompt,
                    "max_output_tokens": 450,
                }).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=25) as response:
                reply = openai_text_from_response(json.loads(response.read().decode("utf-8")))
            if reply:
                return reply, "openai", context
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            pass
    return fallback_coach_reply(member, profile, user_message=user_message, workout_logs=workout_logs, language=language), "fallback", context


def document_config_or_404(document_type):
    config = DOCUMENT_TYPES.get(document_type)
    if not config:
        abort(404, "Document type not found.")
    return config


def member_document_path_or_404(member, document_type):
    config = document_config_or_404(document_type)
    document_path = getattr(member, config["field"], None)
    if not document_path:
        abort(404, "Document not available.")
    return config, document_path.replace("\\", "/")


def member_documents(member):
    return (
        MemberDocument.query
        .filter_by(member_id=member.member_id)
        .order_by(MemberDocument.display_order.asc(), MemberDocument.id.asc())
        .all()
    )


def member_document_groups(member):
    groups = {}
    for document in member_documents(member):
        group = groups.setdefault(
            document.document_type,
            {
                "document_type": document.document_type,
                "documents": [],
                "first_order": document.display_order,
                "first_id": document.id,
            },
        )
        group["documents"].append(document)
        group["first_order"] = min(group["first_order"], document.display_order)
        group["first_id"] = min(group["first_id"], document.id)

    return sorted(
        groups.values(),
        key=lambda group: (
            DOCUMENT_GROUP_ORDER.get(group["document_type"], DOCUMENT_GROUP_ORDER["other"]),
            group["first_order"],
            group["first_id"],
        ),
    )


def member_document_or_404(document_id):
    document = db.session.get(MemberDocument, document_id)
    if not document:
        abort(404, "Document not found.")

    if is_staff_user():
        member = Member.query.filter_by(member_id=document.member_id).first_or_404()
        return (member, document), None

    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return None, redirect_response

    if document.member_id != member.member_id:
        abort(404, "Document not found.")
    return (member, document), None


def configured_document_roots():
    roots = [Path(app.static_folder).resolve(), Path(app.config["DOCUMENT_CACHE_ROOT"]).resolve()]
    attachments_root = app.config.get("GYM_ASSISTANT_ATTACHMENTS_ROOT")
    if attachments_root:
        roots.append(Path(attachments_root).resolve())
    return roots


def configured_photo_roots():
    photos_root = app.config.get("GYM_ASSISTANT_PHOTOS_ROOT")
    return [Path(photos_root).resolve()] if photos_root else []


def is_path_under_root(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolved_document_path(document_path):
    if is_s3_uri(document_path):
        return None

    path = Path(document_path)
    candidates = [path] if path.is_absolute() else [
        Path(app.static_folder) / path,
        Path.cwd() / path,
    ]

    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        if not any(is_path_under_root(resolved, root) for root in configured_document_roots()):
            abort(403, "Document path is not allowed.")
        return resolved

    return None


def resolved_photo_path(photo_path):
    if not photo_path:
        return None
    if is_s3_uri(photo_path):
        return None

    path = Path(photo_path)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path]

    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        if not any(is_path_under_root(resolved, root) for root in configured_photo_roots()):
            abort(403, "Photo path is not allowed.")
        return resolved

    return None

def configured_staff_users():
    ensure_runtime_schema()
    return {
        user.username.strip().lower(): user
        for user in StaffUser.query.filter_by(is_active=True).all()
        if user.username
    }


def current_staff_role():
    role = session.get("staff_role")
    return role if role in {"admin", "manager"} else None


def current_staff_username():
    username = session.get("staff_username")
    return username if isinstance(username, str) else None


def parse_email_list(raw):
    if not raw:
        return []
    normalized = raw.replace(";", ",").replace("\n", ",")
    return [email.strip() for email in normalized.split(",") if email.strip()]


def append_unique_email(addresses, email):
    if not email:
        return
    normalized = email.strip()
    if not normalized:
        return
    existing = {address.lower() for address in addresses}
    if normalized.lower() not in existing:
        addresses.append(normalized)


def active_manager_emails():
    ensure_runtime_schema()
    return [
        user.email.strip()
        for user in StaffUser.query.filter_by(role="manager", is_active=True).all()
        if user.email and user.email.strip()
    ]


def notification_recipients():
    admin_email = setting_value("admin_email", "ron@dreamzfitness.com")
    to_addresses = parse_email_list(setting_value("notification_to", admin_email))
    cc_addresses = parse_email_list(setting_value("notification_cc", ""))

    if setting_value("always_cc_admin", "1") == "1" and admin_email:
        if admin_email.lower() not in {email.lower() for email in to_addresses}:
            append_unique_email(cc_addresses, admin_email)

    for manager_email in active_manager_emails():
        if manager_email.lower() not in {email.lower() for email in to_addresses}:
            append_unique_email(cc_addresses, manager_email)

    if not to_addresses and admin_email:
        to_addresses.append(admin_email)

    return to_addresses, cc_addresses


def normalized_mail_status(status):
    return status if isinstance(status, str) and status else "sent"


def cancellation_reason_from_form(form):
    reason = (form.get("reason") or "").strip()
    if reason != "Other":
        return reason

    other_reason = (form.get("other_reason") or "").strip()
    return other_reason or "Other"


def normalize_email(value):
    return (value or "").strip().lower()


def member_by_email(email):
    normalized = normalize_email(email)
    if not normalized:
        return None
    return Member.query.filter(db.func.lower(Member.email) == normalized).first()


def generate_member_login_code(member):
    normalized_email = normalize_email(member.email)
    now = datetime.now()
    MemberLoginCode.query.filter_by(email=normalized_email, used_at=None).update({"used_at": now})
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + timedelta(minutes=app.config["MEMBER_LOGIN_CODE_TTL_MINUTES"])
    login_code = MemberLoginCode(
        member_id=member.member_id,
        email=normalized_email,
        code_hash=generate_password_hash(code),
        expires_at=expires_at,
    )
    db.session.add(login_code)
    db.session.commit()
    return login_code, code


def send_member_login_code(member, code, language=DEFAULT_LANGUAGE):
    subject, body, html_body = build_member_login_code_email(member, code, language=language)
    return deliver_email([member.email], subject, body, html_body=html_body)


def latest_member_login_code(email):
    return (
        MemberLoginCode.query
        .filter_by(email=normalize_email(email), used_at=None)
        .order_by(MemberLoginCode.created_at.desc())
        .first()
    )


def recent_member_login_code_request(email, now=None):
    now = now or datetime.now()
    cutoff = now - timedelta(seconds=LOGIN_CODE_RESEND_COOLDOWN_SECONDS)
    return (
        MemberLoginCode.query
        .filter_by(email=normalize_email(email), used_at=None)
        .filter(MemberLoginCode.expires_at > now)
        .filter(MemberLoginCode.created_at >= cutoff)
        .order_by(MemberLoginCode.created_at.desc())
        .first()
    )


def is_staff_admin():
    return current_staff_role() == "admin"


def is_staff_user():
    return current_staff_role() in {"admin", "manager"}


def staff_access_from_token():
    expected_token = app.config.get("STAFF_TOKEN") or os.getenv("STAFF_TOKEN")
    supplied_token = request.headers.get("X-Staff-Token") or request.args.get("token")
    return bool(expected_token and supplied_token == expected_token)


def require_sync_access():
    expected_token = app.config.get("SYNC_API_TOKEN") or os.getenv("SYNC_API_TOKEN")
    supplied_token = request.headers.get("X-Sync-Token")
    if not expected_token:
        abort(503, "Sync API token is not configured.")
    if not supplied_token or not secrets.compare_digest(str(supplied_token), str(expected_token)):
        abort(403, "Sync API access denied.")


def require_staff_access(required_role=None):
    role = current_staff_role()
    if role:
        if required_role == "admin" and role != "admin":
            abort(403, "Admin access required.")
        return role

    if staff_access_from_token():
        return "admin"

    abort(403, "Staff login required.")

def cancellation_request_query():
    query = CancellationRequest.query.order_by(CancellationRequest.requested_at.desc())
    status = request.args.get("status", "").strip()
    if status:
        query = query.filter_by(status=status)
    return query, status

def cancellation_request_rows(records):
    for record in records:
        yield {
            "id": record.id,
            "requested_at": record.requested_at.isoformat(sep=" ", timespec="seconds") if record.requested_at else "",
            "member_id": record.member_id,
            "member_name": record.member_name or "",
            "member_email": record.member_email or "",
            "status": record.status,
            "admin_status": record.admin_status or "new",
            "policy_status": record.policy_status or "",
            "mail_status": record.mail_status or "",
            "notification_to": record.notification_to or "",
            "notification_cc": record.notification_cc or "",
            "notification_bcc": record.notification_bcc or "",
            "reason": record.reason or "",
            "plan_type": record.plan_type or "",
            "contract_type": record.contract_type or "",
            "term_months": record.term_months or "",
            "current_term_end": record.current_term_end.isoformat() if record.current_term_end else "",
            "window_open": record.window_open.isoformat() if record.window_open else "",
            "last_request_date": record.last_request_date.isoformat() if record.last_request_date else "",
            "next_window_open": record.next_window_open.isoformat() if record.next_window_open else "",
            "next_window_last_request_date": (
                record.next_window_last_request_date.isoformat()
                if record.next_window_last_request_date
                else ""
            ),
        "mail_error": record.mail_error or "",
        "handled_by": record.handled_by or "",
        "handled_at": record.handled_at.isoformat(sep=" ", timespec="seconds") if record.handled_at else "",
        "staff_note": record.staff_note or "",
        "confirmed_at": record.confirmed_at.isoformat(sep=" ", timespec="seconds") if record.confirmed_at else "",
        "last_paid_date": record.last_paid_date.isoformat() if record.last_paid_date else "",
        "access_until": record.access_until.isoformat() if record.access_until else "",
    }

# ----------------- Jinja‑filter voor datum -----------------
def member_document_types(member):
    return {
        document.document_type
        for document in MemberDocument.query.filter_by(member_id=member.member_id).all()
    }


def member_has_document_type(member, document_type, legacy_field=None):
    document_types = member_document_types(member)
    if document_type in document_types:
        return True
    if document_type in {"contract", "direct_debit_mandate"} and "combined_contract_mandate" in document_types:
        return True
    if legacy_field and getattr(member, legacy_field, None):
        return True
    return False


def path_exists(path_value):
    if not path_value:
        return False
    if is_s3_uri(path_value):
        return True
    try:
        return Path(path_value).exists()
    except OSError:
        return False


def is_contract_member(member):
    contract_type = (member.contract_type or "").strip().lower()
    plan_type = (member.plan_type or "").strip().lower()

    if contract_type == "no-contract" or "no contract" in plan_type:
        return False
    if contract_type in {"6-months", "12-months"}:
        return True
    return "contract" in plan_type


def is_staff_membership(member):
    membership_text = " ".join(
        value for value in [
            member.plan_type,
            member.contract_type,
            member.billing_type,
            member.billing_option,
        ] if value
    ).lower()
    return any(term in membership_text for term in ["medewerker", "employee", "staff"])


def cancellation_portal_available_for_member(member):
    policy = cancellation_policy_for_member(member)
    return (
        not is_staff_membership(member)
        and policy.status
        not in {
            "not_applicable_short_pass",
            "not_applicable_non_contract",
        }
    )


def requires_direct_debit_mandate(member):
    if not is_contract_member(member):
        return False

    billing_text = " ".join(
        value for value in [
            member.billing_type,
            member.billing_option,
            member.plan_type,
        ] if value
    ).lower()
    return any(term in billing_text for term in ["eft", "ach", "direct debit", "debit", "auto", "incasso"])


def payment_status_for_member(member, today=None):
    today = today or date.today()
    due_date = member.next_payment or member.due_date
    balance = member.balance or 0

    if not due_date and not member.last_payment:
        return {
            "status": "missing_payment_data",
            "label": "Missing payment data",
            "severity": "warning",
            "reason": "No next payment/due date or last payment is available.",
        }

    if due_date and due_date < today:
        if balance > 0:
            return {
                "status": "overdue",
                "label": "Overdue",
                "severity": "danger",
                "reason": f"Next payment/due date is {due_date.isoformat()} and balance is ${balance:.2f}.",
            }
        return {
            "status": "stale_payment_data",
            "label": "Payment data may be stale",
            "severity": "warning",
            "reason": f"Next payment/due date is {due_date.isoformat()}, before today, but balance is $0.00.",
        }

    if due_date == today:
        return {
            "status": "due_today",
            "label": "Due today",
            "severity": "info",
            "reason": "Next payment/due date is today.",
        }

    if balance > 0:
        return {
            "status": "balance_open",
            "label": "Open balance",
            "severity": "warning",
            "reason": f"Outstanding balance is ${balance:.2f}.",
            "balance": balance,
        }

    return {
        "status": "up_to_date",
        "label": "Up to date",
        "severity": "ok",
        "reason": "No obvious payment issue detected from the imported fields.",
    }


def localized_payment_status(payment_status, language=DEFAULT_LANGUAGE):
    status_key = payment_status.get("status", "up_to_date")
    localized = dict(payment_status)
    localized["label"] = translated_text(f"payment_status_{status_key}_label", language)
    reason_template = translate(f"payment_status_{status_key}_reason", language)
    if reason_template == f"payment_status_{status_key}_reason":
        return localized
    today = date.today()
    end_of_month = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    localized["reason"] = reason_template.format(
        reason=payment_status.get("reason", ""),
        amount=f"${payment_status.get('balance', 0):,.2f}",
        end_of_month=fmt_policy_date(end_of_month, language),
    )
    return localized


def member_audit_row(member, today=None):
    today = today or date.today()
    payment_status = payment_status_for_member(member, today=today)
    issues = []

    if not member.email:
        issues.append("missing_email")
    if not (member.mobile or member.phone):
        issues.append("missing_phone")
    if not path_exists(member.photo_path):
        issues.append("missing_photo")
    if not member_has_document_type(member, "signup_form", "form_path"):
        issues.append("missing_signup_form")

    contract_required = is_contract_member(member)
    mandate_required = requires_direct_debit_mandate(member)
    if contract_required and not member_has_document_type(member, "contract", "contract_path"):
        issues.append("missing_contract")
    if mandate_required and not member_has_document_type(member, "direct_debit_mandate", "mandate_path"):
        issues.append("missing_direct_debit_mandate")

    policy = cancellation_policy_for_member(member, today=today)
    if contract_required and policy.status == "blocked_missing_contract_dates":
        issues.append("contract_dates_incomplete")

    if payment_status["status"] != "up_to_date":
        issues.append(payment_status["status"])

    return {
        "member_id": member.member_id,
        "name": display_member_name(member.name),
        "raw_name": member.name or "",
        "email": member.email or "",
        "plan_type": member.plan_type or "",
        "contract_type": member.contract_type or "",
        "last_payment": member.last_payment,
        "next_payment": member.next_payment or member.due_date,
        "balance": member.balance or 0,
        "payment_status": payment_status,
        "requires_contract": contract_required,
        "requires_direct_debit_mandate": mandate_required,
        "issues": issues,
        "issue_count": len(issues),
    }


def audit_issue_counts(rows):
    counts = {}
    for row in rows:
        for issue in row["issues"]:
            counts[issue] = counts.get(issue, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def sort_audit_rows(rows, sort_key, direction):
    reverse = direction == "desc"

    def date_value(value):
        return value or date.min

    sorters = {
        "member": lambda row: (row["name"].lower(), row["member_id"]),
        "member_id": lambda row: int(row["member_id"]) if str(row["member_id"]).isdigit() else row["member_id"],
        "payment": lambda row: (row["payment_status"]["severity"], row["payment_status"]["status"]),
        "last_payment": lambda row: date_value(row["last_payment"]),
        "next_payment": lambda row: date_value(row["next_payment"]),
        "balance": lambda row: row["balance"],
        "plan": lambda row: (row["plan_type"].lower(), row["contract_type"].lower()),
        "issues": lambda row: row["issue_count"],
    }
    sorter = sorters.get(sort_key) or (lambda row: (-row["issue_count"], row["member_id"]))
    return sorted(rows, key=sorter, reverse=reverse)


def audit_rows(today=None):
    today = today or date.today()
    return [
        member_audit_row(member, today=today)
        for member in Member.query.order_by(Member.member_id.asc()).all()
    ]


def filtered_audit_rows(rows, issue_filter):
    if not issue_filter:
        return rows
    return [row for row in rows if issue_filter in row["issues"]]


def filtered_plan_rows(rows, plan_filter):
    if not plan_filter:
        return rows
    return [row for row in rows if row["plan_type"] == plan_filter]


def filtered_search_rows(rows, search_query):
    query = (search_query or "").strip().lower()
    if not query:
        return rows

    terms = [term for term in query.split() if term]
    if not terms:
        return rows

    def searchable_text(row):
        values = [
            row.get("member_id"),
            row.get("name"),
            row.get("raw_name"),
            row.get("email"),
            row.get("plan_type"),
            row.get("contract_type"),
            " ".join(row.get("issues") or []),
        ]
        return " ".join(str(value or "").lower() for value in values)

    return [
        row for row in rows
        if all(term in searchable_text(row) for term in terms)
    ]


def audit_plan_options(rows):
    plans = sorted({row["plan_type"] for row in rows if row["plan_type"]})
    return plans


def audit_csv_rows(rows):
    for row in rows:
        yield {
            "member_id": row["member_id"],
            "name": row["name"],
            "email": row["email"],
            "plan_type": row["plan_type"],
            "contract_type": row["contract_type"],
            "last_payment": row["last_payment"].isoformat() if row["last_payment"] else "",
            "next_payment": row["next_payment"].isoformat() if row["next_payment"] else "",
            "balance": f"{row['balance']:.2f}",
            "payment_status": row["payment_status"]["status"],
            "payment_reason": row["payment_status"]["reason"],
            "issues": ";".join(row["issues"]),
        }


def get_csrf_token():
    token = session.get("_csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def current_language():
    return normalize_language(session.get("language", DEFAULT_LANGUAGE))


def t(key):
    return translate(key, current_language())

def validate_csrf_token():
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, "Invalid CSRF token.")


def validate_request_csrf_token():
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, "Invalid CSRF token.")


@app.context_processor
def inject_csrf_token():
    return {
        "csrf_token": get_csrf_token,
        "t": t,
        "current_language": current_language(),
        "available_languages": LANGUAGES,
        "language_flags": LANGUAGE_FLAGS,
        "current_member_id": session.get("member_id"),
        "current_staff_role": current_staff_role(),
        "current_staff_username": current_staff_username(),
        "open_cancellation_count": open_cancellation_count() if is_staff_user() else 0,
        "open_email_log_count": open_email_log_count() if is_staff_user() else 0,
        "t_document_title": lambda document_type: translated_document_title(document_type, current_language()),
        "t_document_explanation": lambda document_type: translated_document_explanation(document_type, current_language()),
    }

@app.template_filter("format_date")
def format_date(value, fmt=None):
    """
    Gebruik in templates:
        {{ some_date|format_date }}                 -> 05 October 2021
        {{ some_date|format_date('%d/%m/%Y') }}     -> 05/10/2021
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = local_datetime(value)
    if fmt:
        return value.strftime(fmt)
    return fmt_policy_date(value, current_language())

# ---------- e-mail helper voor annuleringen ----------
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yourprovider.com")
SMTP_USER = os.getenv("SMTP_USER", "noreply@dreamzfitness.com")
SMTP_PASS = os.getenv("SMTP_PASS", "changeme")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Dreamz Fitness")


def formatted_sender():
    return f"{SMTP_FROM_NAME} <{SMTP_FROM}>" if SMTP_FROM_NAME else SMTP_FROM


def escape_html(value):
    return html.escape("" if value is None else str(value))


def text_to_html(value):
    return escape_html(value).replace("\n", "<br>")


def email_field_rows(rows):
    row_html = []
    for label, value in rows:
        row_html.append(
            "<tr>"
            f"<td style=\"padding:6px 14px 6px 0;color:#fbbf24;font-weight:700;vertical-align:top;white-space:nowrap;\">{escape_html(label)}</td>"
            f"<td style=\"padding:6px 0;color:#f8fafc;vertical-align:top;\">{text_to_html(value)}</td>"
            "</tr>"
        )
    return "".join(row_html)


def email_html_layout(title, intro, rows=None, note=None, action_label=None, action_url=None, tone="gold", signoff="Kind regards,"):
    accent = "#10b981" if tone == "success" else "#fbbf24"
    rows_html = (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"width:100%;margin:18px 0;border-collapse:collapse;\">{email_field_rows(rows)}</table>"
        if rows else ""
    )
    note_html = (
        f"<div style=\"margin-top:18px;padding:14px 16px;border-left:4px solid {accent};background:#111827;color:#e5e7eb;line-height:1.55;\">{text_to_html(note)}</div>"
        if note else ""
    )
    action_html = (
        f"<p style=\"margin:24px 0 0;\"><a href=\"{escape_html(action_url)}\" style=\"display:inline-block;background:{accent};color:#020617;text-decoration:none;font-weight:700;padding:11px 16px;border-radius:6px;\">{escape_html(action_label)}</a></p>"
        if action_label and action_url else ""
    )
    return f"""<!doctype html>
<html>
  <body style="margin:0;background:#0b1220;padding:0;font-family:Arial,Helvetica,sans-serif;color:#f8fafc;">
    <div style="display:none;max-height:0;overflow:hidden;color:transparent;">{escape_html(intro)}</div>
    <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;background:#0b1220;padding:24px 0;">
      <tr>
        <td align="center" style="padding:0 16px;">
          <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;max-width:680px;border-collapse:collapse;background:#111827;border:1px solid #334155;border-radius:10px;overflow:hidden;">
            <tr>
              <td style="background:#1f2937;padding:22px 26px;border-bottom:1px solid #334155;">
                <div style="font-size:24px;font-weight:800;letter-spacing:.08em;color:#ffffff;">DREAMZ <span style="color:#fbbf24;">FITNESS</span></div>
              </td>
            </tr>
            <tr>
              <td style="padding:28px 26px;">
                <h1 style="margin:0 0 14px;font-size:24px;line-height:1.25;color:{accent};">{escape_html(title)}</h1>
                <p style="margin:0;color:#f8fafc;line-height:1.6;">{text_to_html(intro)}</p>
                {rows_html}
                {note_html}
                {action_html}
                <p style="margin:26px 0 0;color:#cbd5e1;line-height:1.5;">{escape_html(signoff)}<br><strong style="color:#ffffff;">Dreamz Fitness</strong></p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def deliver_email(to_addresses, subject, body, cc_addresses=None, bcc_addresses=None, html_body=None):
    to_addresses = [email for email in (to_addresses or []) if email]
    cc_addresses = [email for email in (cc_addresses or []) if email]
    bcc_addresses = [email for email in (bcc_addresses or []) if email]
    delivery_mode = app.config.get("EMAIL_DELIVERY_MODE", "log")

    log_record = EmailLog(
        delivery_mode=delivery_mode,
        status="pending",
        to_addresses=", ".join(to_addresses),
        cc_addresses=", ".join(cc_addresses),
        bcc_addresses=", ".join(bcc_addresses),
        subject=subject,
        body=body,
        html_body=html_body,
    )
    db.session.add(log_record)
    db.session.commit()

    if delivery_mode != "smtp":
        log_record.status = "logged"
        db.session.commit()
        return "logged"

    msg = EmailMessage()
    msg["From"] = formatted_sender()
    msg["To"] = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg, to_addrs=[*to_addresses, *cc_addresses, *bcc_addresses])
    except Exception as exc:
        log_record.status = "failed"
        log_record.error = str(exc)
        db.session.commit()
        raise

    log_record.status = "sent"
    db.session.commit()
    return "sent"


def build_member_login_code_email(member, code, language=DEFAULT_LANGUAGE):
    language = normalize_language(language)
    subject = translated_text("email_login_subject", language)
    member_name = display_member_name(member.name)
    intro = translated_text("email_login_intro", language, name=member_name)
    expires = translated_text(
        "email_expires_minutes",
        language,
        minutes=app.config["MEMBER_LOGIN_CODE_TTL_MINUTES"],
    )
    note = translated_text("email_login_note", language)
    signoff = translated_text("email_signoff", language)
    body = f"""{intro}

{translated_text("login_code", language)}:

{code}

{expires}
{note}

{signoff}
Dreamz Fitness
"""
    html_body = email_html_layout(
        translated_text("email_login_title", language),
        intro,
        rows=[
            (translated_text("login_code", language), code),
            (translated_text("email_expires_in", language), expires),
        ],
        note=note,
        signoff=signoff,
    )
    return subject, body, html_body


def build_staff_cancellation_notification_email(member, reason, request_record=None, event_label="Cancellation request"):
    subject = f"Dreamz Fitness - {event_label} - {display_member_name(member.name) or member.member_id}"
    member_name = display_member_name(member.name)
    body = f"""A cancellation event needs staff/admin attention.

Event: {event_label}
Member: {display_member_name(member.name)} (ID {member.member_id})
Email: {member.email or 'Not available'}
Phone: {member.phone or member.mobile or 'Not available'}

Request status: {request_record.status if request_record else 'pending'}
Admin status: {request_record.admin_status if request_record else 'new'}
Policy status: {request_record.policy_status if request_record else 'not available'}
Contract term ends: {format_email_date(request_record.current_term_end) if request_record else 'Not available'}
Cancellation window: {format_email_date(request_record.window_open) if request_record else 'Not available'} through {format_email_date(request_record.last_request_date) if request_record else 'Not available'}

Reason:
{reason or 'Not provided'}

Next step:
Review this request in the Dreamz Fitness staff dashboard. Only mark it as Processed after GymAssistant has been updated.
"""
    html_body = email_html_layout(
        event_label,
        "A cancellation event needs staff/admin attention.",
        rows=[
            ("Member", f"{member_name} (ID {member.member_id})"),
            ("Email", member.email or "Not available"),
            ("Phone", member.phone or member.mobile or "Not available"),
            ("Request status", request_record.status if request_record else "pending"),
            ("Admin status", request_record.admin_status if request_record else "new"),
            ("Policy status", request_record.policy_status if request_record else "not available"),
            ("Term ends", format_email_date(request_record.current_term_end) if request_record else "Not available"),
            ("Window", f"{format_email_date(request_record.window_open) if request_record else 'Not available'} through {format_email_date(request_record.last_request_date) if request_record else 'Not available'}"),
            ("Reason", reason or "Not provided"),
        ],
        note="Review this request in the Dreamz Fitness staff dashboard. Only mark it as Processed after GymAssistant has been updated.",
    )
    return subject, body, html_body


def build_member_cancellation_request_email(member, request_record):
    language = normalize_language(getattr(request_record, "language", None))
    member_name = display_member_name(member.name)
    request_date = request_record.requested_at.date() if request_record.requested_at else None
    not_provided = translated_text("not_provided", language)
    signoff = translated_text("email_signoff", language)
    if request_record.status == "accepted":
        subject = translated_text("email_cancel_request_subject", language)
        intro = translated_text("email_cancel_request_intro", language, name=member_name)
        note = translated_text("email_cancel_request_note", language)
        body = f"""{intro}

{translated_text("member_id", language)}: {member.member_id}
{translated_text("email_request_date", language)}: {format_email_date(request_date, language)}
{translated_text("reason", language)}:
{request_record.reason or not_provided}

{note}

{signoff}
Dreamz Fitness
"""
        html_body = email_html_layout(
            translated_text("cancellation_request_received_title", language),
            intro,
            rows=[
                (translated_text("member_id", language), member.member_id),
                (translated_text("email_request_date", language), format_email_date(request_date, language)),
                (translated_text("reason", language), request_record.reason or not_provided),
            ],
            note=note,
            signoff=signoff,
        )
        return subject, body, html_body

    subject = translated_text("email_cancel_blocked_subject", language)
    intro = translated_text("email_cancel_blocked_intro", language, name=member_name)
    note = translated_text("email_cancel_blocked_note", language)
    body = f"""{intro}

{translated_text("member_id", language)}: {member.member_id}
{translated_text("email_term_ends", language)}: {format_email_date(request_record.current_term_end, language)}
{translated_text("email_cancellation_window", language)}: {format_email_date(request_record.window_open, language)} - {format_email_date(request_record.last_request_date, language)}

{note}

{signoff}
Dreamz Fitness
"""
    html_body = email_html_layout(
        translated_text("email_cancel_blocked_title", language),
        intro,
        rows=[
            (translated_text("member_id", language), member.member_id),
            (translated_text("email_term_ends", language), format_email_date(request_record.current_term_end, language)),
            (translated_text("email_cancellation_window", language), f"{format_email_date(request_record.window_open, language)} - {format_email_date(request_record.last_request_date, language)}"),
        ],
        note=note,
        signoff=signoff,
    )
    return subject, body, html_body


def masked_config_value(value):
    if not value:
        return "Not configured"
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def mail_config_status():
    return {
        "delivery_mode": app.config.get("EMAIL_DELIVERY_MODE", "log"),
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "user": masked_config_value(SMTP_USER),
        "from": formatted_sender(),
    }


def send_staff_test_email(recipient):
    recipient = (recipient or "").strip()
    if not recipient:
        raise ValueError("Test email recipient is required.")

    subject = "Dreamz Fitness - portal test email"
    body = f"""This is a Dreamz Fitness member portal test email.

Delivery mode: {app.config.get('EMAIL_DELIVERY_MODE', 'log')}
SMTP host: {SMTP_HOST}
SMTP port: {SMTP_PORT}
From: {formatted_sender()}

If you received this e-mail, the portal SMTP configuration is working for outbound mail.

Kind regards,
Dreamz Fitness
"""
    html_body = email_html_layout(
        "Portal test email",
        "This is a Dreamz Fitness member portal test email.",
        rows=[
            ("Delivery mode", app.config.get("EMAIL_DELIVERY_MODE", "log")),
            ("SMTP host", SMTP_HOST),
            ("SMTP port", SMTP_PORT),
            ("From", formatted_sender()),
        ],
        note="If you received this e-mail, the portal SMTP configuration is working for outbound mail.",
        tone="success",
    )
    return deliver_email([recipient], subject, body, html_body=html_body)

def send_cancel_email(member, reason, request_record=None, event_label="Cancellation request"):
    to_addresses, cc_addresses = notification_recipients()
    subject, body, html_body = build_staff_cancellation_notification_email(
        member,
        reason,
        request_record=request_record,
        event_label=event_label,
    )

    if request_record is not None:
        request_record.notification_to = ", ".join(to_addresses)
        request_record.notification_cc = ", ".join(cc_addresses)

    return deliver_email(to_addresses, subject, body, cc_addresses=cc_addresses, html_body=html_body)


def send_member_cancellation_request_email(member, request_record):
    if not member.email:
        return "not_sent"
    subject, body, html_body = build_member_cancellation_request_email(member, request_record)
    return deliver_email([member.email], subject, body, html_body=html_body)


def send_cancellation_confirmation_email(member, request_record):
    subject, body, html_body, last_paid_date, access_until = build_cancellation_confirmation(member, request_record)
    request_record.confirmation_subject = subject
    request_record.confirmation_body = body
    request_record.last_paid_date = last_paid_date
    request_record.access_until = access_until

    to_addresses = [member.email] if member.email else []
    staff_to_addresses, _ = notification_recipients()
    cc_addresses = []
    bcc_addresses = []
    member_email = normalize_email(member.email)
    admin_email = setting_value("admin_email", "ron@dreamzfitness.com")
    if admin_email and normalize_email(admin_email) != member_email:
        append_unique_email(bcc_addresses, admin_email)
    if not to_addresses:
        to_addresses, bcc_addresses = staff_to_addresses, []

    request_record.notification_to = ", ".join(to_addresses)
    request_record.notification_cc = ", ".join(cc_addresses)
    request_record.notification_bcc = ", ".join(bcc_addresses)

    return deliver_email(to_addresses, subject, body, cc_addresses=cc_addresses, bcc_addresses=bcc_addresses, html_body=html_body)

@app.route("/")
def home():
    return redirect(url_for("login"))


@app.get("/manifest.webmanifest")
def web_manifest():
    response = send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/language")
def set_language():
    session["language"] = normalize_language(request.args.get("lang"))
    next_url = request.args.get("next") or request.referrer or url_for("login")
    if not next_url.startswith("/"):
        next_url = url_for("login")
    return redirect(next_url)


def member_dashboard_context(member, staff_admin_view=False):
    policy = cancellation_policy_for_member(member)
    language = current_language()
    policy_text = cancellation_message(policy, language=language)
    policy_summary, policy_detail = cancellation_message_parts(policy, language=language)
    payment_status = localized_payment_status(payment_status_for_member(member), language)
    cancellation_request = active_cancellation_request_for_member(member)
    cancellation_request_message = cancellation_request_member_message(cancellation_request, language=language)
    show_cancellation_section = (
        cancellation_portal_available_for_member(member)
        or bool(cancellation_request)
        or (staff_admin_view and policy.status == "not_applicable_non_contract")
    )
    show_cancel = (
        show_cancellation_section
        and policy.can_request
        and not staff_admin_view
        and not cancellation_request
    )
    staff_member_can_request_cancel = (
        staff_admin_view
        and policy.can_request
        and not cancellation_request
    )

    def fmt_value(val, typ):
        if val in (None, "", 0, 0.0):
            return "Not available" if typ != "money" else "$0"
        if typ == "date":
            return val.strftime("%d %b %Y") if isinstance(val, date) else "Not available"
        if typ == "money":
            return f"${val:,.2f}"
        return val

    extra_cols = [
        (label, fmt_value(getattr(member, key, None), typ))
        for _, label, key, typ in GA_FIELDS
    ]

    info, sub, pay = grouped_fields(member)

    if not member.next_payment:
        member.next_payment = compute_next_payment(member)

    member.last_payment_amount = member.last_payment_amount or 0.0
    member.balance = member.balance or 0.0
    member.billing_amount = member.billing_amount or 0.0
    member_photo_available = bool(is_s3_uri(member.photo_path) or resolved_photo_path(member.photo_path))
    coach_profile = coach_profile_for_member(member)
    coach_completion = coach_profile_completion(coach_profile)
    coach_next_session = coach_next_session_context(coach_profile, member.member_id, language=language)
    coach_latest = coach_latest_workout(member.member_id)
    coach_counts = coach_data_counts(member.member_id) if staff_admin_view else {"total": 0}

    return {
        "member": member,
        "display_name": display_member_name(member.name),
        "member_photo_available": member_photo_available,
        "documents": member_documents(member),
        "document_groups": member_document_groups(member),
        "payment_status": payment_status,
        "info": info,
        "sub": sub,
        "pay": pay,
        "cancellation_info": policy_text,
        "cancellation_summary": policy_summary,
        "cancellation_detail": policy_detail,
        "cancellation_policy": policy,
        "cancellation_request": cancellation_request,
        "cancellation_request_message": cancellation_request_message,
        "show_cancellation_section": show_cancellation_section,
        "show_cancel": show_cancel,
        "staff_member_can_request_cancel": staff_member_can_request_cancel,
        "cancel_window_open": fmt_policy_date(policy.window_open, language),
        "cancel_window_close": fmt_policy_date(policy.last_request_date, language),
        "next_cancel_window_open": fmt_policy_date(policy.next_window_open, language),
        "next_cancel_window_close": fmt_policy_date(policy.next_window_last_request_date, language),
        "extra_cols": extra_cols,
        "staff_admin_view": staff_admin_view,
        "coach_profile": coach_profile,
        "coach_completion": coach_completion,
        "coach_complete": bool(coach_profile and coach_completion == 100),
        "coach_next_session": coach_next_session,
        "coach_latest_workout": coach_latest,
        "coach_data_counts": coach_counts,
        "coach_data_total": coach_counts.get("total", 0),
    }


@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        validate_csrf_token()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = configured_staff_users().get(username)
        if user and check_password_hash(user.password_hash, password):
            csrf_token = session.get("_csrf_token")
            language = session.get("language")
            session.clear()
            if csrf_token:
                session["_csrf_token"] = csrf_token
            if language:
                session["language"] = language
            session["staff_username"] = username
            session["staff_role"] = user.role
            session.permanent = True
            return redirect(url_for("staff_home"))
        flash("Invalid staff login.")
        return redirect(url_for("staff_login"))

    return render_template("staff_login.html")


@app.get("/staff/logout")
def staff_logout():
    session.pop("staff_username", None)
    session.pop("staff_role", None)
    flash("Logged out.")
    return redirect(url_for("staff_login"))


@app.get("/staff")
def staff_home():
    staff_role = current_staff_role()
    token_access = staff_access_from_token()
    if not staff_role and not token_access:
        return render_template("staff_login.html", form_action=url_for("staff_login"))
    staff_portal_url = (
        url_for("staff_data_audit")
        if staff_role
        else url_for("staff_data_audit", token=request.args.get("token", ""))
    )
    return render_template(
        "staff_home.html",
        staff_role=staff_role or ("admin" if token_access else None),
        staff_portal_url=staff_portal_url,
        staff_is_authenticated=bool(staff_role or token_access),
        fep_manager_url=app.config["FEP_MANAGER_URL"],
    )


@app.post("/api/sync/members")
def api_sync_members():
    require_sync_access()
    ensure_runtime_schema()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "Expected JSON sync payload.")

    try:
        sync_run = apply_sync_payload(payload)
    except ValueError as exc:
        abort(400, str(exc))

    return {
        "status": sync_run.status,
        "sync_run_id": sync_run.id,
        "members_received": sync_run.members_received,
        "members_new": sync_run.members_new,
        "members_updated": sync_run.members_updated,
        "documents_received": sync_run.documents_received,
    }


def safe_storage_upload_key(raw_key):
    key = (raw_key or "").replace("\\", "/").strip("/")
    if not key or key.startswith("/") or ".." in key.split("/"):
        abort(400, "Invalid storage key.")
    return key


@app.post("/api/sync/files")
def api_sync_file_upload():
    require_sync_access()
    key = safe_storage_upload_key(request.headers.get("X-Storage-Key"))
    content_type = request.headers.get("X-Content-Type") or request.headers.get("Content-Type")
    data = request.get_data()
    if not data:
        abort(400, "No file data received.")

    try:
        uri = upload_bytes_to_s3(data, key, content_type=content_type)
    except Exception as exc:
        app.logger.exception("Sync file upload failed for key %s", key)
        return {"status": "failed", "error": str(exc), "key": key}, 500
    return {"status": "success", "uri": uri, "key": key, "bytes": len(data)}


@app.get("/api/sync/member-ids")
def api_sync_member_ids():
    require_sync_access()
    ensure_runtime_schema()
    return {
        "member_ids": [
            member_id
            for (member_id,) in db.session.query(Member.member_id).all()
        ]
    }


@app.get("/api/sync/missing-file-keys")
def api_sync_missing_file_keys():
    require_sync_access()
    ensure_runtime_schema()
    uris = []
    for (photo_path,) in db.session.query(Member.photo_path).filter(Member.photo_path.isnot(None)).all():
        if is_s3_uri(photo_path):
            uris.append(photo_path)
    for (path,) in db.session.query(MemberDocument.path).filter(MemberDocument.path.isnot(None)).all():
        if is_s3_uri(path):
            uris.append(path)

    try:
        workers = int(os.getenv("SYNC_STORAGE_CHECK_WORKERS", "24"))
    except ValueError:
        workers = 24
    workers = max(1, min(workers, 64))
    client = s3_client()
    missing_keys = []
    seen = set()
    uri_by_key = {}
    for uri in uris:
        parsed = parse_s3_uri(uri)
        if not parsed:
            continue
        _, key = parsed
        if key in seen:
            continue
        seen.add(key)
        uri_by_key[key] = uri

    def object_exists(item):
        key, uri = item
        return key, s3_object_exists(uri, client=client)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(object_exists, item) for item in uri_by_key.items()]
        for future in as_completed(futures):
            try:
                key, exists = future.result()
            except Exception as exc:
                app.logger.exception("Could not check storage objects")
                return {"status": "failed", "error": str(exc), "missing_keys": missing_keys}, 500
            if not exists:
                missing_keys.append(key)

    return {"status": "success", "missing_keys": sorted(missing_keys), "checked": len(uri_by_key)}


@app.route("/staff/settings", methods=["GET", "POST"])
def staff_settings():
    require_staff_access(required_role="admin")
    ensure_runtime_schema()

    if request.method == "POST":
        validate_csrf_token()
        action = request.form.get("action", "save_settings")
        if action == "send_test_email":
            test_email = request.form.get("test_email", "").strip()
            try:
                mail_status = send_staff_test_email(test_email)
                if mail_status == "logged":
                    flash("Test email logged. Set EMAIL_DELIVERY_MODE=smtp to send real email.")
                else:
                    flash(f"Test email sent to {test_email}.")
            except Exception as exc:
                flash(f"Test email failed: {exc}")
            return redirect(url_for("staff_settings"))

        set_setting_value("admin_email", request.form.get("admin_email", "").strip())
        set_setting_value("notification_to", request.form.get("notification_to", "").strip())
        set_setting_value("notification_cc", request.form.get("notification_cc", "").strip())
        set_setting_value("always_cc_admin", "1" if request.form.get("always_cc_admin") == "1" else "0")

        for user in StaffUser.query.order_by(StaffUser.role.asc(), StaffUser.username.asc()).all():
            prefix = f"user_{user.id}_"
            if prefix + "username" not in request.form:
                continue
            username = request.form.get(prefix + "username", "").strip()
            email = request.form.get(prefix + "email", "").strip()
            password = request.form.get(prefix + "password", "")
            is_active = request.form.get(prefix + "is_active") == "1"
            if username:
                user.username = username
            user.email = email
            user.is_active = is_active
            if user.role == "admin" and user.username == current_staff_username():
                user.is_active = True
            user.updated_at = datetime.now()
            if password:
                user.password_hash = generate_password_hash(password)

        new_username = request.form.get("new_username", "").strip()
        new_password = request.form.get("new_password", "")
        new_role = request.form.get("new_role", "manager").strip()
        new_email = request.form.get("new_email", "").strip()
        if new_username:
            if new_role not in {"admin", "manager"}:
                abort(400, "Invalid staff role.")
            if not new_password:
                flash("New staff users need a password.")
                return redirect(url_for("staff_settings"))
            if StaffUser.query.filter_by(username=new_username).first():
                flash("That username already exists.")
                return redirect(url_for("staff_settings"))
            db.session.add(StaffUser(
                username=new_username,
                role=new_role,
                email=new_email,
                password_hash=generate_password_hash(new_password),
                is_active=True,
            ))

        db.session.commit()
        flash("Staff settings updated.")
        return redirect(url_for("staff_settings"))

    settings = {
        key: setting_value(key, value)
        for key, value in DEFAULT_SETTINGS.items()
    }
    staff_users = StaffUser.query.order_by(StaffUser.role.asc(), StaffUser.username.asc()).all()
    return render_template(
        "staff_settings.html",
        settings=settings,
        staff_users=staff_users,
        mail_config=mail_config_status(),
    )


@app.get("/staff/cancellations")
def staff_cancellations():
    staff_role = require_staff_access()
    query, status = cancellation_request_query()
    admin_status = request.args.get("admin_status", "").strip()
    if admin_status:
        query = query.filter_by(admin_status=admin_status)
    records = query.limit(500).all()
    token = request.args.get("token", "")
    return render_template(
        "staff_cancellations.html",
        requests=records,
        selected_status=status,
        selected_admin_status=admin_status,
        token=token,
        staff_role=staff_role,
    )


@app.get("/staff/email-log")
def staff_email_log():
    staff_role = require_staff_access()
    ensure_runtime_schema()
    selected_status = request.args.get("status", "").strip()
    review_filter = request.args.get("review", "").strip()
    query = EmailLog.query
    if selected_status:
        query = query.filter_by(status=selected_status)
    if review_filter == "open":
        query = query.filter(email_log_review_required_filter())
    logs = query.order_by(EmailLog.created_at.desc()).limit(100).all()
    counts = {
        "open": EmailLog.query.filter(email_log_review_required_filter()).count(),
        "failed": EmailLog.query.filter_by(status="failed").count(),
        "sent": EmailLog.query.filter_by(status="sent").count(),
        "logged": EmailLog.query.filter_by(status="logged").count(),
    }
    return render_template(
        "staff_email_log.html",
        logs=logs,
        counts=counts,
        selected_status=selected_status,
        review_filter=review_filter,
        email_log_requires_review=email_log_requires_review,
        staff_role=staff_role,
    )


@app.get("/staff/sync")
def staff_sync_status():
    require_staff_access()
    ensure_runtime_schema()
    mark_stale_sync_runs()
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    query = SyncRun.query.order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
    total_runs = query.count()
    total_pages = max((total_runs + SYNC_PAGE_SIZE - 1) // SYNC_PAGE_SIZE, 1)
    page = min(page, total_pages)
    start_index = (page - 1) * SYNC_PAGE_SIZE
    runs = query.offset(start_index).limit(SYNC_PAGE_SIZE).all()
    latest = query.first()
    run_changes = {
        run.id: sync_change_summary_for_template(run)
        for run in runs
    }
    page_numbers = []
    last_page_number = 0
    for page_number in range(1, total_pages + 1):
        if page_number in {1, total_pages} or abs(page_number - page) <= 2:
            if last_page_number and page_number - last_page_number > 1:
                page_numbers.append(None)
            page_numbers.append(page_number)
            last_page_number = page_number
    return render_template(
        "staff_sync_status.html",
        runs=runs,
        latest=latest,
        run_changes=run_changes,
        page=page,
        total_pages=total_pages,
        total_runs=total_runs,
        start_run=start_index + 1 if total_runs else 0,
        end_run=min(start_index + len(runs), total_runs),
        sync_page_numbers=page_numbers,
    )


@app.get("/staff/changes")
def staff_daily_changes():
    require_staff_access()
    ensure_runtime_schema()
    mark_stale_sync_runs()
    today = local_datetime(datetime.now(timezone.utc)).date()
    raw_date = request.args.get("date", "").strip()
    try:
        selected_date = date.fromisoformat(raw_date) if raw_date else today
    except ValueError:
        selected_date = today
    changes = daily_sync_changes(selected_date)
    return render_template(
        "staff_daily_changes.html",
        selected_date=selected_date,
        previous_date=selected_date - timedelta(days=1),
        next_date=selected_date + timedelta(days=1),
        today=today,
        runs=changes["runs"],
        new_members=changes["new_members"],
        changed_members=changes["changed_members"],
        document_changes=changes["document_changes"],
    )


@app.get("/staff/coach")
def staff_coach_activity():
    require_staff_access()
    ensure_runtime_schema()
    interactions = (
        CoachInteraction.query
        .order_by(CoachInteraction.created_at.desc(), CoachInteraction.id.desc())
        .limit(100)
        .all()
    )
    member_ids = {interaction.member_id for interaction in interactions}
    members = {}
    if member_ids:
        members = {
            member.member_id: member
            for member in Member.query.filter(Member.member_id.in_(member_ids)).all()
        }
    return render_template(
        "staff_coach_activity.html",
        interactions=interactions,
        members=members,
    )


@app.post("/staff/email-log/<int:email_id>/review")
def staff_email_log_review(email_id):
    validate_csrf_token()
    require_staff_access(required_role="admin")
    email = db.session.get(EmailLog, email_id)
    if not email:
        abort(404, "Email log not found.")
    email.reviewed_at = datetime.now()
    email.reviewed_by = current_staff_username()
    db.session.commit()
    flash("Email log marked as reviewed.")
    return redirect(url_for("staff_email_log", status=request.args.get("status", ""), review=request.args.get("review", "")))


@app.get("/staff/cancellations.csv")
def staff_cancellations_csv():
    require_staff_access(required_role="admin")
    query, _ = cancellation_request_query()
    output = StringIO()
    fieldnames = [
        "id",
        "requested_at",
        "member_id",
        "member_name",
        "member_email",
        "status",
        "admin_status",
        "policy_status",
        "mail_status",
        "notification_to",
        "notification_cc",
        "notification_bcc",
        "reason",
        "plan_type",
        "contract_type",
        "term_months",
        "current_term_end",
        "window_open",
        "last_request_date",
        "next_window_open",
        "next_window_last_request_date",
        "mail_error",
        "handled_by",
        "handled_at",
        "staff_note",
        "confirmed_at",
        "last_paid_date",
        "access_until",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(cancellation_request_rows(query.all()))
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cancellation_requests.csv"},
    )


@app.post("/staff/cancellations/<int:request_id>/status")
def staff_cancellation_status(request_id):
    validate_csrf_token()
    staff_role = require_staff_access()
    request_record = db.session.get(CancellationRequest, request_id)
    if not request_record:
        abort(404, "Cancellation request not found.")

    admin_status = request.form.get("admin_status", "").strip()
    if admin_status not in {"new", "reviewed", "processed", "rejected"}:
        abort(400, "Invalid admin status.")

    previous_status = request_record.admin_status or "new"
    admin_override = request.form.get("admin_override") == "1"
    if staff_role != "admin":
        if admin_override or previous_status != "reviewed" or admin_status != "processed":
            abort(403, "Manager can only complete reviewed cancellation requests.")
    if previous_status == "processed" and not admin_override:
        abort(409, "Processed cancellation requests are locked. Use admin correction to change them.")

    request_record.admin_status = admin_status
    request_record.staff_note = request.form.get("staff_note", "").strip() or None
    request_record.handled_by = current_staff_username()
    request_record.handled_at = datetime.now()
    db.session.commit()

    member = Member.query.filter_by(member_id=request_record.member_id).first()
    if member:
        try:
            if admin_status == "processed" and previous_status != "processed":
                request_record.confirmed_at = datetime.now()
                mail_status = send_cancellation_confirmation_email(member, request_record)
            elif previous_status != "processed":
                mail_status = send_cancel_email(
                    member,
                    request_record.reason,
                    request_record=request_record,
                    event_label=f"Cancellation status updated to {admin_status}",
                )
            else:
                mail_status = request_record.mail_status
            request_record.mail_status = normalized_mail_status(mail_status)
            request_record.mail_error = None
        except Exception as exc:
            request_record.mail_status = "failed"
            request_record.mail_error = str(exc)
        db.session.commit()

    flash("Cancellation request updated.")
    return redirect(url_for("staff_cancellations", admin_status=request.args.get("admin_status", "")))


@app.get("/staff/data-audit")
def staff_data_audit():
    staff_role = require_staff_access()
    issue_filter = request.args.get("issue", "").strip()
    plan_filter = request.args.get("plan", "").strip()
    search_query = request.args.get("q", "").strip()
    sort_key = request.args.get("sort", "issues").strip()
    direction = request.args.get("dir", "desc").strip().lower()
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    if direction not in {"asc", "desc"}:
        direction = "desc"
    all_rows = audit_rows()
    filtered_rows = filtered_audit_rows(all_rows, issue_filter)
    filtered_rows = filtered_plan_rows(filtered_rows, plan_filter)
    filtered_rows = filtered_search_rows(filtered_rows, search_query)
    filtered_rows = sort_audit_rows(filtered_rows, sort_key, direction)
    total_filtered = len(filtered_rows)
    total_pages = max((total_filtered + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE, 1)
    page = min(page, total_pages)
    start_index = (page - 1) * AUDIT_PAGE_SIZE
    rows = filtered_rows[start_index:start_index + AUDIT_PAGE_SIZE]

    return render_template(
        "staff_data_audit.html",
        rows=rows,
        issue_counts=audit_issue_counts(all_rows),
        issue_labels=AUDIT_ISSUE_LABELS,
        plan_options=audit_plan_options(all_rows),
        selected_issue=issue_filter,
        selected_plan=plan_filter,
        search_query=search_query,
        sort_key=sort_key,
        sort_dir=direction,
        total_members=len(all_rows),
        total_filtered=total_filtered,
        shown_members=len(rows),
        page=page,
        total_pages=total_pages,
        page_size=AUDIT_PAGE_SIZE,
        start_member=start_index + 1 if total_filtered else 0,
        end_member=min(start_index + len(rows), total_filtered),
        staff_role=staff_role,
    )


@app.get("/staff/data-audit.csv")
def staff_data_audit_csv():
    require_staff_access()
    issue_filter = request.args.get("issue", "").strip()
    plan_filter = request.args.get("plan", "").strip()
    rows = filtered_plan_rows(filtered_audit_rows(audit_rows(), issue_filter), plan_filter)
    output = StringIO()
    fieldnames = [
        "member_id",
        "name",
        "email",
        "plan_type",
        "contract_type",
        "last_payment",
        "next_payment",
        "balance",
        "payment_status",
        "payment_reason",
        "issues",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(audit_csv_rows(rows))
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=data_audit.csv"},
    )


@app.get("/staff/members/<member_id>")
def staff_member_detail(member_id):
    require_staff_access()
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    return render_template("dashboard.html", **member_dashboard_context(member, staff_admin_view=True))


@app.post("/staff/members/<member_id>/coach/reset")
def staff_reset_member_coach(member_id):
    validate_csrf_token()
    require_staff_access(required_role="admin")
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    counts = reset_member_coach_data(member.member_id)
    flash(
        translated_text(
            "coach_admin_reset_done",
            current_language(),
            name=display_member_name(member.name),
            count=counts.get("total", 0),
        )
    )
    return redirect(url_for("staff_member_detail", member_id=member.member_id))


def mask_email(email):
    email = email or ""
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "***" if local else "***"
    else:
        masked_local = local[:2] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def storage_file_response(uri, download_name=None, mimetype=None, as_attachment=False):
    try:
        body = open_s3_object(uri)
    except Exception as exc:
        app.logger.exception("Could not open storage object %s", uri)
        abort(404, f"Stored file not found or unavailable: {exc}")
    filename = download_name or s3_download_name(uri)
    disposition = "attachment" if as_attachment else "inline"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return Response(body.iter_chunks(), mimetype=mimetype, headers=headers)


def allowed_progress_photo(filename):
    extension = Path(filename or "").suffix.lower()
    return extension in {".jpg", ".jpeg", ".png", ".webp"}


def save_coach_progress_photo(member_id, uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return None
    filename = secure_filename(uploaded_file.filename)
    if not allowed_progress_photo(filename):
        abort(400, translated_text("coach_progress_photo_invalid", current_language()))

    extension = Path(filename).suffix.lower() or ".jpg"
    key = f"coach-progress/{member_id}/{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}{extension}"
    data = uploaded_file.read()
    if not data:
        abort(400, translated_text("coach_progress_photo_required", current_language()))
    if len(data) > 8 * 1024 * 1024:
        abort(400, translated_text("coach_progress_photo_too_large", current_language()))

    content_type = uploaded_file.mimetype or "application/octet-stream"
    if s3_bucket_name() and not app.config.get("COACH_FORCE_LOCAL_UPLOADS"):
        return upload_bytes_to_s3(data, key, content_type=content_type)

    root = Path(app.config["COACH_UPLOAD_ROOT"]).resolve()
    destination = root / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return str(destination)


@app.get("/documents/<document_type>")
def view_document(document_type):
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response

    config, _ = member_document_path_or_404(member, document_type)
    logical_document_type = route_document_type_key(document_type)
    return render_template(
        "document_viewer.html",
        document_label=translated_document_title(logical_document_type, current_language()),
        document_type=logical_document_type,
        pdf_url=url_for("document_file", document_type=document_type),
        download_url=url_for("document_file", document_type=document_type, download="1"),
        close_url=url_for("dashboard", id=member.member_id),
    )


@app.get("/documents/<document_type>/file")
def document_file(document_type):
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response

    config, document_path = member_document_path_or_404(member, document_type)
    if is_s3_uri(document_path):
        return storage_file_response(
            document_path,
            as_attachment=request.args.get("download") == "1",
            download_name=f"{member.member_id}-{config['filename']}",
            mimetype="application/pdf",
        )

    resolved_path = resolved_document_path(document_path)
    if resolved_path:
        return send_file(
            resolved_path,
            as_attachment=request.args.get("download") == "1",
            download_name=f"{member.member_id}-{config['filename']}",
            mimetype="application/pdf",
        )

    return send_from_directory(
        app.static_folder,
        document_path,
        as_attachment=request.args.get("download") == "1",
        download_name=f"{member.member_id}-{config['filename']}",
    )


@app.get("/documents/item/<int:document_id>")
def view_member_document(document_id):
    result, redirect_response = member_document_or_404(document_id)
    if redirect_response:
        return redirect_response
    member, document = result

    return render_template(
        "document_viewer.html",
        document_label=translated_document_title(document.document_type, current_language()),
        document_type=document.document_type,
        document_filename=document.source_filename,
        pdf_url=url_for("member_document_file", document_id=document.id),
        download_url=url_for("member_document_file", document_id=document.id, download="1"),
        close_url=(
            url_for("staff_member_detail", member_id=member.member_id)
            if is_staff_user()
            else url_for("dashboard", id=member.member_id)
        ),
    )


@app.get("/documents/item/<int:document_id>/file")
def member_document_file(document_id):
    result, redirect_response = member_document_or_404(document_id)
    if redirect_response:
        return redirect_response
    member, document = result

    if is_s3_uri(document.path):
        return storage_file_response(
            document.path,
            as_attachment=request.args.get("download") == "1",
            download_name=f"{member.member_id}-{document.document_type}-{document.id}.pdf",
            mimetype="application/pdf",
        )

    resolved_path = resolved_document_path(document.path)
    if not resolved_path:
        abort(404, "Document file not found.")

    return send_file(
        resolved_path,
        as_attachment=request.args.get("download") == "1",
        download_name=f"{member.member_id}-{document.document_type}-{document.id}.pdf",
        mimetype="application/pdf",
    )


@app.get("/member-photo/<member_id>")
def member_photo(member_id):
    if not is_staff_user() and ("member_id" not in session or session["member_id"] != member_id):
        abort(404)

    member = Member.query.filter_by(member_id=member_id).first_or_404()
    if is_s3_uri(member.photo_path):
        return storage_file_response(member.photo_path, mimetype="image/jpeg")

    resolved_path = resolved_photo_path(member.photo_path)
    if not resolved_path:
        abort(404, "Photo not found.")

    return send_file(resolved_path)


@app.get("/coach/progress-photo/<int:entry_id>")
def coach_progress_photo(entry_id):
    entry = CoachProgressEntry.query.get_or_404(entry_id)
    if not is_staff_user() and session.get("member_id") != entry.member_id:
        abort(404)

    if is_s3_uri(entry.photo_path):
        return storage_file_response(entry.photo_path, mimetype=entry.photo_mimetype or "image/jpeg")

    root = Path(app.config["COACH_UPLOAD_ROOT"]).resolve()
    path = Path(entry.photo_path or "").resolve()
    if not entry.photo_path or not path.exists() or not path.is_file() or not is_path_under_root(path, root):
        abort(404, "Progress photo not found.")

    return send_file(path, mimetype=entry.photo_mimetype or "image/jpeg")


def _legacy_in_cancel_window_unused(member):
    """Retourneert (show_button, window_end_date)"""
    if member.contract_type not in ["6-months", "12-months"] or not member.signup_date:
        return False, None

    period = 6 if member.contract_type == "6-months" else 12
    renewal = member.signup_date
    today   = date.today()

    while renewal <= today:
        renewal += relativedelta(months=period)

    window_start = renewal - timedelta(days=30)
    return window_start <= today < renewal, renewal

# ------------------------------------------------------------------
#  DASHBOARD – toont membergegevens + cancellation-venster
# ------------------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    member_id = request.args.get("id")

    # --- eenvoudige login-check -----------------------------------
    if "member_id" not in session or session["member_id"] != member_id:
        flash("Please log in first.")
        return redirect(url_for("login"))

    if not member_id:
        return abort(400, "Missing member ID.")

    member = Member.query.filter_by(member_id=member_id).first()
    if not member:
        return abort(404, "Member not found.")

    return render_template("dashboard.html", **member_dashboard_context(member))


@app.route("/coach", methods=["GET", "POST"])
def member_coach():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response

    profile = coach_profile_for_member(member)
    if request.method == "POST":
        validate_csrf_token()
        profile_data = {
            "primary_goal": request.form.get("primary_goal", "").strip() or None,
            "experience_level": request.form.get("experience_level", "").strip() or None,
            "training_days": parse_optional_int(request.form.get("training_days")),
            "session_minutes": parse_optional_int(request.form.get("session_minutes")),
            "training_place": request.form.get("training_place", "").strip() or None,
            "home_equipment": request.form.get("home_equipment", "").strip() or None,
            "height_cm": parse_optional_float(request.form.get("height_cm")),
            "weight_kg": parse_optional_float(request.form.get("weight_kg")),
            "injuries": request.form.get("injuries", "").strip() or None,
            "nutrition_goal": request.form.get("nutrition_goal", "").strip() or None,
            "dietary_preferences": request.form.get("dietary_preferences", "").strip() or None,
            "allergies": request.form.get("allergies", "").strip() or None,
        }
        if coach_profile_missing_fields(profile_data):
            flash(translated_text("coach_complete_required", current_language()), "error")
            return redirect(url_for("member_coach"))

        if not profile:
            profile = CoachProfile(member_id=member.member_id)
            db.session.add(profile)

        for key, value in profile_data.items():
            setattr(profile, key, value)
        profile.updated_at = datetime.now()
        CoachPlan.query.filter_by(member_id=member.member_id).delete()
        db.session.commit()
        flash(translated_text("coach_profile_saved", current_language()))
        return redirect(url_for("member_coach"))

    personal_plan = coach_plan_for_member(member, profile) if profile else None
    training_calendar = coach_week_calendar(member.member_id, profile, personal_plan) if profile and personal_plan else None
    return render_template(
        "coach.html",
        member=member,
        display_name=display_member_name(member.name),
        profile=profile,
        completion=coach_profile_completion(profile),
        starter_guidance=coach_starter_guidance(profile),
        personal_plan=personal_plan,
        coach_goals=COACH_GOALS,
        coach_experience_levels=COACH_EXPERIENCE_LEVELS,
        coach_training_days=COACH_TRAINING_DAYS,
        coach_session_minutes=COACH_SESSION_MINUTES,
        coach_training_places=COACH_TRAINING_PLACES,
        coach_nutrition_goals=COACH_NUTRITION_GOALS,
        coach_label=coach_label,
        workout_history=coach_workout_history(member.member_id),
        progress_entries=coach_progress_history(member.member_id),
        training_calendar=training_calendar,
        coach_interactions=coach_recent_interactions(member.member_id),
        coach_next_session=coach_next_session_context(profile, member.member_id),
    )


@app.post("/coach/progress")
def save_coach_progress():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()

    entry_type = request.form.get("entry_type", "progress").strip() or "progress"
    if entry_type not in {"before", "progress"}:
        entry_type = "progress"
    weight_kg = parse_optional_float(request.form.get("weight_kg"))
    notes = request.form.get("notes", "").strip() or None
    photo = request.files.get("photo")
    has_photo = bool(photo and photo.filename)
    if weight_kg is None and not notes and not has_photo:
        flash(translated_text("coach_progress_need_input", current_language()), "error")
        return redirect(url_for("member_coach", view="progress"))

    photo_mimetype = photo.mimetype if has_photo else None
    photo_path = save_coach_progress_photo(member.member_id, photo) if has_photo else None
    progress = CoachProgressEntry(
        member_id=member.member_id,
        entry_type=entry_type,
        weight_kg=weight_kg,
        photo_path=photo_path,
        photo_mimetype=photo_mimetype,
        notes=notes,
        language=current_language(),
    )
    db.session.add(progress)

    profile = coach_profile_for_member(member)
    if profile and weight_kg is not None:
        profile.weight_kg = weight_kg
        profile.updated_at = datetime.now()

    context = coach_context_summary(member, profile)
    save_coach_interaction(
        member.member_id,
        "member",
        (
            f"Progress check-in: {entry_type}, "
            f"weight={weight_kg if weight_kg is not None else '-'} kg, "
            f"photo={'yes' if photo_path else 'no'}, notes={notes or '-'}"
        ),
        category="progress_checkin",
        source="member",
        context_summary=context,
    )
    CoachPlan.query.filter_by(member_id=member.member_id).delete()
    db.session.commit()
    flash(translated_text("coach_progress_saved", current_language()))
    return redirect(url_for("member_coach", view="progress"))


@app.route("/coach/workout-log", methods=["POST"])
def save_coach_workout_log():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return jsonify({"status": "error", "message": "Login required."}), 401
    validate_request_csrf_token()

    payload = request.get_json(silent=True) or {}
    exercises = payload.get("exercises") if isinstance(payload.get("exercises"), list) else []
    completed_exercises = [exercise for exercise in exercises if exercise.get("done")]
    if not exercises or len(completed_exercises) != len(exercises):
        return jsonify({"status": "error", "message": translated_text("coach_done_required", current_language())}), 400

    workout = CoachWorkoutSession(
        member_id=member.member_id,
        session_number=parse_optional_int(payload.get("sessionNumber")),
        focus=str(payload.get("focus") or "")[:255],
        planned_minutes=parse_optional_int(payload.get("minutes")),
        language=current_language(),
        completed_at=datetime.now(),
    )
    db.session.add(workout)
    db.session.flush()

    workout_logs = []
    for index, exercise in enumerate(exercises, start=1):
        log = CoachWorkoutExerciseLog(
            workout_session_id=workout.id,
            member_id=member.member_id,
            exercise_order=index,
            exercise_name=str(exercise.get("name") or "")[:255],
            equipment=str(exercise.get("equipment") or "")[:255],
            planned_sets=str(exercise.get("sets") or "")[:50],
            planned_reps=str(exercise.get("reps") or "")[:80],
            planned_rest=str(exercise.get("rest") or "")[:80],
            weight_used=str(exercise.get("weightUsed") or "")[:80],
            reps_completed=str(exercise.get("repsCompleted") or "")[:80],
            completed=bool(exercise.get("done")),
        )
        db.session.add(log)
        workout_logs.append(log)

    profile = coach_profile_for_member(member)
    reply, source, context = generate_coach_reply(member, profile, workout_logs=workout_logs, category="workout_feedback")
    save_coach_interaction(
        member.member_id,
        "coach",
        reply,
        category="workout_feedback",
        source=source,
        context_summary=context,
    )
    CoachPlan.query.filter_by(member_id=member.member_id).delete()
    db.session.commit()
    return jsonify(
        {
            "status": "success",
            "message": translated_text("coach_workout_saved", current_language()),
            "workout_id": workout.id,
            "coach_reply": reply,
        }
    )


@app.route("/coach/activity-log", methods=["POST"])
def save_coach_activity_log():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return jsonify({"status": "error", "message": "Login required."}), 401
    validate_request_csrf_token()

    payload = request.get_json(silent=True) or {}
    activity_type = str(payload.get("activityType") or "").strip()[:80]
    activity_date_raw = str(payload.get("activityDate") or "").strip()
    duration_minutes = parse_optional_int(payload.get("durationMinutes"))
    intensity = str(payload.get("intensity") or "").strip()[:80]
    notes = str(payload.get("notes") or "").strip()[:1000]
    try:
        activity_date = datetime.strptime(activity_date_raw, "%Y-%m-%d").date()
    except ValueError:
        activity_date = None

    if not activity_type or not activity_date or not duration_minutes:
        return jsonify({"status": "error", "message": translated_text("coach_activity_required", current_language())}), 400

    activity = CoachActivityLog(
        member_id=member.member_id,
        activity_type=activity_type,
        activity_date=activity_date,
        duration_minutes=duration_minutes,
        intensity=intensity,
        notes=notes,
        language=current_language(),
    )
    db.session.add(activity)
    context = coach_context_summary(member, coach_profile_for_member(member))
    save_coach_interaction(
        member.member_id,
        "member",
        f"Extra activity logged: {activity_type}, {duration_minutes} min, intensity={intensity or '-'}, notes={notes or '-'}",
        category="activity_log",
        source="member",
        context_summary=context,
    )
    CoachPlan.query.filter_by(member_id=member.member_id).delete()
    db.session.commit()
    return jsonify({"status": "success", "message": translated_text("coach_activity_saved", current_language())})


@app.route("/coach/message", methods=["POST"])
def coach_message():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return jsonify({"status": "error", "message": "Login required."}), 401
    validate_request_csrf_token()
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("message") or "").strip()
    if not question:
        return jsonify({"status": "error", "message": translated_text("coach_question_required", current_language())}), 400

    profile = coach_profile_for_member(member)
    context = coach_context_summary(member, profile)
    save_coach_interaction(
        member.member_id,
        "member",
        question,
        category="question",
        source="member",
        context_summary=context,
    )
    reply, source, context = generate_coach_reply(member, profile, user_message=question, category="question")
    save_coach_interaction(
        member.member_id,
        "coach",
        reply,
        category="answer",
        source=source,
        context_summary=context,
    )
    db.session.commit()
    return jsonify({"status": "success", "reply": reply, "source": source})


@app.post("/cancel")
def cancel():
    validate_csrf_token()
    member_id = request.form.get("member_id", "").strip()
    reason = cancellation_reason_from_form(request.form)

    if not member_id:
        abort(400, "Missing member ID.")

    if "member_id" not in session or session["member_id"] != member_id:
        flash(translated_text("please_log_in", current_language()))
        return redirect(url_for("login"))

    member = Member.query.filter_by(member_id=member_id).first_or_404()
    policy = cancellation_policy_for_member(member)
    existing_request = active_cancellation_request_for_member(member)
    if not cancellation_portal_available_for_member(member):
        flash(translated_text("cancel_not_available_for_membership", current_language()))
        return redirect(url_for("dashboard", id=member_id))

    if existing_request:
        flash(translated_text("cancel_already_reviewing", current_language()))
        return redirect(url_for("dashboard", id=member_id))

    if not reason:
        flash(translated_text("cancel_choose_reason", current_language()))
        return redirect(url_for("dashboard", id=member_id))

    if not policy.can_request:
        request_record = create_cancellation_request(member, policy, reason, status="blocked", language=current_language())
        db.session.commit()
        try:
            staff_mail_status = send_cancel_email(
                member,
                reason,
                request_record=request_record,
                event_label="Blocked cancellation attempt",
            )
            send_member_cancellation_request_email(member, request_record)
            request_record.mail_status = normalized_mail_status(staff_mail_status)
            request_record.mail_error = None
        except Exception as exc:
            request_record.mail_status = "failed"
            request_record.mail_error = str(exc)
        db.session.commit()
        flash(cancellation_message(policy, language=current_language()))
        return redirect(url_for("dashboard", id=member_id))

    request_record = create_cancellation_request(
        member,
        policy,
        reason,
        status="accepted",
        mail_status="pending",
        language=current_language(),
    )
    db.session.commit()

    try:
        mail_status = send_cancel_email(member, reason, request_record=request_record)
        send_member_cancellation_request_email(member, request_record)
    except Exception as exc:
        request_record.mail_status = "failed"
        request_record.mail_error = str(exc)
        db.session.commit()
        flash(translated_text("cancel_email_failed", current_language()))
        return redirect(url_for("dashboard", id=member_id))

    request_record.mail_status = normalized_mail_status(mail_status)
    request_record.mail_error = None
    db.session.commit()
    flash(translated_text("cancel_request_received_flash", current_language()))
    return redirect(url_for("dashboard", id=member_id))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        validate_csrf_token()
        step = request.form.get("step", "email")

        if step == "email":
            email = normalize_email(request.form.get("email"))
            member = member_by_email(email)
            if member:
                session["pending_login_email"] = normalize_email(member.email)
                recent_code = recent_member_login_code_request(member.email)
                if not recent_code:
                    login_code, code = generate_member_login_code(member)
                    try:
                        mail_status = send_member_login_code(member, code, language=current_language())
                    except Exception:
                        mail_status = "failed"
                    if mail_status == "logged":
                        session["dev_login_code"] = code
                    else:
                        session.pop("dev_login_code", None)
                flash(translated_text("login_code_sent_if_registered", current_language()), "success")
                return redirect(url_for("login", step="code"))

            session.pop("pending_login_email", None)
            session.pop("dev_login_code", None)
            flash(translated_text("login_not_verified", current_language()), "error")
            return redirect(url_for("login"))

        pending_email = session.get("pending_login_email")
        supplied_code = request.form.get("code", "").strip()
        login_code = latest_member_login_code(pending_email)
        if not login_code:
            flash(translated_text("request_new_login_code", current_language()), "error")
            return redirect(url_for("login"))

        login_code.attempts += 1
        if login_code.expires_at < datetime.now() or login_code.attempts > 5:
            db.session.commit()
            session.pop("pending_login_email", None)
            session.pop("dev_login_code", None)
            flash(translated_text("login_code_expired", current_language()), "error")
            return redirect(url_for("login"))

        if check_password_hash(login_code.code_hash, supplied_code):
            member = Member.query.filter_by(member_id=login_code.member_id).first()
            if not member:
                flash(translated_text("login_not_verified", current_language()), "error")
                return redirect(url_for("login"))
            login_code.used_at = datetime.now()
            db.session.commit()
            csrf_token = session.get("_csrf_token")
            language = session.get("language")
            session.clear()
            if csrf_token:
                session["_csrf_token"] = csrf_token
            if language:
                session["language"] = language
            session["member_id"] = member.member_id
            session.permanent = True
            return redirect(url_for("dashboard", id=member.member_id))

        db.session.commit()
        flash(translated_text("invalid_login_code", current_language()), "error")
        return redirect(url_for("login", step="code"))

    # GET
    step = request.args.get("step")
    if step == "code" and session.get("pending_login_email"):
        return render_template(
            "login.html",
            step="code",
            masked_email=mask_email(session.get("pending_login_email")),
            dev_code=session.get("dev_login_code"),
        )
    return render_template("login.html", step="email")


@app.get("/member")
def member_login_alias():
    return redirect(url_for("login"))

@app.get("/logout")
def logout():
    session.pop("member_id", None)
    flash(translated_text("logged_out", current_language()))
    return redirect(url_for("login"))
