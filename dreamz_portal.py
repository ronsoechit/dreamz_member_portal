import calendar
import hashlib
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
    redirect, url_for, flash, session, Response, send_file, send_from_directory, jsonify, has_request_context
)

from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from dateutil.relativedelta import relativedelta
from datetime import datetime, date, timedelta   # ← bestaande regel uitbreiden
from datetime import timezone
from cancellation_policy import evaluate_cancellation_policy
from ga_fields import GA_FIELDS
from translations import (
    LANGUAGES,
    LANGUAGE_FLAGS,
    MONTH_NAMES,
    TRANSLATIONS,
    DEFAULT_LANGUAGE,
    normalize_language,
    translate,
)
from storage_backend import is_s3_uri, open_s3_object, parse_s3_uri, s3_bucket_name, s3_client, s3_download_name, s3_object_exists, upload_bytes_to_s3

import csv
import secrets
from types import SimpleNamespace
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
app.config["MEMBER_SESSION_DAYS"] = int(os.getenv("MEMBER_SESSION_DAYS", "90"))
app.config["MEMBER_PASSWORD_MIN_LENGTH"] = int(os.getenv("MEMBER_PASSWORD_MIN_LENGTH", "8"))
app.config["DIRECT_DEBIT_DAY"] = int(os.getenv("DIRECT_DEBIT_DAY", "28"))
app.config["SESSION_COOKIE_NAME"] = "dreamz_member_portal_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=app.config["MEMBER_SESSION_DAYS"])
app.config["LANGUAGE_COOKIE_NAME"] = "dreamz_language"
app.config["LANGUAGE_COOKIE_DAYS"] = 365
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
    password_hash    = db.Column(db.String(512))
    password_set_at  = db.Column(db.DateTime)


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
    contract_id = db.Column(db.String, index=True)
    membership_id = db.Column(db.String, index=True)
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
    cancellation_window_open_date = db.Column(db.Date)
    cancellation_window_close_date = db.Column(db.Date)
    confirmation_number = db.Column(db.String, index=True)
    pdf_receipt_url = db.Column(db.String)
    member_confirmation_sent_at = db.Column(db.DateTime)
    staff_notification_sent_at = db.Column(db.DateTime)


class AgreementCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True, nullable=False, index=True)
    name = db.Column(db.String, nullable=False)
    description = db.Column(db.Text)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class LegalDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_type = db.Column(db.String, unique=True, nullable=False, index=True)
    title = db.Column(db.String, nullable=False)
    category_key = db.Column(db.String, db.ForeignKey("agreement_category.key"), index=True)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    required_for = db.Column(db.Text)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    internal_notes = db.Column(db.Text)
    legal_review_needed = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    category = db.relationship("AgreementCategory", primaryjoin="LegalDocument.category_key == AgreementCategory.key")


class LegalDocumentVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("legal_document.id"), nullable=False, index=True)
    version = db.Column(db.String, nullable=False, index=True)
    effective_from = db.Column(db.Date)
    effective_to = db.Column(db.Date)
    source_language = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    full_legal_text = db.Column(db.Text, nullable=False)
    short_summary = db.Column(db.Text)
    plain_language_summary = db.Column(db.Text)
    pdf_template_key = db.Column(db.String)
    is_current = db.Column(db.Boolean, default=True, nullable=False, index=True)
    legal_review_status = db.Column(db.String, default="draft", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    document = db.relationship("LegalDocument", backref=db.backref("versions", lazy=True))


class LegalTranslation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(db.Integer, db.ForeignKey("legal_document_version.id"), nullable=False, index=True)
    language = db.Column(db.String, nullable=False, index=True)
    title = db.Column(db.String, nullable=False)
    short_summary = db.Column(db.Text)
    plain_language_summary = db.Column(db.Text)
    full_legal_text = db.Column(db.Text, nullable=False)
    translation_status = db.Column(db.String, default="draft", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    version = db.relationship("LegalDocumentVersion", backref=db.backref("translations", lazy=True))


class RequiredAgreementRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    applies_to_membership_type = db.Column(db.String, index=True)
    applies_to_contract_term = db.Column(db.String, index=True)
    applies_to_payment_method = db.Column(db.String, index=True)
    applies_to_add_on = db.Column(db.String, index=True)
    applies_to_under18 = db.Column(db.Boolean)
    required_legal_document_types = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class MembershipApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    applicant_first_name = db.Column(db.String)
    applicant_last_name = db.Column(db.String)
    date_of_birth = db.Column(db.Date)
    place_of_birth = db.Column(db.String)
    address = db.Column(db.Text)
    phone = db.Column(db.String)
    email = db.Column(db.String, index=True)
    emergency_contact_first_name = db.Column(db.String)
    emergency_contact_last_name = db.Column(db.String)
    emergency_contact_relationship = db.Column(db.String)
    emergency_contact_phone = db.Column(db.String)
    selected_membership_type = db.Column(db.String, index=True)
    selected_contract_term = db.Column(db.String, default="none", nullable=False, index=True)
    selected_add_ons = db.Column(db.Text)
    selected_payment_method = db.Column(db.String, index=True)
    mcb_account_holder_name = db.Column(db.String)
    mcb_account_number = db.Column(db.String)
    mcb_account_type = db.Column(db.String)
    direct_debit_confirmed_current_account = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String, default="draft", nullable=False, index=True)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    submitted_at = db.Column(db.DateTime)
    signed_at = db.Column(db.DateTime)
    activated_at = db.Column(db.DateTime)
    activated_by_staff_user_id = db.Column(db.Integer, db.ForeignKey("staff_user.id"), index=True)
    rejection_reason = db.Column(db.Text)
    internal_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    activated_by_staff = db.relationship("StaffUser")


class MembershipApplicationStep(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), nullable=False, index=True)
    step_key = db.Column(db.String, nullable=False, index=True)
    status = db.Column(db.String, default="pending", nullable=False, index=True)
    completed_at = db.Column(db.DateTime)
    data_snapshot = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    application = db.relationship("MembershipApplication", backref=db.backref("steps", lazy=True))


class MembershipApplicationDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), nullable=False, index=True)
    legal_document_version_id = db.Column(db.Integer, db.ForeignKey("legal_document_version.id"), index=True)
    document_type = db.Column(db.String, nullable=False, index=True)
    status = db.Column(db.String, default="required", nullable=False, index=True)
    file_url = db.Column(db.String)
    pdf_hash = db.Column(db.String)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    application = db.relationship("MembershipApplication", backref=db.backref("documents", lazy=True))
    legal_document_version = db.relationship("LegalDocumentVersion")


class MembershipApplicationStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), nullable=False, index=True)
    status = db.Column(db.String, nullable=False, index=True)
    note = db.Column(db.Text)
    changed_by_staff_user_id = db.Column(db.Integer, db.ForeignKey("staff_user.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    application = db.relationship("MembershipApplication", backref=db.backref("status_history", lazy=True))
    changed_by_staff = db.relationship("StaffUser")


class MembershipApplicationAuditEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), nullable=False, index=True)
    event_type = db.Column(db.String, nullable=False, index=True)
    actor_type = db.Column(db.String)
    actor_id = db.Column(db.String)
    message = db.Column(db.Text)
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    application = db.relationship("MembershipApplication", backref=db.backref("audit_events", lazy=True))


class DigitalSignatureRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), index=True)
    member_id = db.Column(db.String, index=True)
    full_legal_name = db.Column(db.String, nullable=False)
    email = db.Column(db.String, index=True)
    phone = db.Column(db.String)
    date_of_birth = db.Column(db.Date)
    signature_image_url = db.Column(db.String)
    signature_vector_data = db.Column(db.Text)
    signature_text_name = db.Column(db.String)
    signed_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    ip_address = db.Column(db.String)
    user_agent = db.Column(db.Text)
    verification_method = db.Column(db.String, nullable=False)
    verification_reference = db.Column(db.String)
    audit_reference_number = db.Column(db.String, unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    application = db.relationship("MembershipApplication", backref=db.backref("signature_records", lazy=True))


class DigitalSignatureAuditTrail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    signature_record_id = db.Column(db.Integer, db.ForeignKey("digital_signature_record.id"), nullable=False, index=True)
    event_type = db.Column(db.String, nullable=False, index=True)
    message = db.Column(db.Text)
    ip_address = db.Column(db.String)
    user_agent = db.Column(db.Text)
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    signature_record = db.relationship("DigitalSignatureRecord", backref=db.backref("audit_trail", lazy=True))


class MemberAgreementAcceptance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    legal_document_version_id = db.Column(db.Integer, db.ForeignKey("legal_document_version.id"), nullable=False, index=True)
    language_accepted = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    accepted_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    acceptance_method = db.Column(db.String, nullable=False, index=True)
    ip_address = db.Column(db.String)
    user_agent = db.Column(db.Text)
    contract_id = db.Column(db.String, index=True)
    membership_id = db.Column(db.String, index=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), index=True)
    signature_record_id = db.Column(db.Integer, db.ForeignKey("digital_signature_record.id"), index=True)
    accepted_text_snapshot = db.Column(db.Text)
    accepted_text_hash = db.Column(db.String, index=True)
    pdf_hash = db.Column(db.String)
    staff_user_id = db.Column(db.Integer, db.ForeignKey("staff_user.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    legal_document_version = db.relationship("LegalDocumentVersion")
    application = db.relationship("MembershipApplication")
    signature_record = db.relationship("DigitalSignatureRecord")
    staff_user = db.relationship("StaffUser")


class MemberSignedDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), index=True)
    document_type = db.Column(db.String, nullable=False, index=True)
    file_url = db.Column(db.String)
    storage_reference = db.Column(db.String)
    pdf_hash = db.Column(db.String, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    signed_at = db.Column(db.DateTime)
    related_contract_id = db.Column(db.String, index=True)
    staff_user_id = db.Column(db.Integer, db.ForeignKey("staff_user.id"), index=True)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    version = db.Column(db.String)
    status = db.Column(db.String, default="generated", nullable=False, index=True)

    application = db.relationship("MembershipApplication")
    staff_user = db.relationship("StaffUser")


class SignedPdfRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("membership_application.id"), index=True)
    member_id = db.Column(db.String, index=True)
    document_type = db.Column(db.String, nullable=False, index=True)
    legal_document_version_id = db.Column(db.Integer, db.ForeignKey("legal_document_version.id"), index=True)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    pdf_url = db.Column(db.String)
    storage_reference = db.Column(db.String)
    pdf_hash = db.Column(db.String, index=True)
    generated_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    emailed_to_member_at = db.Column(db.DateTime)
    emailed_to_admin_at = db.Column(db.DateTime)
    audit_reference_number = db.Column(db.String, index=True)
    status = db.Column(db.String, default="generated", nullable=False, index=True)

    application = db.relationship("MembershipApplication", backref=db.backref("signed_pdfs", lazy=True))
    legal_document_version = db.relationship("LegalDocumentVersion")


class CancellationWindow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, index=True)
    contract_id = db.Column(db.String, index=True)
    membership_id = db.Column(db.String, index=True)
    current_term_start_date = db.Column(db.Date)
    current_term_end_date = db.Column(db.Date, nullable=False, index=True)
    window_open_date = db.Column(db.Date, nullable=False, index=True)
    window_close_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String, default="scheduled", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class CancellationConfirmation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cancellation_request_id = db.Column(db.Integer, db.ForeignKey("cancellation_request.id"), nullable=False, index=True)
    confirmation_number = db.Column(db.String, unique=True, nullable=False, index=True)
    pdf_receipt_url = db.Column(db.String)
    pdf_hash = db.Column(db.String)
    generated_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    sent_to_member_at = db.Column(db.DateTime)
    sent_to_staff_at = db.Column(db.DateTime)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE, nullable=False)
    status = db.Column(db.String, default="generated", nullable=False, index=True)

    cancellation_request = db.relationship("CancellationRequest", backref=db.backref("confirmations", lazy=True))


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
    sex = db.Column(db.String)
    pregnancy_status = db.Column(db.String, default="not_pregnant")
    gestational_weeks = db.Column(db.Integer)
    expected_due_date = db.Column(db.Date)
    pre_pregnancy_weight_kg = db.Column(db.Float)
    multiple_pregnancy = db.Column(db.String)
    provider_cleared_exercise = db.Column(db.String)
    provider_restrictions = db.Column(db.Text)
    pregnancy_symptoms = db.Column(db.Text)
    pregnancy_consent = db.Column(db.Boolean, default=False)
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


class MealLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    meal_key = db.Column(db.String, nullable=False, index=True)
    meal_title = db.Column(db.String)
    log_type = db.Column(db.String, nullable=False, default="followed", index=True)
    food_items = db.Column(db.Text)
    portion = db.Column(db.Text)
    calories = db.Column(db.Float)
    protein = db.Column(db.Float)
    carbs = db.Column(db.Float)
    fat = db.Column(db.Float)
    language = db.Column(db.String, default=DEFAULT_LANGUAGE)
    logged_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
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


class GroupClassType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False, index=True)
    category = db.Column(db.String, nullable=False)
    intensity = db.Column(db.String, nullable=False)
    muscle_focus = db.Column(db.String)
    cardio_load = db.Column(db.String)
    strength_load = db.Column(db.String)
    recovery_impact = db.Column(db.String)
    impact_level = db.Column(db.String)
    pregnancy_safety_level = db.Column(db.String)
    default_bookable = db.Column(db.Boolean, default=True, nullable=False)
    default_publish = db.Column(db.Boolean, default=True, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class GroupClassSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False, index=True)
    source = db.Column(db.String)
    timezone = db.Column(db.String, default="America/Kralendijk", nullable=False)
    last_updated_from_pdf = db.Column(db.Date)
    status = db.Column(db.String, default="draft", nullable=False)
    published_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class GroupClassOccurrence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("group_class_schedule.id"), nullable=False, index=True)
    class_type_id = db.Column(db.Integer, db.ForeignKey("group_class_type.id"), nullable=False, index=True)
    day_of_week = db.Column(db.Integer, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    room = db.Column(db.String, nullable=False, index=True)
    instructor = db.Column(db.String)
    capacity = db.Column(db.Integer)
    note = db.Column(db.String)
    status = db.Column(db.String, default="scheduled", nullable=False, index=True)
    is_bookable = db.Column(db.Boolean, default=True, nullable=False)
    is_published = db.Column(db.Boolean, default=True, nullable=False)
    blocks_room = db.Column(db.Boolean, default=True, nullable=False)
    seed_key = db.Column(db.String, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    schedule = db.relationship("GroupClassSchedule", backref=db.backref("occurrences", lazy=True))
    class_type = db.relationship("GroupClassType", backref=db.backref("occurrences", lazy=True))


class MemberClassPreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    class_type_id = db.Column(db.Integer, db.ForeignKey("group_class_type.id"), index=True)
    preferred_classes_per_week = db.Column(db.Integer)
    plan_mode = db.Column(db.String, default="supplement", nullable=False)
    is_favorite = db.Column(db.Boolean, default=False, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    class_type = db.relationship("GroupClassType")


class MemberClassPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    occurrence_id = db.Column(db.Integer, db.ForeignKey("group_class_occurrence.id"), nullable=False, index=True)
    class_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String, default="planned", nullable=False, index=True)
    replaces_personal_workout = db.Column(db.Boolean, default=False, nullable=False)
    source = db.Column(db.String, default="member", nullable=False)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    occurrence = db.relationship("GroupClassOccurrence")


class MemberClassAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("member_class_plan.id"), index=True)
    occurrence_id = db.Column(db.Integer, db.ForeignKey("group_class_occurrence.id"), nullable=False, index=True)
    class_date = db.Column(db.Date, nullable=False, index=True)
    attended_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    source = db.Column(db.String, default="member", nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    plan = db.relationship("MemberClassPlan")
    occurrence = db.relationship("GroupClassOccurrence")


class ScheduleChangeNotification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String, index=True)
    occurrence_id = db.Column(db.Integer, db.ForeignKey("group_class_occurrence.id"), index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("member_class_plan.id"), index=True)
    change_type = db.Column(db.String, nullable=False, index=True)
    message_key = db.Column(db.String)
    message = db.Column(db.Text, nullable=False)
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    published_at = db.Column(db.DateTime)

    occurrence = db.relationship("GroupClassOccurrence")
    plan = db.relationship("MemberClassPlan")


class PricingCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True, nullable=False, index=True)
    name = db.Column(db.String, nullable=False)
    description = db.Column(db.Text)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class PricingItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seed_key = db.Column(db.String, unique=True, index=True)
    name = db.Column(db.String, nullable=False, index=True)
    category_key = db.Column(db.String, db.ForeignKey("pricing_category.key"), nullable=False, index=True)
    description = db.Column(db.Text)
    price_amount = db.Column(db.Float, default=0.0, nullable=False)
    currency = db.Column(db.String, default="USD", nullable=False)
    billing_interval = db.Column(db.String, nullable=False)
    duration = db.Column(db.String)
    visibility = db.Column(db.String, default="public", nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    effective_from = db.Column(db.Date)
    effective_to = db.Column(db.Date)
    sort_order = db.Column(db.Integer, default=0, nullable=False, index=True)
    terms = db.Column(db.Text)
    requires_front_desk_handling = db.Column(db.Boolean, default=True, nullable=False)
    online_payment_available = db.Column(db.Boolean, default=False, nullable=False)
    member_eligible = db.Column(db.Boolean, default=True, nullable=False)
    contract_only = db.Column(db.Boolean, default=False, nullable=False)
    source = db.Column(db.String)
    notes = db.Column(db.Text)
    internal_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    category = db.relationship("PricingCategory", primaryjoin="PricingItem.category_key == PricingCategory.key")


class PricingChangeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pricing_item_id = db.Column(db.Integer, db.ForeignKey("pricing_item.id"), nullable=False, index=True)
    changed_by = db.Column(db.String)
    change_type = db.Column(db.String, nullable=False, index=True)
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)

    pricing_item = db.relationship("PricingItem")


class FeatureAccessRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True, nullable=False, index=True)
    label = db.Column(db.String, nullable=False)
    access_level = db.Column(db.String, nullable=False, index=True)
    description = db.Column(db.Text)
    pricing_item_id = db.Column(db.Integer, db.ForeignKey("pricing_item.id"), index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_future_ready = db.Column(db.Boolean, default=False, nullable=False)
    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    pricing_item = db.relationship("PricingItem")


DEFAULT_SETTINGS = {
    "admin_email": "ron@dreamzfitness.com",
    "frontdesk_email": "frontdesk@dreamzfitness.com",
    "notification_to": "ron@dreamzfitness.com",
    "notification_cc": "",
    "always_cc_admin": "1",
}

DEFAULT_PORTAL_TIMEZONE_OFFSET_HOURS = -4
STALE_SYNC_RUN_MINUTES = 15
LOGIN_CODE_RESEND_COOLDOWN_SECONDS = 60
COACH_PLAN_SCHEMA_VERSION = "2026-05-27b"
GROUP_CLASS_SCHEDULE_NAME = "Dreamz Fitness Group Class Schedule"
GROUP_CLASS_SCHEDULE_SOURCE = "uploaded PDF schedule converted to database seed"
GROUP_CLASS_SCHEDULE_TIMEZONE = "America/Kralendijk"
GROUP_CLASS_SCHEDULE_LAST_UPDATED_FROM_PDF = date(2026, 5, 18)
GROUP_CLASS_DAY_KEYS = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}
GROUP_CLASS_OCCURRENCE_STATUSES = {"scheduled", "cancelled", "reserved"}
PRICING_BILLING_INTERVALS = ["one_time", "per_month", "per_session", "per_pass", "included_free"]
PRICING_VISIBILITY_OPTIONS = ["public", "members", "staff_only", "public_business"]
FEATURE_ACCESS_LEVELS = [
    "public_basic",
    "member",
    "contract_member",
    "no_contract_member",
    "staff",
    "admin",
    "internal_test",
    "premium_future",
]
LEGAL_DOCUMENT_TYPES = [
    "membership_application_form",
    "general_terms",
    "gym_rules",
    "liability_waiver",
    "media_security_consent",
    "membership_contract_6_months",
    "membership_contract_12_months",
    "direct_debit_mandate",
    "group_pt_addon_waiver",
    "cancellation_renewal_rules",
    "payment_rules",
    "personal_training_business_rules",
    "external_personal_trainer_package_terms",
]
LEGAL_REVIEW_STATUSES = ["draft", "reviewed", "legal_reviewed"]
LEGAL_TRANSLATION_STATUSES = ["draft", "reviewed", "legal_reviewed"]
MEMBERSHIP_APPLICATION_STATUSES = [
    "draft",
    "submitted",
    "signed",
    "pending_frontdesk_payment",
    "pending_staff_activation",
    "active",
    "rejected",
    "cancelled",
]
DIGITAL_SIGNATURE_VERIFICATION_METHODS = [
    "email_link",
    "logged_in_user",
    "otp_email",
    "otp_sms",
    "frontdesk_identity_verified",
]
LEGAL_TRANSLATION_DRAFT_NOTICE = (
    "Translations are provided to help members understand the agreement. "
    "Final legal wording should be reviewed before being treated as legally final."
)
LEGAL_TRANSLATION_REVIEWED_NOTICE = (
    "Current Dreamz Fitness agreement wording and translations are marked as legally reviewed."
)
AGREEMENT_CATEGORY_SEED = [
    ("general_membership_rules", "General Membership Rules", "Signup, member conduct and basic membership obligations.", 10),
    ("contract_renewal_rules", "Contract & Renewal Rules", "Fixed-term contract, renewal and cancellation rules.", 20),
    ("app_cancellation_rules", "App Cancellation Rules", "Cancellation windows and app-based cancellation process.", 30),
    ("payment_direct_debit_rules", "Payment & Direct Debit Rules", "Frontdesk payments, MCB direct debit and payment fees.", 40),
    ("gym_house_rules", "Gym House Rules", "Facility rules, towel/shirt requirements and equipment conduct.", 50),
    ("liability_waiver", "Liability Waiver", "Assumption of risk, release and indemnity language.", 60),
    ("group_pt_rules", "Group PT Rules", "Small Group PT add-on terms and waiver.", 70),
    ("personal_training_business_rules", "Personal Training Business Rules", "Rules for paid training activity on Dreamz premises.", 80),
    ("external_trainer_terms", "External Trainer Terms", "B2B external personal trainer package terms.", 90),
    ("media_security_consent", "Media/Security Consent", "Photo, video, security and promotional consent.", 100),
]


def legal_terms_text(*paragraphs):
    return "\n\n".join(paragraph.strip() for paragraph in paragraphs if paragraph and paragraph.strip())


LEGAL_DOCUMENT_SEED = [
    {
        "document_type": "membership_application_form",
        "title": "General Membership Application / Signup Terms",
        "category_key": "general_membership_rules",
        "required_for": ["all_membership_applications"],
        "sort_order": 10,
        "summary": "Every member must provide accurate personal details, choose a membership, accept required rules and sign before activation.",
        "plain": "Your application must be true and complete. Dreamz Fitness uses it to process your membership, emergency contact, payment choice and required agreements.",
        "text": legal_terms_text(
            "The applicant confirms that all personal details, date of birth, contact details, emergency contact details, membership selections and payment selections are true, complete and current.",
            "The applicant accepts the selected membership type, registration fee, set prices and payment terms shown by Dreamz Fitness. Payments and membership setup may be handled at the Dreamz Fitness front desk unless a separate online payment flow is explicitly available.",
            "The applicant agrees to follow Dreamz Fitness club rules, posted announcements, staff instructions and entry conditions. Management may refuse entry, cancel membership, or ask a member to leave for irresponsible behaviour, drugs or alcohol, unsafe conduct, or breach of entry conditions.",
            "The applicant understands that Dreamz Fitness Bonaire is operated by ABC Fitness & Health N.V. and that Bonaire, Dutch Caribbean law and jurisdiction apply where applicable."
        ),
    },
    {
        "document_type": "general_terms",
        "title": "General Terms & Conditions",
        "category_key": "general_membership_rules",
        "required_for": ["all_members"],
        "sort_order": 20,
        "summary": "General terms for Dreamz Fitness membership, facility access and member responsibilities.",
        "plain": "Be respectful, follow staff instructions, pay agreed fees, and use the facility responsibly.",
        "text": legal_terms_text(
            "Members agree with Dreamz Fitness terms, club rules, announcements, conduct rules, pricing and payment obligations applicable to their selected membership or product.",
            "Memberships, passes, add-ons, direct debit mandates and other products must be paid according to the agreed terms. All prices and fees are non-negotiable unless Dreamz Fitness formally publishes another rule.",
            "Dreamz Fitness may refuse entry, suspend access, cancel membership or require a member to leave if behaviour is unsafe, irresponsible, abusive, drug or alcohol related, or violates entry conditions or club rules.",
            "Dreamz Fitness is not responsible for lost, stolen or damaged personal property. Members remain responsible for their own belongings."
        ),
    },
    {
        "document_type": "gym_rules",
        "title": "Gym Rules",
        "category_key": "gym_house_rules",
        "required_for": ["all_members"],
        "sort_order": 30,
        "summary": "House rules for safe and respectful use of Dreamz Fitness.",
        "plain": "Use a towel, wear a training shirt, respect the equipment, and keep the gym safe and clean.",
        "text": legal_terms_text(
            "A towel is required at all times and must be used on benches and equipment. Training shirts are required. Smoking and food are not allowed in the facility.",
            "Equipment must be handled properly, returned after use and shared respectfully. Do not drop weights. Report faults, damage or unsafe situations to staff immediately.",
            "Members must train responsibly and respect other members, staff, instructors and visitors. Management instructions must be followed.",
            "Dreamz Fitness may remove members from the facility without refund if house rules or safety conditions are breached."
        ),
    },
    {
        "document_type": "liability_waiver",
        "title": "Liability Waiver & Assumption of Risk",
        "category_key": "liability_waiver",
        "required_for": ["all_members"],
        "sort_order": 40,
        "summary": "Exercise has risks. Members participate voluntarily and at their own risk.",
        "plain": "Training can cause injury. You choose to participate and must stop or ask for help if something feels unsafe.",
        "text": legal_terms_text(
            "The member understands that physical exercise, fitness training, classes, personal training and performance activities can be demanding and may cause serious injury, paralysis or death.",
            "The member voluntarily participates at their own risk and confirms they are responsible for choosing activities suitable for their health, ability and medical situation.",
            "To the maximum extent permitted by applicable law, the member releases, indemnifies and holds harmless Dreamz Fitness Bonaire, ABC Fitness & Health N.V., management, staff, instructors and representatives from claims related to participation, facility use or breach of rules.",
            "The member confirms that the information provided is true and understands that rights may be limited by this disclaimer and waiver."
        ),
    },
    {
        "document_type": "media_security_consent",
        "title": "Media & Security Consent",
        "category_key": "media_security_consent",
        "required_for": ["all_members"],
        "sort_order": 50,
        "summary": "Dreamz may use camera/security recording and limited media where applicable.",
        "plain": "The gym may use cameras for safety and may use photos/video for promotion when applicable.",
        "text": legal_terms_text(
            "The member gives permission to photograph, videotape or record for safety, security, legal, exhibition, publicity, advertising and promotional materials where applicable.",
            "Security recording may be used to protect members, staff, property, legal interests and facility safety.",
            "Dreamz Fitness will handle media and security data with care and according to applicable privacy requirements."
        ),
    },
    {
        "document_type": "membership_contract_6_months",
        "title": "6-Month Membership Contract",
        "category_key": "contract_renewal_rules",
        "required_for": ["contract_term:6_months"],
        "sort_order": 60,
        "internal_notes": "Legacy 6-month PDF appears to contain inconsistent wording referring to minimum of 12 months. That inconsistent wording is intentionally excluded from the legally reviewed templates.",
        "summary": "Six-month fixed term contract with discounted pricing and app-based cancellation window.",
        "plain": "You commit to six months. Monthly direct debit is only a payment method; it does not make the contract monthly cancellable.",
        "text": legal_terms_text(
            "This is a 6-month Dreamz Fitness membership contract. Discounted contract pricing is offered because the member commits to the full 6-month term.",
            "Payment options are USD 420 cash/pin all at once or USD 70 per month by MCB Bank Bonaire direct debit only with a valid current account and signed direct debit mandate.",
            "The first initial payment is USD 20 signup fee plus remaining weeks in the month according to Dreamz Fitness policy.",
            "Monthly payment is a payment method only and does not make the contract cancellable month-to-month. All months in the contract term remain due.",
            "There are no refunds for paid memberships. Memberships and payments cannot be frozen. Direct debit cannot be cancelled before valid cancellation or contract end according to the cancellation rules.",
            "Temporary closures, government measures and official holidays do not cancel the payment obligation according to the stated rules.",
            "The contract renews automatically under the same conditions if the member does not cancel through the Dreamz Fitness member portal during the valid cancellation window."
        ),
    },
    {
        "document_type": "membership_contract_12_months",
        "title": "12-Month Membership Contract",
        "category_key": "contract_renewal_rules",
        "required_for": ["contract_term:12_months"],
        "sort_order": 70,
        "summary": "Twelve-month fixed term contract with discounted pricing and app-based cancellation window.",
        "plain": "You commit to twelve months. Monthly direct debit is only a payment method; it does not make the contract monthly cancellable.",
        "text": legal_terms_text(
            "This is a 12-month Dreamz Fitness membership contract. Discounted contract pricing is offered because the member commits to the full 12-month term.",
            "Payment options are USD 720 cash/pin all at once or USD 60 per month by MCB Bank Bonaire direct debit only with a valid current account and signed direct debit mandate.",
            "The first initial payment is USD 20 signup fee plus remaining weeks in the month according to Dreamz Fitness policy.",
            "Monthly payment is a payment method only and does not make the contract cancellable month-to-month. All months in the contract term remain due.",
            "There are no refunds for paid memberships. Memberships and payments cannot be frozen. Direct debit cannot be cancelled before valid cancellation or contract end according to the cancellation rules.",
            "Temporary closures, government measures and official holidays do not cancel the payment obligation according to the stated rules.",
            "The contract renews automatically under the same conditions if the member does not cancel through the Dreamz Fitness member portal during the valid cancellation window."
        ),
    },
    {
        "document_type": "cancellation_renewal_rules",
        "title": "App-Based Cancellation & Renewal Rules",
        "category_key": "app_cancellation_rules",
        "required_for": ["contract_members"],
        "sort_order": 80,
        "summary": "Cancellation must be submitted in the app during the valid cancellation window.",
        "plain": "Your contract can only be cancelled during the cancellation window shown in the app. The window opens 30 days before the end of your current term and stays open for 10 days.",
        "text": legal_terms_text(
            "Cancellation is no longer handled by sending an email to info@dreamzfitness.com. Cancellation must be handled through the Dreamz Fitness member portal.",
            "The cancellation window opens 30 calendar days before the end of the current contract term and remains open for 10 calendar days.",
            "Cancellation is only possible through the app during this window. The app shows the current contract term end date, cancellation window open date, cancellation window close date and whether cancellation is currently available.",
            "If the member does not submit cancellation through the app during the valid window, the contract renews automatically under the same conditions.",
            "If cancellation is submitted successfully, Dreamz Fitness generates a confirmation or receipt."
        ),
    },
    {
        "document_type": "payment_rules",
        "title": "Payment & Direct Debit Rules",
        "category_key": "payment_direct_debit_rules",
        "required_for": ["all_members", "direct_debit_members"],
        "sort_order": 90,
        "summary": "Payment rules, direct debit restrictions and fees.",
        "plain": "Direct debit is only available for MCB Bank Bonaire current accounts. Savings accounts and non-MCB accounts are not accepted.",
        "text": legal_terms_text(
            "Direct debit is only allowed for MCB Bank Bonaire current accounts. Savings accounts are not accepted. Non-MCB accounts are not accepted. MCB savings accounts are not accepted. No exceptions.",
            "The member must explicitly confirm: I confirm that this is an MCB Bank Bonaire current account and not a savings account. If a savings account or non-MCB account is selected or entered, direct debit must be blocked.",
            "Direct debit may include membership fees and debts on account where applicable. Direct debit is processed around the 28th of the month and multiple attempts may be made where applicable.",
            "A USD 1.00 extra charge applies per direct debit transaction. A USD 10 fee applies if direct debit returns due to insufficient funds.",
            "The direct debit mandate remains valid for the duration of the contract including automatic renewals. Monthly direct debit is only a payment method; it does not change the 6- or 12-month contract commitment."
        ),
    },
    {
        "document_type": "direct_debit_mandate",
        "title": "MCB Direct Debit Mandate",
        "category_key": "payment_direct_debit_rules",
        "required_for": ["payment_method:mcb_direct_debit_monthly"],
        "sort_order": 100,
        "summary": "Authorization for Dreamz Fitness direct debit from an eligible MCB current account.",
        "plain": "You authorize Dreamz Fitness to debit your MCB current account for agreed membership fees and eligible account debts.",
        "text": legal_terms_text(
            "The member authorizes Dreamz Fitness Bonaire / ABC Fitness & Health N.V. to process direct debit from the stated MCB Bank Bonaire current account for agreed membership fees and account debts where applicable.",
            "The account must be an MCB Bank Bonaire current account. Savings accounts, MCB savings accounts and non-MCB accounts are not accepted.",
            "The mandate remains valid for the contract duration and automatic renewals unless cancellation is completed according to the app-based cancellation rules."
        ),
    },
    {
        "document_type": "group_pt_addon_waiver",
        "title": "Small Group PT Add-On & Waiver",
        "category_key": "group_pt_rules",
        "required_for": ["addon:group_pt"],
        "sort_order": 110,
        "summary": "Small Group PT add-on terms and training waiver.",
        "plain": "Small Group PT is an add-on. You participate voluntarily and at your own risk.",
        "text": legal_terms_text(
            "Small Group PT add-on is USD 85 per month and allows participation up to 5 times per week according to availability and Dreamz Fitness procedures.",
            "The member voluntarily participates in physical fitness and performance training at their own risk.",
            "The member releases, indemnifies and holds harmless the trainer, instructor, Dreamz Fitness Bonaire and ABC Fitness & Health N.V. to the maximum extent permitted by applicable law.",
            "The member confirms that all provided information is true and understands that rights may be limited by this disclaimer."
        ),
    },
    {
        "document_type": "personal_training_business_rules",
        "title": "Personal Training Business Rules",
        "category_key": "personal_training_business_rules",
        "required_for": ["all_members"],
        "sort_order": 120,
        "summary": "Paid training of clients on Dreamz premises is not allowed without approved package.",
        "plain": "You may not train clients for money inside Dreamz Fitness unless management approved the right package.",
        "text": legal_terms_text(
            "It is not allowed to help or train clients in Dreamz Fitness premises and charge them money without having an approved Personal Trainer membership/package.",
            "If management discovers unauthorized paid personal training activity, Dreamz Fitness may remove the person from the gym without refund.",
            "Dreamz Fitness may charge a fine calculated after investigation and evidence due to loss of business."
        ),
    },
    {
        "document_type": "external_personal_trainer_package_terms",
        "title": "External Personal Trainer Package Terms",
        "category_key": "external_trainer_terms",
        "required_for": ["external_trainer_b2b"],
        "sort_order": 130,
        "summary": "B2B external trainer package terms, separate from normal membership add-ons.",
        "plain": "This package is for external personal trainers, not a normal member upgrade.",
        "text": legal_terms_text(
            "The External Personal Trainer Package is a B2B/external trainer access package. It is not a member add-on.",
            "The external trainer pays Dreamz Fitness USD 250 per month to use the facilities for themselves and their own clients.",
            "The external trainer is responsible for their own clients. Dreamz Fitness is not responsible for the external trainer's coaching quality, training advice, payment collection, cancellations, disputes or client relationship.",
            "The external trainer handles their own personal training session pricing and payment directly with their own clients.",
            "Every client trained by the external trainer must either be an active Dreamz Fitness member, regardless of membership type, or purchase a valid Dreamz Fitness day pass.",
            "For training Delfins Resort guests, Dreamz Fitness charges USD 10 per guest. This package does not include a Dreamz Fitness membership for the trainer's clients."
        ),
    },
]
FEATURE_ACCESS_RULE_SEED = [
    {
        "key": "pricing_public_catalog",
        "label": "Public pricing catalog",
        "access_level": "public_basic",
        "description": "Public/basic users can view active public pricing catalog items.",
    },
    {
        "key": "member_pricing_options",
        "label": "Member pricing options",
        "access_level": "member",
        "description": "Logged-in members can view relevant member-eligible pricing options.",
    },
    {
        "key": "contract_member_included_features",
        "label": "Contract member included features",
        "access_level": "contract_member",
        "description": "Foundation for features included with contract memberships.",
        "is_future_ready": True,
    },
    {
        "key": "no_contract_member_access",
        "label": "No-contract member access",
        "access_level": "no_contract_member",
        "description": "Foundation for no-contract member access decisions.",
        "is_future_ready": True,
    },
    {
        "key": "staff_pricing_management",
        "label": "Staff pricing management",
        "access_level": "staff",
        "description": "Staff can manage pricing catalog data.",
    },
    {
        "key": "admin_pricing_controls",
        "label": "Admin pricing controls",
        "access_level": "admin",
        "description": "Admins can manage sensitive pricing and staff settings.",
    },
    {
        "key": "internal_test_access",
        "label": "Internal test access",
        "access_level": "internal_test",
        "description": "Reserved for staff/internal test access to future catalog features.",
        "is_future_ready": True,
    },
    {
        "key": "premium_future_features",
        "label": "Future premium features",
        "access_level": "premium_future",
        "description": "Placeholder for future paid features. No online payment flow is active.",
        "is_future_ready": True,
    },
]
PRICING_CATALOG_SOURCE = "current Dreamz Fitness printed price list"
PRICING_CATALOG_INITIAL_SEED_DATE = date(2026, 5, 27)
PRICING_CATALOG_CURRENCY = "USD"
PRICING_CATALOG_GLOBAL_RULE = "All prices and fees are non-negotiable."
PRICING_CATEGORY_SEED = [
    ("memberships", "Memberships"),
    ("mcb_direct_debit_contracts", "MCB Direct Debit Contracts"),
    ("day_week_passes", "Day & Week Passes"),
    ("group_class_add_ons", "Group Class Add-ons"),
    ("delfins_resort_guests", "Delfins Resort Guests"),
    ("fees_other", "Fees & Other"),
    ("under_18", "Under 18"),
    ("external_trainer_b2b", "External Trainer Packages / B2B Services"),
    ("personal_training", "Personal Training"),
]
PRICING_ITEM_SEED = [
    {
        "seed_key": "membership-no-contract-1-month",
        "name": "No contract / 1 month",
        "category_key": "memberships",
        "description": "Flexible one month Dreamz Fitness membership.",
        "price_amount": 80,
        "billing_interval": "per_month",
        "duration": "1 month",
        "visibility": ["public", "members"],
        "terms": [
            "No long-term contract.",
            "Payment and membership changes are handled at the front desk.",
        ],
    },
    {
        "seed_key": "membership-6-month-contract",
        "name": "6 months contract",
        "category_key": "memberships",
        "description": "Six month Dreamz Fitness membership contract.",
        "price_amount": 420,
        "billing_interval": "one_time",
        "duration": "6 months",
        "visibility": ["public", "members"],
        "contract_only": True,
        "terms": [
            "6 months contract.",
            "Payment and membership changes are handled at the front desk.",
        ],
    },
    {
        "seed_key": "membership-12-month-contract",
        "name": "12 months contract",
        "category_key": "memberships",
        "description": "Twelve month Dreamz Fitness membership contract.",
        "price_amount": 720,
        "billing_interval": "one_time",
        "duration": "12 months",
        "visibility": ["public", "members"],
        "contract_only": True,
        "terms": [
            "12 months contract.",
            "Payment and membership changes are handled at the front desk.",
        ],
    },
    {
        "seed_key": "mcb-direct-debit-6-month-contract",
        "name": "6 months contract - MCB Direct Debit",
        "category_key": "mcb_direct_debit_contracts",
        "description": "Six month monthly MCB direct debit contract.",
        "price_amount": 70,
        "billing_interval": "per_month",
        "duration": "6 months",
        "visibility": ["public", "members"],
        "contract_only": True,
        "terms": [
            "MCB Bank Bonaire current account holders only.",
            "Direct Debit only.",
            "Current MCB Bank Bonaire accounts only.",
            "No savings accounts.",
            "No non-MCB bank accounts.",
            "No exceptions.",
            "Payment and contract setup are handled at the front desk.",
        ],
    },
    {
        "seed_key": "mcb-direct-debit-12-month-contract",
        "name": "12 months contract - MCB Direct Debit",
        "category_key": "mcb_direct_debit_contracts",
        "description": "Twelve month monthly MCB direct debit contract.",
        "price_amount": 60,
        "billing_interval": "per_month",
        "duration": "12 months",
        "visibility": ["public", "members"],
        "contract_only": True,
        "terms": [
            "MCB Bank Bonaire current account holders only.",
            "Direct Debit only.",
            "Current MCB Bank Bonaire accounts only.",
            "No savings accounts.",
            "No non-MCB bank accounts.",
            "No exceptions.",
            "Payment and contract setup are handled at the front desk.",
        ],
    },
    {
        "seed_key": "group-pt-addon-5x-week",
        "name": "Add-on Group PT 5x a week",
        "category_key": "group_class_add_ons",
        "description": "Monthly group personal training add-on.",
        "price_amount": 85,
        "billing_interval": "per_month",
        "visibility": ["public", "members"],
        "terms": [
            "Add-on for group personal training.",
            "Payment and changes are handled at the front desk.",
        ],
    },
    {
        "seed_key": "personal-training-1-on-1",
        "name": "1-on-1 Personal Training",
        "category_key": "personal_training",
        "description": "One personal training session.",
        "price_amount": 35,
        "billing_interval": "per_session",
        "visibility": ["public", "members"],
        "terms": [
            "Per personal training session.",
            "Booking and payment are handled at the front desk or directly according to Dreamz Fitness procedures.",
        ],
    },
    {
        "seed_key": "pass-1-day",
        "name": "1 Day Pass",
        "category_key": "day_week_passes",
        "description": "Single day Dreamz Fitness access pass.",
        "price_amount": 20,
        "billing_interval": "one_time",
        "duration": "1 day",
        "visibility": ["public"],
        "terms": ["Valid for one day."],
    },
    {
        "seed_key": "pass-1-week",
        "name": "1 Week Pass",
        "category_key": "day_week_passes",
        "description": "One week Dreamz Fitness access pass.",
        "price_amount": 45,
        "billing_interval": "one_time",
        "duration": "1 week",
        "visibility": ["public"],
        "terms": ["A week pass is valid for 6 consecutive entry days."],
    },
    {
        "seed_key": "pass-2-week",
        "name": "2 Week Pass",
        "category_key": "day_week_passes",
        "description": "Two week Dreamz Fitness access pass.",
        "price_amount": 60,
        "billing_interval": "one_time",
        "duration": "2 weeks",
        "visibility": ["public"],
        "terms": ["Week passes are based on consecutive entry days."],
    },
    {
        "seed_key": "pass-3-week",
        "name": "3 Week Pass",
        "category_key": "day_week_passes",
        "description": "Three week Dreamz Fitness access pass.",
        "price_amount": 70,
        "billing_interval": "one_time",
        "duration": "3 weeks",
        "visibility": ["public"],
        "terms": ["Week passes are based on consecutive entry days."],
    },
    {
        "seed_key": "under-18-all-inclusive",
        "name": "Under 18 All Inclusive",
        "category_key": "under_18",
        "description": "All inclusive membership for members under 18.",
        "price_amount": 55,
        "billing_interval": "per_month",
        "visibility": ["public", "members"],
        "terms": ["Under 18 years old.", "All inclusive membership."],
    },
    {
        "seed_key": "delfins-unlimited-fitness-no-classes",
        "name": "Delfins Resort Guests - Unlimited Fitness, no classes",
        "category_key": "delfins_resort_guests",
        "description": "Included fitness access for Delfins Resort guests, without classes.",
        "price_amount": 0,
        "billing_interval": "included_free",
        "visibility": ["public"],
        "terms": [
            "Delfins Resort guests have unlimited fitness access without classes.",
            "Classes are not included.",
        ],
    },
    {
        "seed_key": "delfins-classes-day-pass",
        "name": "Delfins Resort Guests - Add-on classes day pass",
        "category_key": "delfins_resort_guests",
        "description": "Day class add-on for Delfins Resort guests.",
        "price_amount": 10,
        "billing_interval": "one_time",
        "duration": "1 day",
        "visibility": ["public"],
        "terms": ["Add-on for classes for Delfins Resort guests."],
    },
    {
        "seed_key": "delfins-classes-1-week-pass",
        "name": "Delfins Resort Guests - Add-on classes 1 week pass",
        "category_key": "delfins_resort_guests",
        "description": "One week class add-on for Delfins Resort guests.",
        "price_amount": 35,
        "billing_interval": "one_time",
        "duration": "1 week",
        "visibility": ["public"],
        "terms": ["Add-on classes for Delfins Resort guests."],
    },
    {
        "seed_key": "delfins-classes-2-week-pass",
        "name": "Delfins Resort Guests - Add-on classes 2 week pass",
        "category_key": "delfins_resort_guests",
        "description": "Two week class add-on for Delfins Resort guests.",
        "price_amount": 50,
        "billing_interval": "one_time",
        "duration": "2 weeks",
        "visibility": ["public"],
        "terms": ["Add-on classes for Delfins Resort guests."],
    },
    {
        "seed_key": "delfins-classes-3-week-pass",
        "name": "Delfins Resort Guests - Add-on classes 3 week pass",
        "category_key": "delfins_resort_guests",
        "description": "Three week class add-on for Delfins Resort guests.",
        "price_amount": 60,
        "billing_interval": "one_time",
        "duration": "3 weeks",
        "visibility": ["public"],
        "terms": ["Add-on classes for Delfins Resort guests."],
    },
    {
        "seed_key": "fee-first-time-registration",
        "name": "First-time registration fee",
        "category_key": "fees_other",
        "description": "Registration fee for new memberships.",
        "price_amount": 20,
        "billing_interval": "one_time",
        "visibility": ["public", "members"],
        "terms": ["Applies to contract and no-contract memberships."],
    },
    {
        "seed_key": "fee-rfid-key-tag-upgrade",
        "name": "Upgrade RFID entry key-tag",
        "category_key": "fees_other",
        "description": "RFID entry key-tag upgrade fee.",
        "price_amount": 10,
        "billing_interval": "one_time",
        "visibility": ["public", "members"],
        "terms": ["Upgrade fee for RFID entry key-tag."],
    },
    {
        "seed_key": "fee-towel-rental",
        "name": "Towel rental",
        "category_key": "fees_other",
        "description": "Front desk towel rental.",
        "price_amount": 3,
        "billing_interval": "one_time",
        "visibility": ["public", "members"],
        "terms": [
            "Use of a towel is mandatory.",
            "Towel rental is available at the front desk.",
        ],
    },
    {
        "seed_key": "fee-dreamz-towel-sale",
        "name": "Dreamz Fitness towel sale",
        "category_key": "fees_other",
        "description": "Dreamz Fitness towel purchase.",
        "price_amount": 25,
        "billing_interval": "one_time",
        "visibility": ["public", "members"],
        "terms": ["Dreamz Fitness towel purchase."],
    },
    {
        "seed_key": "b2b-external-personal-trainer-package",
        "name": "External Personal Trainer Package",
        "category_key": "external_trainer_b2b",
        "description": "B2B facility access package for external personal trainers.",
        "price_amount": 250,
        "billing_interval": "per_month",
        "visibility": ["public_business", "staff_only"],
        "member_eligible": False,
        "terms": [
            "This is not a membership product for regular Dreamz Fitness members.",
            "This is not a member add-on.",
            "This is a B2B / external trainer access package.",
            "External personal trainers can pay Dreamz Fitness $250 per month to use the Dreamz Fitness facilities for themselves and for training their own clients.",
            "The external personal trainer is responsible for their own clients.",
            "Dreamz Fitness is not responsible for the external trainer's coaching quality, training advice, payment collection, cancellations, disputes or client relationship.",
            "The external trainer handles their own personal training session pricing and payment directly with their own clients.",
            "Every client trained by the external trainer must either be an active Dreamz Fitness member, regardless of membership type, or purchase a valid Dreamz Fitness day pass.",
            "For training Delfins Resort guests, Dreamz Fitness charges $10 per guest.",
            "This package does not include a Dreamz Fitness membership for the trainer's clients.",
            "This package should not be shown as a normal member upgrade or member add-on.",
            "It can be shown in a public/business info section such as Are you a personal trainer? if enabled.",
            "Staff/admin must be able to manage this item.",
        ],
    },
]

GROUP_CLASS_TYPE_SEED = {
    "BODYPUMP": {
        "category": "strength",
        "intensity": "medium_high",
        "muscle_focus": "full_body",
        "cardio_load": "medium",
        "strength_load": "high",
        "recovery_impact": "medium_high",
        "impact_level": "low_medium",
        "pregnancy_safety_level": "caution",
    },
    "BODYCOMBAT": {
        "category": "cardio",
        "intensity": "high",
        "muscle_focus": "full_body_cardio",
        "cardio_load": "high",
        "strength_load": "low_medium",
        "recovery_impact": "high",
        "impact_level": "high",
        "pregnancy_safety_level": "not_recommended_or_requires_modification",
    },
    "ZUMBA": {
        "category": "cardio",
        "intensity": "medium",
        "muscle_focus": "full_body_cardio",
        "cardio_load": "medium",
        "strength_load": "low",
        "recovery_impact": "medium",
        "impact_level": "medium",
        "pregnancy_safety_level": "caution",
    },
    "TOTAL BODY": {
        "category": "hybrid",
        "intensity": "medium_high",
        "muscle_focus": "full_body",
        "cardio_load": "medium",
        "strength_load": "medium_high",
        "recovery_impact": "medium_high",
        "impact_level": "medium",
        "pregnancy_safety_level": "caution",
    },
    "YOGA": {
        "category": "mobility_recovery",
        "intensity": "low_medium",
        "muscle_focus": "mobility",
        "cardio_load": "low",
        "strength_load": "low",
        "recovery_impact": "low",
        "impact_level": "low",
        "pregnancy_safety_level": "suitable_or_requires_modification_after_16_weeks",
    },
    "SPINNING": {
        "category": "cardio",
        "intensity": "medium_high",
        "muscle_focus": "lower_body_cardio",
        "cardio_load": "high",
        "strength_load": "low_medium",
        "recovery_impact": "medium_high",
        "impact_level": "low",
        "pregnancy_safety_level": "caution",
    },
    "STEP AEROBICS": {
        "category": "cardio",
        "intensity": "medium_high",
        "muscle_focus": "lower_body_cardio",
        "cardio_load": "high",
        "strength_load": "low_medium",
        "recovery_impact": "medium_high",
        "impact_level": "medium_high",
        "pregnancy_safety_level": "caution_or_not_recommended_depending_on_balance_and_weeks",
    },
    "BOOTY SHAPE": {
        "category": "strength",
        "intensity": "medium_high",
        "muscle_focus": "glutes_lower_body",
        "cardio_load": "low_medium",
        "strength_load": "medium_high",
        "recovery_impact": "medium_high",
        "impact_level": "low_medium",
        "pregnancy_safety_level": "caution",
    },
    "PILATES": {
        "category": "core_mobility",
        "intensity": "low_medium",
        "muscle_focus": "core_mobility",
        "cardio_load": "low",
        "strength_load": "low_medium",
        "recovery_impact": "low_medium",
        "impact_level": "low",
        "pregnancy_safety_level": "suitable_or_requires_modification",
    },
    "RESERVED": {
        "category": "unavailable_reserved",
        "intensity": "none",
        "muscle_focus": None,
        "cardio_load": None,
        "strength_load": None,
        "recovery_impact": None,
        "impact_level": None,
        "pregnancy_safety_level": None,
        "default_bookable": False,
        "default_publish": False,
        "description": "Blocks the room/time in admin schedule and is hidden from members by default.",
    },
}

GROUP_CLASS_WEEKLY_SCHEDULE_SEED = [
    (0, "08:00", "09:00", "BODYPUMP", "AEROBICS ROOM", None),
    (0, "18:00", "19:00", "BODYCOMBAT", "AEROBICS ROOM", None),
    (0, "19:00", "20:00", "ZUMBA", "AEROBICS ROOM", None),
    (0, "20:00", "21:00", "BODYPUMP", "AEROBICS ROOM", None),
    (1, "08:00", "09:00", "TOTAL BODY", "AEROBICS ROOM", None),
    (1, "17:00", "18:00", "RESERVED", "DOJO", None),
    (1, "18:00", "19:00", "BODYPUMP", "AEROBICS ROOM", None),
    (1, "19:00", "20:00", "TOTAL BODY", "AEROBICS ROOM", None),
    (2, "08:00", "09:00", "BODYPUMP", "AEROBICS ROOM", None),
    (2, "09:00", "10:00", "YOGA", "AEROBICS ROOM", None),
    (2, "18:00", "19:00", "ZUMBA", "AEROBICS ROOM", None),
    (2, "18:00", "19:00", "SPINNING", "SPINNING ROOM", None),
    (3, "08:00", "09:00", "STEP AEROBICS", "DOJO", None),
    (3, "17:00", "18:00", "RESERVED", "DOJO", None),
    (3, "18:00", "19:00", "BOOTY SHAPE", "AEROBICS ROOM", None),
    (3, "20:00", "21:00", "BODYPUMP", "AEROBICS ROOM", None),
    (4, "08:00", "09:00", "TOTAL BODY", "AEROBICS ROOM", None),
    (4, "09:00", "10:00", "BODYPUMP", "AEROBICS ROOM", None),
    (4, "18:00", "19:00", "ZUMBA", "AEROBICS ROOM", None),
    (4, "18:00", "19:00", "SPINNING", "SPINNING ROOM", None),
    (5, "08:00", "09:00", "PILATES", "AEROBICS ROOM", "NEW"),
    (5, "09:00", "10:00", "ZUMBA", "AEROBICS ROOM", None),
    (5, "10:00", "11:00", "BODYCOMBAT", "AEROBICS ROOM", None),
    (5, "11:00", "12:00", "BODYPUMP", "AEROBICS ROOM", None),
]


def portal_timezone():
    offset_hours = int(os.getenv("PORTAL_TIMEZONE_OFFSET_HOURS", DEFAULT_PORTAL_TIMEZONE_OFFSET_HOURS))
    return timezone(timedelta(hours=offset_hours), name="Dreamz local time")


def local_datetime(value):
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(portal_timezone())


def current_portal_datetime():
    return local_datetime(datetime.now(timezone.utc))


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


def runtime_column_definition(column):
    return column.type.compile(dialect=db.engine.dialect)


def ensure_runtime_model_columns():
    for mapper in db.Model.registry.mappers:
        table = mapper.local_table
        for column in table.columns:
            if column.primary_key:
                continue
            ensure_model_column(table.name, column.name, runtime_column_definition(column))


def sql_bool(value):
    if db.engine.dialect.name == "postgresql":
        return "TRUE" if value else "FALSE"
    return "1" if value else "0"


def backfill_runtime_schema_defaults():
    from sqlalchemy import text

    true_value = sql_bool(True)
    db.session.execute(text(
        f"UPDATE staff_user SET is_active = {true_value} WHERE is_active IS NULL"
    ))
    db.session.execute(text(
        "UPDATE staff_user SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE staff_user SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE sync_run SET status = 'received' WHERE status IS NULL"
    ))
    db.session.execute(text(
        "UPDATE sync_run SET members_received = 0 WHERE members_received IS NULL"
    ))
    db.session.execute(text(
        "UPDATE sync_run SET members_new = 0 WHERE members_new IS NULL"
    ))
    db.session.execute(text(
        "UPDATE sync_run SET members_updated = 0 WHERE members_updated IS NULL"
    ))
    db.session.execute(text(
        "UPDATE sync_run SET documents_received = 0 WHERE documents_received IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE group_class_type SET default_bookable = {true_value} WHERE default_bookable IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE group_class_type SET default_publish = {true_value} WHERE default_publish IS NULL"
    ))
    db.session.execute(text(
        "UPDATE group_class_type SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE group_class_type SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE group_class_occurrence SET status = 'scheduled' WHERE status IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE group_class_occurrence SET is_bookable = {true_value} WHERE is_bookable IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE group_class_occurrence SET is_published = {true_value} WHERE is_published IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE group_class_occurrence SET blocks_room = {true_value} WHERE blocks_room IS NULL"
    ))
    db.session.execute(text(
        "UPDATE group_class_occurrence SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE group_class_occurrence SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE pricing_category SET is_active = {true_value} WHERE is_active IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_category SET sort_order = 0 WHERE sort_order IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_category SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_category SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET price_amount = 0 WHERE price_amount IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET currency = 'USD' WHERE currency IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET billing_interval = 'one_time' WHERE billing_interval IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET visibility = 'staff_only' WHERE visibility IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE pricing_item SET is_active = {true_value} WHERE is_active IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET sort_order = 0 WHERE sort_order IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE pricing_item SET requires_front_desk_handling = {true_value} WHERE requires_front_desk_handling IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE pricing_item SET online_payment_available = {sql_bool(False)} WHERE online_payment_available IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE pricing_item SET member_eligible = {true_value} WHERE member_eligible IS NULL"
    ))
    db.session.execute(text(
        f"UPDATE pricing_item SET contract_only = {sql_bool(False)} WHERE contract_only IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
    ))
    db.session.execute(text(
        "UPDATE pricing_item SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))
    db.session.commit()


def ensure_runtime_schema():
    if app.config.get("_RUNTIME_SCHEMA_READY") and not app.config.get("TESTING"):
        return

    db.create_all()
    ensure_runtime_model_columns()
    backfill_runtime_schema_defaults()
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
    ensure_model_column("cancellation_request", "contract_id", "VARCHAR")
    ensure_model_column("cancellation_request", "membership_id", "VARCHAR")
    ensure_model_column("cancellation_request", "cancellation_window_open_date", "DATE")
    ensure_model_column("cancellation_request", "cancellation_window_close_date", "DATE")
    ensure_model_column("cancellation_request", "confirmation_number", "VARCHAR")
    ensure_model_column("cancellation_request", "pdf_receipt_url", "VARCHAR")
    ensure_model_column("cancellation_request", "member_confirmation_sent_at", "DATETIME")
    ensure_model_column("cancellation_request", "staff_notification_sent_at", "DATETIME")
    ensure_sqlite_model_column("email_log", "html_body", "TEXT")
    ensure_model_column("email_log", "bcc_addresses", "TEXT")
    ensure_sqlite_model_column("email_log", "reviewed_at", "DATETIME")
    ensure_sqlite_model_column("email_log", "reviewed_by", "VARCHAR")
    ensure_model_column("sync_run", "change_summary", "TEXT")
    ensure_model_column("coach_profile", "home_equipment", "TEXT")
    ensure_model_column("coach_profile", "sex", "VARCHAR")
    ensure_model_column("coach_profile", "pregnancy_status", "VARCHAR")
    ensure_model_column("coach_profile", "gestational_weeks", "INTEGER")
    ensure_model_column("coach_profile", "expected_due_date", "DATE")
    ensure_model_column("coach_profile", "pre_pregnancy_weight_kg", "FLOAT")
    ensure_model_column("coach_profile", "multiple_pregnancy", "VARCHAR")
    ensure_model_column("coach_profile", "provider_cleared_exercise", "VARCHAR")
    ensure_model_column("coach_profile", "provider_restrictions", "TEXT")
    ensure_model_column("coach_profile", "pregnancy_symptoms", "TEXT")
    ensure_model_column("coach_profile", "pregnancy_consent", "BOOLEAN")
    ensure_model_column("coach_plan", "plan_version", "VARCHAR")
    ensure_model_column("group_class_occurrence", "status", "VARCHAR DEFAULT 'scheduled' NOT NULL")
    ensure_model_column("member", "password_hash", "VARCHAR(512)")
    ensure_model_column("member", "password_set_at", "TIMESTAMP")
    db.create_all()
    seed_default_settings()
    seed_default_staff_users()
    seed_group_class_schedule()
    seed_pricing_catalog()
    seed_feature_access_rules()
    seed_legal_documents()
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


def configured_staff_user_fallbacks():
    return {
        app.config["STAFF_ADMIN_USERNAME"].strip().lower(): SimpleNamespace(
            username=app.config["STAFF_ADMIN_USERNAME"].strip().lower(),
            password_hash=generate_password_hash(app.config["STAFF_ADMIN_PASSWORD"]),
            role="admin",
        ),
        app.config["STAFF_MANAGER_USERNAME"].strip().lower(): SimpleNamespace(
            username=app.config["STAFF_MANAGER_USERNAME"].strip().lower(),
            password_hash=generate_password_hash(app.config["STAFF_MANAGER_PASSWORD"]),
            role="manager",
        ),
    }


def parse_group_class_seed_time(value):
    return datetime.strptime(value, "%H:%M").time()


def group_class_seed_key(day_of_week, start_at, end_at, class_name, room):
    key_parts = [
        GROUP_CLASS_SCHEDULE_LAST_UPDATED_FROM_PDF.isoformat(),
        str(day_of_week),
        start_at.replace(":", ""),
        end_at.replace(":", ""),
        class_name.lower().replace(" ", "-"),
        room.lower().replace(" ", "-"),
    ]
    return "-".join(key_parts)


def seed_group_class_schedule():
    schedule = GroupClassSchedule.query.filter_by(name=GROUP_CLASS_SCHEDULE_NAME).first()
    if not schedule:
        schedule = GroupClassSchedule(
            name=GROUP_CLASS_SCHEDULE_NAME,
            status="published",
            published_at=datetime.now(),
        )
        db.session.add(schedule)

    schedule.source = schedule.source or GROUP_CLASS_SCHEDULE_SOURCE
    schedule.timezone = schedule.timezone or GROUP_CLASS_SCHEDULE_TIMEZONE
    schedule.last_updated_from_pdf = schedule.last_updated_from_pdf or GROUP_CLASS_SCHEDULE_LAST_UPDATED_FROM_PDF
    schedule.updated_at = datetime.now()

    class_types = {}
    for class_name, defaults in GROUP_CLASS_TYPE_SEED.items():
        class_type = GroupClassType.query.filter_by(name=class_name).first()
        if not class_type:
            class_type = GroupClassType(name=class_name)
            db.session.add(class_type)

        for field, value in defaults.items():
            if getattr(class_type, field, None) in (None, ""):
                setattr(class_type, field, value)
        if class_type.default_bookable is None:
            class_type.default_bookable = defaults.get("default_bookable", True)
        if class_type.default_publish is None:
            class_type.default_publish = defaults.get("default_publish", True)
        class_type.updated_at = datetime.now()
        class_types[class_name] = class_type

    db.session.flush()

    for day_of_week, start_at, end_at, class_name, room, note in GROUP_CLASS_WEEKLY_SCHEDULE_SEED:
        seed_key = group_class_seed_key(day_of_week, start_at, end_at, class_name, room)
        if GroupClassOccurrence.query.filter_by(seed_key=seed_key).first():
            continue

        class_type = class_types[class_name]
        is_reserved = class_name == "RESERVED"
        occurrence = GroupClassOccurrence(
            schedule_id=schedule.id,
            class_type_id=class_type.id,
            day_of_week=day_of_week,
            start_time=parse_group_class_seed_time(start_at),
            end_time=parse_group_class_seed_time(end_at),
            room=room,
            note=note,
            status="reserved" if is_reserved else "scheduled",
            is_bookable=not is_reserved and class_type.default_bookable,
            is_published=not is_reserved and class_type.default_publish,
            blocks_room=True,
            seed_key=seed_key,
        )
        db.session.add(occurrence)

    db.session.commit()
    return schedule


def pricing_terms_json(terms):
    return json.dumps(terms or [], ensure_ascii=True)


def pricing_visibility_value(visibility):
    if isinstance(visibility, str):
        return visibility
    return ",".join(visibility or [])


def pricing_visibility_list(item):
    if not item or not item.visibility:
        return []
    return [value.strip() for value in item.visibility.split(",") if value.strip()]


def pricing_terms_list(item):
    if not item or not item.terms:
        return []
    try:
        terms = json.loads(item.terms)
    except (TypeError, ValueError):
        return [line.strip() for line in str(item.terms).splitlines() if line.strip()]
    return terms if isinstance(terms, list) else []


def pricing_terms_from_form(value):
    return pricing_terms_json([line.strip() for line in str(value or "").splitlines() if line.strip()])


def pricing_item_snapshot(item):
    return {
        "name": item.name,
        "category_key": item.category_key,
        "price_amount": item.price_amount,
        "currency": item.currency,
        "billing_interval": item.billing_interval,
        "duration": item.duration,
        "visibility": item.visibility,
        "is_active": item.is_active,
        "effective_from": item.effective_from.isoformat() if item.effective_from else None,
        "effective_to": item.effective_to.isoformat() if item.effective_to else None,
        "sort_order": item.sort_order,
        "requires_front_desk_handling": item.requires_front_desk_handling,
        "online_payment_available": item.online_payment_available,
        "member_eligible": item.member_eligible,
        "contract_only": item.contract_only,
        "terms": pricing_terms_list(item),
    }


def ensure_pricing_business_rules(item):
    visibility = set(pricing_visibility_list(item))
    if item.category_key == "external_trainer_b2b" or item.seed_key == "b2b-external-personal-trainer-package":
        item.member_eligible = False
        visibility.discard("members")
        if not visibility:
            visibility.update(["public_business", "staff_only"])
        if "public" in visibility:
            visibility.discard("public")
            visibility.add("public_business")
        if "public_business" not in visibility and "staff_only" not in visibility:
            visibility.update(["public_business", "staff_only"])

    if item.category_key == "mcb_direct_debit_contracts" or "mcb direct debit" in (item.name or "").lower():
        required_terms = [
            "Current MCB Bank Bonaire accounts only.",
            "Direct Debit only.",
            "No exceptions.",
        ]
        terms = pricing_terms_list(item)
        terms_lower = " ".join(terms).lower()
        for required in required_terms:
            if required.lower() not in terms_lower:
                terms.append(required)
        item.terms = pricing_terms_json(terms)

    item.visibility = ",".join(option for option in PRICING_VISIBILITY_OPTIONS if option in visibility)


def pricing_admin_context():
    ensure_runtime_schema()
    seed_pricing_catalog()
    category_filter = request.args.get("category", "").strip()
    status_filter = request.args.get("status", "").strip()
    visibility_filter = request.args.get("visibility", "").strip()
    query = PricingItem.query
    if category_filter:
        query = query.filter(PricingItem.category_key == category_filter)
    if status_filter == "active":
        query = query.filter(PricingItem.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.filter(PricingItem.is_active.is_(False))
    if visibility_filter:
        query = query.filter(PricingItem.visibility.like(f"%{visibility_filter}%"))
    categories = PricingCategory.query.order_by(PricingCategory.sort_order.asc(), PricingCategory.name.asc()).all()
    items = query.order_by(PricingItem.sort_order.asc(), PricingItem.name.asc()).all()
    changes = PricingChangeLog.query.order_by(PricingChangeLog.created_at.desc()).limit(25).all()
    return {
        "pricing_items": items,
        "pricing_categories": categories,
        "pricing_changes": changes,
        "pricing_billing_intervals": PRICING_BILLING_INTERVALS,
        "pricing_visibility_options": PRICING_VISIBILITY_OPTIONS,
        "selected_category": category_filter,
        "selected_status": status_filter,
        "selected_visibility": visibility_filter,
        "pricing_global_rule": setting_value("pricing_catalog_global_rule", PRICING_CATALOG_GLOBAL_RULE),
        "pricing_terms_list": pricing_terms_list,
        "pricing_visibility_list": pricing_visibility_list,
    }


def empty_pricing_admin_context():
    return {
        "pricing_items": [],
        "pricing_categories": [],
        "pricing_changes": [],
        "pricing_billing_intervals": PRICING_BILLING_INTERVALS,
        "pricing_visibility_options": PRICING_VISIBILITY_OPTIONS,
        "selected_category": request.args.get("category", "").strip(),
        "selected_status": request.args.get("status", "").strip(),
        "selected_visibility": request.args.get("visibility", "").strip(),
        "pricing_global_rule": PRICING_CATALOG_GLOBAL_RULE,
        "pricing_terms_list": pricing_terms_list,
        "pricing_visibility_list": pricing_visibility_list,
    }


def current_legal_version(document):
    if not document:
        return None
    current = (
        LegalDocumentVersion.query
        .filter_by(document_id=document.id, is_current=True)
        .order_by(LegalDocumentVersion.effective_from.desc(), LegalDocumentVersion.id.desc())
        .first()
    )
    if current:
        return current
    return (
        LegalDocumentVersion.query
        .filter_by(document_id=document.id)
        .order_by(LegalDocumentVersion.id.desc())
        .first()
    )


def terms_admin_context():
    ensure_runtime_schema()
    documents = LegalDocument.query.order_by(LegalDocument.sort_order.asc(), LegalDocument.title.asc()).all()
    versions = LegalDocumentVersion.query.order_by(LegalDocumentVersion.created_at.desc(), LegalDocumentVersion.id.desc()).limit(100).all()
    translations = LegalTranslation.query.order_by(LegalTranslation.language.asc(), LegalTranslation.id.asc()).all()
    translations_by_version = {}
    for translation in translations:
        translations_by_version.setdefault(translation.version_id, {})[translation.language] = translation
    document_versions = {document.id: current_legal_version(document) for document in documents}
    rules = RequiredAgreementRule.query.order_by(RequiredAgreementRule.id.asc()).all()
    applications = MembershipApplication.query.order_by(MembershipApplication.created_at.desc(), MembershipApplication.id.desc()).limit(100).all()
    signed_documents = MemberSignedDocument.query.order_by(MemberSignedDocument.uploaded_at.desc(), MemberSignedDocument.id.desc()).limit(100).all()
    signatures = DigitalSignatureRecord.query.order_by(DigitalSignatureRecord.signed_at.desc(), DigitalSignatureRecord.id.desc()).limit(100).all()
    signed_pdfs = SignedPdfRecord.query.order_by(SignedPdfRecord.generated_at.desc(), SignedPdfRecord.id.desc()).limit(100).all()
    cancellations = CancellationRequest.query.order_by(CancellationRequest.requested_at.desc(), CancellationRequest.id.desc()).limit(100).all()
    warnings = []
    if any(document.legal_review_needed for document in documents):
        warnings.append(translated_text("terms_warning_legal_review_needed", current_language()))
    if any((document.internal_notes or "").lower().find("legacy 6-month") >= 0 for document in documents):
        warnings.append(translated_text("terms_warning_legacy_contract", current_language()))
    missing_translation_count = 0
    draft_translation_count = 0
    for document in documents:
        version = document_versions.get(document.id)
        if not version:
            warnings.append(translated_text("terms_warning_missing_active_version", current_language()))
            continue
        language_map = translations_by_version.get(version.id, {})
        missing_translation_count += len([language for language in LANGUAGES if language not in language_map])
        draft_translation_count += len([
            translation for translation in language_map.values()
            if translation.translation_status == "draft"
        ])
    if missing_translation_count:
        warnings.append(translated_text("terms_warning_missing_translations", current_language(), count=missing_translation_count))
    if draft_translation_count:
        warnings.append(translated_text("terms_warning_draft_translations", current_language(), count=draft_translation_count))
    if RequiredAgreementRule.query.filter_by(active=True).count() == 0:
        warnings.append(translated_text("terms_warning_required_rules_missing", current_language()))
    pending_application_count = MembershipApplication.query.filter(
        MembershipApplication.status.in_(["submitted", "signed", "pending_frontdesk_payment", "pending_staff_activation"])
    ).count()
    if pending_application_count:
        warnings.append(translated_text("terms_warning_pending_applications", current_language(), count=pending_application_count))

    return {
        "agreement_categories": AgreementCategory.query.order_by(AgreementCategory.sort_order.asc(), AgreementCategory.name.asc()).all(),
        "legal_documents": documents,
        "legal_versions": versions,
        "document_versions": document_versions,
        "translations_by_version": translations_by_version,
        "required_rules": rules,
        "membership_applications": applications,
        "application_statuses": MEMBERSHIP_APPLICATION_STATUSES,
        "member_signed_documents": signed_documents,
        "digital_signatures": signatures,
        "signed_pdfs": signed_pdfs,
        "cancellation_requests": cancellations,
        "legal_languages": LANGUAGES,
        "legal_review_statuses": LEGAL_REVIEW_STATUSES,
        "translation_statuses": LEGAL_TRANSLATION_STATUSES,
        "terms_admin_warnings": warnings,
        "legal_translation_notice": setting_value("legal_translation_review_notice", LEGAL_TRANSLATION_DRAFT_NOTICE),
        "current_legal_version": current_legal_version,
    }


def empty_terms_admin_context():
    return {
        "agreement_categories": [],
        "legal_documents": [],
        "legal_versions": [],
        "document_versions": {},
        "translations_by_version": {},
        "required_rules": [],
        "membership_applications": [],
        "application_statuses": MEMBERSHIP_APPLICATION_STATUSES,
        "member_signed_documents": [],
        "digital_signatures": [],
        "signed_pdfs": [],
        "cancellation_requests": [],
        "legal_languages": LANGUAGES,
        "legal_review_statuses": LEGAL_REVIEW_STATUSES,
        "translation_statuses": LEGAL_TRANSLATION_STATUSES,
        "terms_admin_warnings": [],
        "legal_translation_notice": LEGAL_TRANSLATION_DRAFT_NOTICE,
        "current_legal_version": current_legal_version,
    }


def pricing_item_visible_to(item, visibility):
    return visibility in pricing_visibility_list(item)


def active_pricing_items_for_visibility(visibility_values):
    today = date.today()
    query = PricingItem.query.filter(PricingItem.is_active.is_(True))
    query = query.filter(db.or_(PricingItem.effective_from.is_(None), PricingItem.effective_from <= today))
    query = query.filter(db.or_(PricingItem.effective_to.is_(None), PricingItem.effective_to >= today))
    visibility_filters = [
        PricingItem.visibility.like(f"%{visibility}%")
        for visibility in visibility_values
    ]
    if visibility_filters:
        query = query.filter(db.or_(*visibility_filters))
    return query.order_by(PricingItem.sort_order.asc(), PricingItem.name.asc()).all()


def pricing_items_by_category(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item.category_key, []).append(item)
    return grouped


def public_pricing_context():
    try:
        ensure_runtime_schema()
        seed_pricing_catalog()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while rendering public pricing.")
    try:
        items = [
            item for item in active_pricing_items_for_visibility(["public", "public_business"])
            if "staff_only" not in pricing_visibility_list(item) or pricing_item_visible_to(item, "public_business")
        ]
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Public pricing catalog unavailable; rendering empty pricing page.")
        items = []
    try:
        categories = PricingCategory.query.order_by(PricingCategory.sort_order.asc(), PricingCategory.name.asc()).all()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Public pricing categories unavailable; rendering empty category list.")
        categories = []
    try:
        global_rule = setting_value("pricing_catalog_global_rule", PRICING_CATALOG_GLOBAL_RULE)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Pricing global rule unavailable; using default rule.")
        global_rule = PRICING_CATALOG_GLOBAL_RULE
    return {
        "pricing_categories": categories,
        "pricing_items_by_category": pricing_items_by_category(items),
        "pricing_terms_list": pricing_terms_list,
        "pricing_global_rule": global_rule,
    }


def member_pricing_context(member):
    policy = cancellation_policy_for_member(member)
    language = current_language()
    try:
        ensure_runtime_schema()
        seed_pricing_catalog()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while rendering member pricing.")
    try:
        items = [
            item for item in active_pricing_items_for_visibility(["members"])
            if item.member_eligible and item.category_key != "external_trainer_b2b"
        ]
        member_pricing_unavailable = False
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Member pricing catalog unavailable; rendering empty member pricing page.")
        items = []
        member_pricing_unavailable = True
    return {
        "member": member,
        "display_name": display_member_name(member.name),
        "current_membership": {
            "plan_type": member.plan_type or translated_text("not_available", current_language()),
            "contract_type": member.contract_type or translated_text("not_available", current_language()),
            "billing_amount": member.billing_amount or 0,
            "next_payment": member.next_payment or compute_next_payment(member),
            "current_term_end": fmt_policy_date(policy.current_term_end, language),
            "renewal_status": translated_text("automatic_renewal", language) if is_contract_member_record(member) else translated_text("not_available", language),
        },
        "membership_options": [item for item in items if item.category_key in {"memberships", "mcb_direct_debit_contracts", "under_18"}],
        "addon_options": [item for item in items if item.category_key in {"group_class_add_ons", "personal_training"}],
        "fee_options": [item for item in items if item.category_key == "fees_other"],
        "member_pricing_unavailable": member_pricing_unavailable,
        "pricing_terms_list": pricing_terms_list,
        "gym_balance": member.balance or 0,
    }


def member_relevant_legal_document_types(member):
    text = " ".join([member.plan_type or "", member.contract_type or "", member.billing_option or "", member.billing_type or ""]).lower()
    document_types = [
        "general_terms",
        "gym_rules",
        "liability_waiver",
        "media_security_consent",
        "payment_rules",
        "cancellation_renewal_rules",
        "personal_training_business_rules",
    ]
    if "6" in (member.contract_type or "") or "6 month" in text or "6-month" in text:
        document_types.append("membership_contract_6_months")
    if "12" in (member.contract_type or "") or "12 month" in text or "12-month" in text:
        document_types.append("membership_contract_12_months")
    if "direct debit" in text or "mcb" in text:
        document_types.append("direct_debit_mandate")
    if "group pt" in text or "group personal" in text:
        document_types.append("group_pt_addon_waiver")
    return document_types


def application_required_document_types(contract_term, payment_method, selected_add_ons=None):
    selected_add_ons = selected_add_ons or []
    document_types = [
        "membership_application_form",
        "general_terms",
        "gym_rules",
        "liability_waiver",
        "media_security_consent",
        "personal_training_business_rules",
    ]
    if contract_term == "6_months":
        document_types.extend(["membership_contract_6_months", "cancellation_renewal_rules", "payment_rules"])
    elif contract_term == "12_months":
        document_types.extend(["membership_contract_12_months", "cancellation_renewal_rules", "payment_rules"])
    else:
        document_types.append("payment_rules")
    if payment_method == "mcb_direct_debit_monthly":
        document_types.extend(["direct_debit_mandate", "payment_rules"])
    if "group_pt" in selected_add_ons:
        document_types.append("group_pt_addon_waiver")
    return list(dict.fromkeys(document_types))


def current_versions_for_document_types(document_types):
    documents = (
        LegalDocument.query
        .filter(LegalDocument.active.is_(True))
        .filter(LegalDocument.document_type.in_(document_types))
        .order_by(LegalDocument.sort_order.asc(), LegalDocument.title.asc())
        .all()
    )
    rows = []
    for document in documents:
        version = current_legal_version(document)
        if version:
            rows.append((document, version))
    return rows


def create_application_pdf_hash(application, document_type, audit_reference):
    payload = f"{application.id}|{document_type}|{audit_reference}|{application.email}|{datetime.now().isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pdf_escape(value):
    return str(value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def simple_text_pdf_bytes(title, lines):
    visible_lines = [title] + [str(line or "") for line in lines]
    text_commands = ["BT", "/F1 12 Tf", "50 792 Td", "14 TL"]
    for index, line in enumerate(visible_lines[:52]):
        prefix = "" if index == 0 else "T* "
        text_commands.append(f"{prefix}({pdf_escape(line[:115])}) Tj")
    text_commands.append("ET")
    stream = "\n".join(text_commands).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(pdf)


def printable_agreement_context(document_type, language, application=None):
    language = normalize_language(language)
    document = LegalDocument.query.filter_by(document_type=document_type).first_or_404()
    version = current_legal_version(document)
    if not version:
        abort(404)
    translation = legal_translation_for_version(version, language)
    return {
        "document": document,
        "version": version,
        "translation": translation,
        "language": language,
        "application": application,
        "company_name": "ABC Fitness & Health N.V.",
        "brand_name": "Dreamz Fitness Bonaire",
        "company_location": "Bonaire, Dutch Caribbean",
        "legal_review_notice": LEGAL_TRANSLATION_DRAFT_NOTICE if document.legal_review_needed or (translation and translation.translation_status == "draft") else "",
    }


def printable_agreement_pdf(document_type, language, application=None):
    context = printable_agreement_context(document_type, language, application=application)
    translation = context["translation"]
    version = context["version"]
    application_lines = []
    if application:
        application_lines = [
            f"Applicant: {application.applicant_first_name or ''} {application.applicant_last_name or ''}".strip(),
            f"Email: {application.email or ''}",
            f"Membership: {application.selected_membership_type or ''}",
            f"Payment method: {application.selected_payment_method or ''}",
            "Signed electronically through the Dreamz Fitness member portal.",
        ]
    lines = [
        context["company_name"],
        context["brand_name"],
        context["company_location"],
        f"Document type: {context['document'].document_type}",
        f"Version: {version.version}",
        f"Language: {context['language']}",
        f"Effective from: {version.effective_from.isoformat() if version.effective_from else 'Not available'}",
        context["legal_review_notice"],
        translation.plain_language_summary if translation else version.plain_language_summary,
        "",
        *(application_lines or ["Manual signature field: ______________________________", "Date: __________________"]),
        "",
        translation.full_legal_text if translation else version.full_legal_text,
    ]
    title = translation.title if translation else context["document"].title
    return simple_text_pdf_bytes(title, lines)


def member_agreements_context(member):
    try:
        ensure_runtime_schema()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while rendering member agreements.")
    language = current_language()
    document_types = member_relevant_legal_document_types(member)
    try:
        documents = (
            LegalDocument.query
            .filter(LegalDocument.active.is_(True))
            .filter(LegalDocument.document_type.in_(document_types))
            .order_by(LegalDocument.sort_order.asc(), LegalDocument.title.asc())
            .all()
        )
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Legal documents unavailable; rendering empty agreements center.")
        documents = []
    agreement_cards = []
    for document in documents:
        version = optional_dashboard_value("agreement_version", None, lambda document=document: current_legal_version(document))
        translation = optional_dashboard_value("agreement_translation", None, lambda version=version: legal_translation_for_version(version, language))
        agreement_cards.append({
            "document": document,
            "version": version,
            "translation": translation,
            "accepted": (
                optional_dashboard_value(
                    "agreement_acceptance",
                    None,
                    lambda version=version: (
                        MemberAgreementAcceptance.query
                        .filter_by(member_id=member.member_id, legal_document_version_id=version.id)
                        .order_by(MemberAgreementAcceptance.accepted_at.desc())
                        .first()
                    ),
                )
                if version else None
            ),
        })
    signed_documents = optional_dashboard_value(
        "signed_documents",
        [],
        lambda: (
            MemberSignedDocument.query
            .filter_by(member_id=member.member_id)
            .order_by(MemberSignedDocument.signed_at.desc(), MemberSignedDocument.uploaded_at.desc())
            .all()
        ),
    )
    policy = cancellation_policy_for_member(member)
    policy_summary, policy_detail = cancellation_message_parts(policy, language=language)
    cancellation_request = optional_dashboard_value(
        "agreement_cancellation_request",
        None,
        lambda: active_cancellation_request_for_member(member),
    )
    return {
        "member": member,
        "display_name": display_member_name(member.name),
        "agreement_cards": agreement_cards,
        "signed_documents": signed_documents,
        "policy": policy,
        "cancellation_summary": policy_summary,
        "cancellation_detail": policy_detail,
        "cancel_window_open": fmt_policy_date(policy.window_open, language),
        "cancel_window_close": fmt_policy_date(policy.last_request_date, language),
        "current_term_end": fmt_policy_date(policy.current_term_end, language),
        "cancellation_request": cancellation_request,
        "cancellation_request_message": cancellation_request_member_message(cancellation_request, language=language),
        "show_cancel": (
            cancellation_portal_available_for_member(member)
            and policy.can_request
            and not cancellation_request
        ),
        "gym_balance": member.balance or 0,
        "is_contract_member": is_contract_member_record(member),
    }


def legal_translation_seed_text(document, language, field):
    return document[field]


def clean_seeded_legal_review_prefix(value):
    if not value:
        return value
    for prefix in [
        "Nederlandse conceptvertaling",
        "Draft tradukshon na Papiamentu",
        "Traduccion borrador en Espanol",
        "Draft translation",
    ]:
        old_prefix = f"{prefix}. {LEGAL_TRANSLATION_DRAFT_NOTICE}\n\n"
        if value.startswith(old_prefix):
            return value[len(old_prefix):]
    return value


def legal_translation_for_version(version, language):
    if not version:
        return None
    language = normalize_language(language)
    translation = LegalTranslation.query.filter_by(version_id=version.id, language=language).first()
    if translation:
        return translation
    return LegalTranslation.query.filter_by(version_id=version.id, language=DEFAULT_LANGUAGE).first()


def seed_required_agreement_rules():
    rule_defaults = [
        {
            "applies_to_contract_term": None,
            "applies_to_payment_method": None,
            "applies_to_add_on": None,
            "required_legal_document_types": [
                "membership_application_form",
                "general_terms",
                "gym_rules",
                "liability_waiver",
                "media_security_consent",
                "personal_training_business_rules",
            ],
        },
        {
            "applies_to_contract_term": "6_months",
            "required_legal_document_types": [
                "membership_contract_6_months",
                "cancellation_renewal_rules",
                "payment_rules",
            ],
        },
        {
            "applies_to_contract_term": "12_months",
            "required_legal_document_types": [
                "membership_contract_12_months",
                "cancellation_renewal_rules",
                "payment_rules",
            ],
        },
        {
            "applies_to_payment_method": "mcb_direct_debit_monthly",
            "required_legal_document_types": [
                "direct_debit_mandate",
                "payment_rules",
            ],
        },
        {
            "applies_to_add_on": "group_pt",
            "required_legal_document_types": ["group_pt_addon_waiver"],
        },
    ]
    for defaults in rule_defaults:
        required_json = json.dumps(defaults["required_legal_document_types"])
        existing = RequiredAgreementRule.query.filter_by(
            applies_to_membership_type=defaults.get("applies_to_membership_type"),
            applies_to_contract_term=defaults.get("applies_to_contract_term"),
            applies_to_payment_method=defaults.get("applies_to_payment_method"),
            applies_to_add_on=defaults.get("applies_to_add_on"),
            active=True,
        ).first()
        if existing:
            continue
        db.session.add(RequiredAgreementRule(
            applies_to_membership_type=defaults.get("applies_to_membership_type"),
            applies_to_contract_term=defaults.get("applies_to_contract_term"),
            applies_to_payment_method=defaults.get("applies_to_payment_method"),
            applies_to_add_on=defaults.get("applies_to_add_on"),
            applies_to_under18=defaults.get("applies_to_under18"),
            required_legal_document_types=required_json,
            active=True,
        ))


def seed_legal_documents():
    review_notice = AppSetting.query.filter_by(key="legal_translation_review_notice").first()
    if not review_notice:
        db.session.add(AppSetting(key="legal_translation_review_notice", value=LEGAL_TRANSLATION_REVIEWED_NOTICE))
    elif review_notice.value == LEGAL_TRANSLATION_DRAFT_NOTICE:
        review_notice.value = LEGAL_TRANSLATION_REVIEWED_NOTICE

    for key, name, description, sort_order in AGREEMENT_CATEGORY_SEED:
        category = AgreementCategory.query.filter_by(key=key).first()
        if not category:
            db.session.add(AgreementCategory(
                key=key,
                name=name,
                description=description,
                sort_order=sort_order,
                is_active=True,
            ))
        else:
            if not category.name:
                category.name = name
            if not category.description:
                category.description = description
            if category.sort_order in (None, 0):
                category.sort_order = sort_order
            category.updated_at = datetime.now()

    db.session.flush()

    for defaults in LEGAL_DOCUMENT_SEED:
        document = LegalDocument.query.filter_by(document_type=defaults["document_type"]).first()
        if not document:
            document = LegalDocument(
                document_type=defaults["document_type"],
                title=defaults["title"],
                category_key=defaults["category_key"],
                active=True,
                required_for=json.dumps(defaults.get("required_for", [])),
                sort_order=defaults["sort_order"],
                internal_notes=defaults.get("internal_notes"),
                legal_review_needed=False,
            )
            db.session.add(document)
        else:
            if not document.title:
                document.title = defaults["title"]
            if not document.category_key:
                document.category_key = defaults["category_key"]
            if not document.required_for:
                document.required_for = json.dumps(defaults.get("required_for", []))
            if document.sort_order in (None, 0):
                document.sort_order = defaults["sort_order"]
            if not document.internal_notes and defaults.get("internal_notes"):
                document.internal_notes = defaults.get("internal_notes")
            elif document.internal_notes and "Legal review needed." in document.internal_notes:
                document.internal_notes = document.internal_notes.replace(" Legal review needed.", "")
            document.legal_review_needed = False
            document.updated_at = datetime.now()
        db.session.flush()

        version_value = "2026-05-27-legal-reviewed"
        legacy_version_value = "2026-05-27-draft"
        version = LegalDocumentVersion.query.filter_by(
            document_id=document.id,
            version=version_value,
        ).first()
        if not version:
            version = LegalDocumentVersion.query.filter_by(
                document_id=document.id,
                version=legacy_version_value,
            ).first()
        if not version:
            version = LegalDocumentVersion(
                document_id=document.id,
                effective_from=date(2026, 5, 27),
                source_language="en",
                full_legal_text=defaults["text"],
                short_summary=defaults["summary"],
                plain_language_summary=defaults["plain"],
                pdf_template_key=defaults["document_type"],
                is_current=True,
                legal_review_status="legal_reviewed",
            )
            db.session.add(version)
        version.version = version_value
        version.legal_review_status = "legal_reviewed"
        version.is_current = True
        LegalDocumentVersion.query.filter(
            LegalDocumentVersion.document_id == document.id,
            LegalDocumentVersion.id != version.id,
            LegalDocumentVersion.version != version_value,
            LegalDocumentVersion.is_current.is_(True),
        ).update({"is_current": False}, synchronize_session=False)
        db.session.flush()

        for language in LANGUAGES:
            translation = LegalTranslation.query.filter_by(version_id=version.id, language=language).first()
            if not translation:
                translation = LegalTranslation(
                    version_id=version.id,
                    language=language,
                    title=legal_translation_seed_text(defaults, language, "title"),
                    short_summary=legal_translation_seed_text(defaults, language, "summary"),
                    plain_language_summary=legal_translation_seed_text(defaults, language, "plain"),
                    full_legal_text=legal_translation_seed_text(defaults, language, "text"),
                )
                db.session.add(translation)
            translation.title = clean_seeded_legal_review_prefix(translation.title)
            translation.short_summary = clean_seeded_legal_review_prefix(translation.short_summary)
            translation.plain_language_summary = clean_seeded_legal_review_prefix(translation.plain_language_summary)
            translation.full_legal_text = clean_seeded_legal_review_prefix(translation.full_legal_text)
            translation.translation_status = "legal_reviewed"

    seed_required_agreement_rules()
    db.session.commit()
    return LegalDocument.query.order_by(LegalDocument.sort_order.asc(), LegalDocument.title.asc()).all()


def seed_pricing_catalog():
    metadata = {
        "pricing_catalog_source": PRICING_CATALOG_SOURCE,
        "pricing_catalog_initial_seed_date": PRICING_CATALOG_INITIAL_SEED_DATE.isoformat(),
        "pricing_catalog_currency": PRICING_CATALOG_CURRENCY,
        "pricing_catalog_global_rule": PRICING_CATALOG_GLOBAL_RULE,
    }
    for key, value in metadata.items():
        if not AppSetting.query.filter_by(key=key).first():
            db.session.add(AppSetting(key=key, value=value))

    for sort_order, (category_key, name) in enumerate(PRICING_CATEGORY_SEED, start=10):
        category = PricingCategory.query.filter_by(key=category_key).first()
        if not category:
            category = PricingCategory(
                key=category_key,
                name=name,
                sort_order=sort_order,
                is_active=True,
            )
            db.session.add(category)
        else:
            if not category.name:
                category.name = name
            if category.sort_order in (None, 0):
                category.sort_order = sort_order
        category.updated_at = datetime.now()

    db.session.flush()

    for sort_order, defaults in enumerate(PRICING_ITEM_SEED, start=10):
        item = PricingItem.query.filter_by(seed_key=defaults["seed_key"]).first()
        if not item:
            item = PricingItem(
                seed_key=defaults["seed_key"],
                name=defaults["name"],
                category_key=defaults["category_key"],
                price_amount=float(defaults["price_amount"]),
                billing_interval=defaults["billing_interval"],
                visibility=pricing_visibility_value(defaults.get("visibility", ["public"])),
                sort_order=sort_order,
                effective_from=PRICING_CATALOG_INITIAL_SEED_DATE,
                source=PRICING_CATALOG_SOURCE,
            )
            db.session.add(item)

        optional_defaults = {
            "description": defaults.get("description"),
            "currency": PRICING_CATALOG_CURRENCY,
            "duration": defaults.get("duration"),
            "terms": pricing_terms_json(defaults.get("terms", [])),
            "requires_front_desk_handling": defaults.get("requires_front_desk_handling", True),
            "online_payment_available": defaults.get("online_payment_available", False),
            "member_eligible": defaults.get("member_eligible", True),
            "contract_only": defaults.get("contract_only", False),
            "notes": defaults.get("notes"),
            "internal_notes": defaults.get("internal_notes"),
        }
        for field, value in optional_defaults.items():
            current = getattr(item, field, None)
            if current in (None, ""):
                setattr(item, field, value)
        item.updated_at = datetime.now()

    db.session.commit()
    return PricingItem.query.order_by(PricingItem.sort_order.asc(), PricingItem.name.asc()).all()


def seed_feature_access_rules():
    for defaults in FEATURE_ACCESS_RULE_SEED:
        rule = FeatureAccessRule.query.filter_by(key=defaults["key"]).first()
        if not rule:
            rule = FeatureAccessRule(
                key=defaults["key"],
                label=defaults["label"],
                access_level=defaults["access_level"],
                description=defaults.get("description"),
                is_active=True,
                is_future_ready=defaults.get("is_future_ready", False),
                metadata_json=pricing_terms_json(defaults.get("metadata", [])),
            )
            db.session.add(rule)
        else:
            if not rule.label:
                rule.label = defaults["label"]
            if not rule.access_level:
                rule.access_level = defaults["access_level"]
            if not rule.description:
                rule.description = defaults.get("description")
            rule.updated_at = datetime.now()
    db.session.commit()
    return FeatureAccessRule.query.order_by(FeatureAccessRule.access_level.asc(), FeatureAccessRule.key.asc()).all()


def is_contract_member_record(member):
    contract_text = " ".join([member.contract_type or "", member.plan_type or ""]).lower()
    return "6-month" in contract_text or "12-month" in contract_text or "contract" in contract_text and "no-contract" not in contract_text


def member_access_profile(member):
    levels = {"member"}
    if is_contract_member_record(member):
        levels.add("contract_member")
    else:
        levels.add("no_contract_member")
    return sorted(levels)


def pricing_item_access_tags(item):
    tags = set()
    visibility = set(pricing_visibility_list(item))
    if "public" in visibility or "public_business" in visibility:
        tags.add("public_basic")
    if "members" in visibility and item.member_eligible:
        tags.add("member")
    if item.contract_only:
        tags.add("contract_member")
    if "staff_only" in visibility:
        tags.add("staff")
    if item.category_key == "external_trainer_b2b":
        tags.discard("member")
        tags.discard("contract_member")
    return sorted(tags)


def group_class_day_label(day_of_week, language=None):
    day_key = GROUP_CLASS_DAY_KEYS.get(day_of_week, "monday")
    return translated_text(f"group_class_day_{day_key}", language or current_language())


def group_class_time_label(value):
    return value.strftime("%H:%M") if value else ""


def parse_group_class_time(value):
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        abort(400, translated_text("group_class_invalid_time", current_language()))


def group_class_occurrence_snapshot(occurrence):
    return {
        "class_type_id": occurrence.class_type_id,
        "class_name": occurrence.class_type.name if occurrence.class_type else "",
        "day_of_week": occurrence.day_of_week,
        "start_time": group_class_time_label(occurrence.start_time),
        "end_time": group_class_time_label(occurrence.end_time),
        "room": occurrence.room,
        "instructor": occurrence.instructor or "",
        "capacity": occurrence.capacity,
        "status": occurrence.status,
        "is_bookable": bool(occurrence.is_bookable),
        "is_published": bool(occurrence.is_published),
    }


def group_class_change_type(old_data, new_data):
    if old_data.get("status") != "cancelled" and new_data.get("status") == "cancelled":
        return "class_cancelled"
    if old_data.get("class_type_id") != new_data.get("class_type_id"):
        return "class_type_changed"
    if (
        old_data.get("day_of_week") != new_data.get("day_of_week")
        or old_data.get("start_time") != new_data.get("start_time")
        or old_data.get("end_time") != new_data.get("end_time")
    ):
        return "class_time_changed"
    if old_data.get("room") != new_data.get("room"):
        return "room_changed"
    if old_data.get("instructor") != new_data.get("instructor"):
        return "instructor_changed"
    if old_data != new_data:
        return "schedule_updated"
    return None


def group_class_change_message(occurrence, change_type, old_data, new_data):
    class_name = new_data.get("class_name") or old_data.get("class_name") or "Class"
    day_name = group_class_day_label(new_data.get("day_of_week", occurrence.day_of_week), DEFAULT_LANGUAGE)
    start_time = new_data.get("start_time") or group_class_time_label(occurrence.start_time)
    if change_type == "class_cancelled":
        return f"{class_name} on {day_name} at {start_time} has been cancelled."
    if change_type == "class_time_changed":
        return f"{class_name} on {day_name} has moved to {start_time}."
    if change_type == "room_changed":
        return f"{class_name} on {day_name} at {start_time} has moved to {new_data.get('room')}."
    if change_type == "instructor_changed":
        return f"{class_name} on {day_name} at {start_time} has a new instructor."
    if change_type == "class_type_changed":
        return f"The class on {day_name} at {start_time} changed from {old_data.get('class_name')} to {class_name}."
    return f"{class_name} on {day_name} at {start_time} has been updated."


def future_member_class_plans_for_occurrence(occurrence_id, today=None):
    today = today or local_datetime(datetime.now(timezone.utc)).date()
    return (
        MemberClassPlan.query
        .filter_by(occurrence_id=occurrence_id)
        .filter(MemberClassPlan.class_date >= today)
        .filter(MemberClassPlan.status.in_(["planned", "adjusted"]))
        .all()
    )


def create_group_class_change_notifications(occurrence, change_type, old_data, new_data):
    if not change_type:
        return 0
    affected_plans = future_member_class_plans_for_occurrence(occurrence.id)
    message = group_class_change_message(occurrence, change_type, old_data, new_data)
    old_value = json.dumps(old_data, sort_keys=True)
    new_value = json.dumps(new_data, sort_keys=True)

    if not affected_plans:
        db.session.add(ScheduleChangeNotification(
            occurrence_id=occurrence.id,
            change_type=change_type,
            message_key=f"group_class_notification_{change_type}",
            message=message,
            old_value=old_value,
            new_value=new_value,
            published_at=datetime.now(),
        ))
        return 0

    for plan in affected_plans:
        db.session.add(ScheduleChangeNotification(
            member_id=plan.member_id,
            occurrence_id=occurrence.id,
            plan_id=plan.id,
            change_type=change_type,
            message_key=f"group_class_notification_{change_type}",
            message=message,
            old_value=old_value,
            new_value=new_value,
            published_at=datetime.now(),
        ))
        if change_type in {"class_cancelled", "class_time_changed", "class_type_changed"}:
            plan.status = "adjusted"
            plan.note = message
            plan.updated_at = datetime.now()
    return len(affected_plans)


def group_class_admin_context():
    selected_day = request.args.get("day", "").strip()
    selected_room = request.args.get("room", "").strip()
    selected_type = request.args.get("class_type", "").strip()
    ensure_runtime_schema()
    schedule = GroupClassSchedule.query.filter_by(name=GROUP_CLASS_SCHEDULE_NAME).first()
    if not schedule:
        schedule = seed_group_class_schedule()
    class_types = GroupClassType.query.order_by(GroupClassType.name.asc()).all()
    occurrences_query = (
        GroupClassOccurrence.query
        .join(GroupClassType)
        .order_by(GroupClassOccurrence.day_of_week.asc(), GroupClassOccurrence.start_time.asc(), GroupClassType.name.asc())
    )
    if selected_day != "":
        try:
            occurrences_query = occurrences_query.filter(GroupClassOccurrence.day_of_week == int(selected_day))
        except ValueError:
            selected_day = ""
    if selected_room:
        occurrences_query = occurrences_query.filter(GroupClassOccurrence.room == selected_room)
    if selected_type:
        try:
            occurrences_query = occurrences_query.filter(GroupClassOccurrence.class_type_id == int(selected_type))
        except ValueError:
            selected_type = ""

    occurrences = occurrences_query.all()
    rooms = [
        room for (room,) in
        db.session.query(GroupClassOccurrence.room).distinct().order_by(GroupClassOccurrence.room.asc()).all()
        if room
    ]
    today = local_datetime(datetime.now(timezone.utc)).date()
    future_plan_counts = {
        occurrence_id: count
        for occurrence_id, count in (
            db.session.query(MemberClassPlan.occurrence_id, db.func.count(MemberClassPlan.id))
            .filter(MemberClassPlan.class_date >= today)
            .filter(MemberClassPlan.status.in_(["planned", "adjusted"]))
            .group_by(MemberClassPlan.occurrence_id)
            .all()
        )
    }
    changes = (
        ScheduleChangeNotification.query
        .order_by(ScheduleChangeNotification.created_at.desc(), ScheduleChangeNotification.id.desc())
        .limit(25)
        .all()
    )
    return {
        "schedule": schedule,
        "class_types": class_types,
        "occurrences": occurrences,
        "rooms": rooms,
        "selected_day": selected_day,
        "selected_room": selected_room,
        "selected_type": selected_type,
        "day_options": [(day, group_class_day_label(day)) for day in range(7)],
        "statuses": sorted(GROUP_CLASS_OCCURRENCE_STATUSES),
        "future_plan_counts": future_plan_counts,
        "changes": changes,
        "time_label": group_class_time_label,
        "day_label": group_class_day_label,
    }


def empty_group_class_admin_context():
    selected_day = request.args.get("day", "").strip()
    selected_room = request.args.get("room", "").strip()
    selected_type = request.args.get("class_type", "").strip()
    return {
        "schedule": None,
        "class_types": [],
        "occurrences": [],
        "rooms": [],
        "selected_day": selected_day,
        "selected_room": selected_room,
        "selected_type": selected_type,
        "day_options": [(day, group_class_day_label(day)) for day in range(7)],
        "statuses": sorted(GROUP_CLASS_OCCURRENCE_STATUSES),
        "future_plan_counts": {},
        "changes": [],
        "time_label": group_class_time_label,
        "day_label": group_class_day_label,
    }


def next_date_for_group_class(day_of_week, today=None):
    today = today or local_datetime(datetime.now(timezone.utc)).date()
    days_ahead = (day_of_week - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def member_group_class_preference(member_id):
    return MemberClassPreference.query.filter_by(member_id=member_id, class_type_id=None).first()


def member_favorite_class_type_ids(member_id):
    return {
        class_type_id for (class_type_id,) in
        db.session.query(MemberClassPreference.class_type_id)
        .filter_by(member_id=member_id, is_favorite=True)
        .filter(MemberClassPreference.class_type_id.isnot(None))
        .all()
    }


def published_member_group_class_query():
    return (
        GroupClassOccurrence.query
        .join(GroupClassType)
        .filter(GroupClassOccurrence.status == "scheduled")
        .filter(GroupClassOccurrence.is_published.is_(True))
        .filter(GroupClassOccurrence.is_bookable.is_(True))
        .order_by(GroupClassOccurrence.day_of_week.asc(), GroupClassOccurrence.start_time.asc(), GroupClassType.name.asc())
    )


def member_group_class_plan_map(member_id, start_date, end_date):
    plans = (
        MemberClassPlan.query
        .filter_by(member_id=member_id)
        .filter(MemberClassPlan.class_date >= start_date)
        .filter(MemberClassPlan.class_date <= end_date)
        .all()
    )
    return {(plan.occurrence_id, plan.class_date): plan for plan in plans}


def member_group_class_schedule_rows(member_id, selected_day="", selected_type="", today=None):
    today = today or local_datetime(datetime.now(timezone.utc)).date()
    query = published_member_group_class_query()
    if selected_day != "":
        try:
            query = query.filter(GroupClassOccurrence.day_of_week == int(selected_day))
        except ValueError:
            selected_day = ""
    if selected_type:
        try:
            query = query.filter(GroupClassOccurrence.class_type_id == int(selected_type))
        except ValueError:
            selected_type = ""
    occurrences = query.all()
    plan_map = member_group_class_plan_map(member_id, today, today + timedelta(days=6))
    favorite_ids = member_favorite_class_type_ids(member_id)
    rows = []
    for occurrence in occurrences:
        class_date = next_date_for_group_class(occurrence.day_of_week, today=today)
        plan = plan_map.get((occurrence.id, class_date))
        rows.append({
            "occurrence": occurrence,
            "class_date": class_date,
            "plan": plan,
            "is_favorite": occurrence.class_type_id in favorite_ids,
        })
    return rows


def group_class_time_status(occurrence, current_time):
    if occurrence.status == "cancelled":
        return "cancelled"
    if occurrence.end_time < current_time:
        return "past"
    if occurrence.start_time <= current_time <= occurrence.end_time:
        return "live"
    return "upcoming"


def member_today_group_class_sections(member_id, now=None, limit=3, earlier_limit=2):
    now = now or current_portal_datetime()
    today = now.date()
    current_time = now.time()
    rows = [
        row for row in member_group_class_schedule_rows(member_id, selected_day=str(today.weekday()), today=today)
        if row["class_date"] == today
    ]
    current_rows = []
    earlier_rows = []
    for row in rows:
        status = group_class_time_status(row["occurrence"], current_time)
        row["time_status"] = status
        if status in {"past", "cancelled"}:
            earlier_rows.append(row)
        else:
            current_rows.append(row)
    current_rows.sort(key=lambda row: (0 if row["time_status"] == "live" else 1, row["occurrence"].start_time))
    earlier_rows.sort(key=lambda row: row["occurrence"].start_time, reverse=True)
    return {
        "current": current_rows[:limit],
        "earlier": earlier_rows[:earlier_limit],
    }


def member_today_group_classes(member_id, limit=3):
    return member_today_group_class_sections(member_id, limit=limit)["current"]


def member_planned_group_classes(member_id, start_date=None, end_date=None):
    start_date = start_date or local_datetime(datetime.now(timezone.utc)).date()
    end_date = end_date or (start_date + timedelta(days=6))
    return (
        MemberClassPlan.query
        .filter_by(member_id=member_id)
        .filter(MemberClassPlan.class_date >= start_date)
        .filter(MemberClassPlan.class_date <= end_date)
        .order_by(MemberClassPlan.class_date.asc(), MemberClassPlan.id.asc())
        .all()
    )


def member_group_class_attendance_count(member_id, start_date=None, end_date=None):
    query = MemberClassAttendance.query.filter_by(member_id=member_id)
    if start_date:
        query = query.filter(MemberClassAttendance.class_date >= start_date)
    if end_date:
        query = query.filter(MemberClassAttendance.class_date <= end_date)
    return query.count()


def group_class_load_text(occurrence, profile=None):
    class_type = occurrence.class_type
    parts = [
        class_type.name,
        f"intensity={class_type.intensity}",
        f"muscle_focus={class_type.muscle_focus or 'unknown'}",
        f"cardio_load={class_type.cardio_load or 'unknown'}",
        f"strength_load={class_type.strength_load or 'unknown'}",
        f"recovery_impact={class_type.recovery_impact or 'unknown'}",
        f"impact_level={class_type.impact_level or 'unknown'}",
    ]
    if is_pregnant_profile(profile):
        parts.append(f"pregnancy_safety_level={class_type.pregnancy_safety_level or 'unknown'}")
    return ", ".join(parts)


def group_class_training_load_context(member, profile=None):
    if not member:
        return "group_classes=unavailable"
    today = local_datetime(datetime.now(timezone.utc)).date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    planned = member_planned_group_classes(member.member_id, today, today + timedelta(days=6))
    attended = (
        MemberClassAttendance.query
        .filter_by(member_id=member.member_id)
        .filter(MemberClassAttendance.class_date >= week_start)
        .filter(MemberClassAttendance.class_date <= week_end)
        .order_by(MemberClassAttendance.class_date.asc(), MemberClassAttendance.id.asc())
        .all()
    )
    preference = member_group_class_preference(member.member_id)
    favorite_names = [
        class_type.name for class_type in GroupClassType.query
        .join(MemberClassPreference)
        .filter(MemberClassPreference.member_id == member.member_id)
        .filter(MemberClassPreference.is_favorite.is_(True))
        .filter(MemberClassPreference.class_type_id.isnot(None))
        .order_by(GroupClassType.name.asc())
        .all()
    ]
    changes = (
        ScheduleChangeNotification.query
        .filter_by(member_id=member.member_id)
        .order_by(ScheduleChangeNotification.created_at.desc(), ScheduleChangeNotification.id.desc())
        .limit(5)
        .all()
    )

    planned_text = "; ".join(
        f"{plan.class_date.isoformat()} {group_class_load_text(plan.occurrence, profile)} room={plan.occurrence.room} status={plan.status} replaces_personal_workout={plan.replaces_personal_workout}"
        for plan in planned
    ) or "none"
    attended_text = "; ".join(
        f"{entry.class_date.isoformat()} {group_class_load_text(entry.occurrence, profile)} room={entry.occurrence.room}"
        for entry in attended
    ) or "none"
    high_recovery_count = sum(
        1 for plan in planned
        if plan.occurrence.class_type.recovery_impact in {"medium_high", "high"}
    )
    strength_count = sum(
        1 for plan in planned
        if plan.occurrence.class_type.strength_load in {"medium_high", "high"}
    )
    cardio_count = sum(
        1 for plan in planned
        if plan.occurrence.class_type.cardio_load in {"medium", "medium_high", "high"}
    )
    changes_text = "; ".join(change.message for change in changes) or "none"
    return (
        "group_class_preferences="
        f"preferred_per_week={preference.preferred_classes_per_week if preference else 'unknown'}, "
        f"mode={preference.plan_mode if preference else 'supplement'}, favorites={','.join(favorite_names) or 'none'}; "
        f"planned_group_classes_this_week={planned_text}; "
        f"attended_group_classes_this_week={attended_text}; "
        "weekly_group_class_training_load="
        f"planned_count={len(planned)}, attended_count={len(attended)}, "
        f"cardio_classes={cardio_count}, strength_classes={strength_count}, high_recovery_impact_classes={high_recovery_count}; "
        f"schedule_changes={changes_text}"
    )


def coach_today_group_class_rows(member, now=None):
    if not member:
        return []
    now = now or current_portal_datetime()
    today = now.date()
    current_time = now.time()
    rows = [
        row for row in member_group_class_schedule_rows(member.member_id, selected_day=str(today.weekday()), today=today)
        if row["class_date"] == today
    ]
    for row in rows:
        row["time_status"] = group_class_time_status(row["occurrence"], current_time)
    rows.sort(key=lambda row: row["occurrence"].start_time)
    return rows


def coach_today_group_class_context(member, now=None):
    now = now or current_portal_datetime()
    rows = coach_today_group_class_rows(member, now=now)
    if not rows:
        return (
            f"today_group_class_schedule(date={now.date().isoformat()}, timezone=America/Kralendijk, "
            "schedule_link=/group-classes)=none"
        )
    schedule_text = "; ".join(
        f"{row['occurrence'].start_time.strftime('%H:%M')}-{row['occurrence'].end_time.strftime('%H:%M')} "
        f"{row['occurrence'].class_type.name} room={row['occurrence'].room} "
        f"status={'ended' if row['time_status'] == 'past' else row['time_status']}"
        for row in rows
    )
    return (
        f"today_group_class_schedule(date={now.date().isoformat()}, timezone=America/Kralendijk, "
        f"schedule_link=/group-classes)={schedule_text}"
    )


@app.before_request
def prepare_runtime_schema():
    try:
        ensure_runtime_schema()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema preparation failed; continuing with existing schema.")
        if app.config.get("TESTING") or os.getenv("RUNTIME_SCHEMA_STRICT", "").lower() in ("1", "true", "yes"):
            raise


def staff_data_warning(section_key="staff"):
    return translated_text("staff_data_source_warning", current_language(), section=translated_text(section_key, current_language()))


def try_staff_runtime_schema(section_key="staff"):
    try:
        ensure_runtime_schema()
        return None
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while rendering staff section: %s", section_key)
        return staff_data_warning(section_key)


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


SYNC_INVALID_SENTINELS = {
    "<INVALID>",
    "INVALID",
    "<ERROR>",
    "#VALUE!",
}


SYNC_BLANK_PROTECTED_FIELDS = {
    "name",
    "email",
    "phone",
    "mobile",
    "birthdate",
    "plan_type",
    "contract_type",
    "billing_status",
    "billing_option",
    "billing_type",
    "last_payment",
    "next_payment",
    "due_date",
    "start_date",
    "end_date",
    "signup_date",
    "photo_path",
}


def is_sync_invalid_sentinel(value):
    return isinstance(value, str) and value.strip().upper() in SYNC_INVALID_SENTINELS


def is_blank_sync_value(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def has_meaningful_sync_value(value):
    return value not in (None, "")


def sync_invalid_field_names(raw_member):
    columns = {column.name for column in Member.__table__.columns}
    return [
        key
        for key, value in (raw_member or {}).items()
        if key in columns and key != "id" and is_sync_invalid_sentinel(value)
    ]


def sync_blank_field_names(existing, member_data):
    return [
        field
        for field in SYNC_BLANK_PROTECTED_FIELDS
        if field in member_data
        and is_blank_sync_value(member_data.get(field))
        and has_meaningful_sync_value(change_value(getattr(existing, field, None)))
    ]


def remove_blank_sync_fields(existing, member_data):
    ignored_fields = sync_blank_field_names(existing, member_data)
    for field in ignored_fields:
        member_data.pop(field, None)
    return ignored_fields


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
        if is_sync_invalid_sentinel(value):
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
    "birthdate",
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
    "birthdate": "Birthdate",
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


def visible_sync_change(change):
    change = change or {}
    if is_sync_invalid_sentinel(change.get("new")):
        return False
    if (
        change.get("field") in SYNC_BLANK_PROTECTED_FIELDS
        and is_blank_sync_value(change.get("new"))
        and has_meaningful_sync_value(change.get("old"))
    ):
        return False
    return True


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
            if is_sync_invalid_sentinel(item.get("plan_type")):
                item = {**item, "plan_type": ""}
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
                if not visible_sync_change(change):
                    continue
                entry["changes"].append({**change, "run_time": run_time})
            if not entry["changes"]:
                changed_members.pop(member_id, None)

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
    ignored_invalid_fields = {}
    ignored_blank_fields = {}
    try:
        for raw_member in members:
            invalid_fields = sync_invalid_field_names(raw_member)
            member_data = normalize_sync_member_data(raw_member)
            if invalid_fields:
                ignored_invalid_fields[member_data["member_id"]] = invalid_fields
            existing = Member.query.filter_by(member_id=member_data["member_id"]).first()
            if existing:
                blank_fields = remove_blank_sync_fields(existing, member_data)
                if blank_fields:
                    ignored_blank_fields[member_data["member_id"]] = blank_fields
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
        if ignored_invalid_fields:
            ignored_count = sum(len(fields) for fields in ignored_invalid_fields.values())
            warning_text = (
                f"Ignored {ignored_count} invalid GymAssistant sentinel value(s) "
                f"for {len(ignored_invalid_fields)} member(s). Create a fresh GymAssistant backup if this keeps happening."
            )
            sync_run.error = f"{sync_run.error}\n{warning_text}" if sync_run.error else warning_text
        if ignored_blank_fields:
            ignored_count = sum(len(fields) for fields in ignored_blank_fields.values())
            warning_text = (
                f"Ignored {ignored_count} blank GymAssistant value(s) over existing member data "
                f"for {len(ignored_blank_fields)} member(s). Create a fresh GymAssistant backup if this keeps happening."
            )
            sync_run.error = f"{sync_run.error}\n{warning_text}" if sync_run.error else warning_text
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
        cancellation_window_open_date=policy.window_open,
        cancellation_window_close_date=policy.last_request_date,
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


def generate_cancellation_confirmation_number(request_record):
    today_part = datetime.now().strftime("%Y%m%d")
    member_part = re.sub(r"\W+", "", request_record.member_id or "")[-6:] or "member"
    return f"CAN-{today_part}-{member_part}-{request_record.id:05d}"


def create_cancellation_confirmation_record(request_record, language=None):
    if not request_record.confirmation_number:
        request_record.confirmation_number = generate_cancellation_confirmation_number(request_record)
    if not request_record.pdf_receipt_url:
        request_record.pdf_receipt_url = f"/cancellations/{request_record.id}/confirmation.pdf"
    existing = CancellationConfirmation.query.filter_by(cancellation_request_id=request_record.id).first()
    if existing:
        return existing
    pdf_hash = hashlib.sha256(
        f"{request_record.id}|{request_record.member_id}|{request_record.confirmation_number}".encode("utf-8")
    ).hexdigest()
    confirmation = CancellationConfirmation(
        cancellation_request_id=request_record.id,
        confirmation_number=request_record.confirmation_number,
        pdf_receipt_url=request_record.pdf_receipt_url,
        pdf_hash=pdf_hash,
        language=normalize_language(language or request_record.language or DEFAULT_LANGUAGE),
        status="generated",
    )
    db.session.add(confirmation)
    return confirmation


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
    try:
        return CancellationRequest.query.filter(
            CancellationRequest.status == "accepted",
            CancellationRequest.admin_status.in_(["new", "reviewed"]),
        ).count()
    except Exception:
        db.session.rollback()
        app.logger.exception("Open cancellation count unavailable; hiding staff badge.")
        return 0


def open_email_log_count():
    try:
        return EmailLog.query.filter(email_log_review_required_filter()).count()
    except Exception:
        db.session.rollback()
        app.logger.exception("Open email log count unavailable; hiding staff badge.")
        return 0


def member_account_notification_count(member_id):
    if not member_id:
        return 0
    try:
        balance = db.session.execute(
            db.select(Member.balance).where(Member.member_id == member_id).limit(1)
        ).scalar()
    except SQLAlchemyError:
        db.session.rollback()
        return 0
    count = 0
    if (balance or 0) > 0:
        count += 1
    return count


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


def start_member_session(member, password_verified=False):
    csrf_token = session.get("_csrf_token")
    language = session.get("language")
    session.clear()
    if csrf_token:
        session["_csrf_token"] = csrf_token
    if language:
        session["language"] = language
    session["member_id"] = member.member_id
    if password_verified:
        session["member_password_verified"] = True
    session.permanent = True


def validate_member_password(password, confirmation):
    if password != confirmation:
        return "member_password_mismatch"
    if len(password or "") < app.config["MEMBER_PASSWORD_MIN_LENGTH"]:
        return "member_password_too_short"
    return None


COACH_GOALS = ["lose_weight", "build_muscle", "get_fitter", "strength", "health"]
COACH_EXPERIENCE_LEVELS = ["beginner", "intermediate", "advanced"]
COACH_TRAINING_DAYS = [2, 3, 4, 5, 6]
COACH_SESSION_MINUTES = [30, 45, 60, 75, 90]
COACH_TRAINING_PLACES = ["dreamz_gym", "home", "both"]
COACH_NUTRITION_GOALS = ["fat_loss", "muscle_gain", "maintenance", "healthier"]
COACH_SEX_VALUES = ["male", "female"]
PREGNANCY_STATUSES = ["not_pregnant", "pregnant"]
PREGNANCY_MULTIPLE_VALUES = ["no", "yes", "unknown"]
PREGNANCY_PROVIDER_CLEARANCE_VALUES = ["yes", "no", "unknown"]
PREGNANCY_WARNING_SYMPTOMS = [
    "dizziness",
    "vaginal_bleeding",
    "chest_pain",
    "severe_shortness_of_breath",
    "severe_headache",
    "painful_contractions",
    "fluid_leakage",
    "pelvic_pain",
    "other",
]
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


def parse_optional_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_optional_datetime(value):
    value = str(value or "").strip()
    if not value:
        return None
    for date_format in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    return None


def pregnancy_symptom_list(profile):
    if not profile or not profile.pregnancy_symptoms:
        return []
    try:
        symptoms = json.loads(profile.pregnancy_symptoms)
    except (TypeError, ValueError):
        symptoms = [part.strip() for part in str(profile.pregnancy_symptoms).split(",")]
    return [symptom for symptom in symptoms if symptom in PREGNANCY_WARNING_SYMPTOMS]


def set_pregnancy_symptoms(profile, symptoms):
    selected = [symptom for symptom in symptoms if symptom in PREGNANCY_WARNING_SYMPTOMS]
    profile.pregnancy_symptoms = json.dumps(selected) if selected else None


def is_pregnant_profile(profile):
    return bool(profile and profile.sex == "female" and profile.pregnancy_status == "pregnant")


def pregnancy_trimester(profile):
    weeks = profile.gestational_weeks if profile else None
    if not weeks:
        return None
    if weeks <= 13:
        return 1
    if weeks <= 27:
        return 2
    return 3


def pregnancy_safety_status(profile):
    if not is_pregnant_profile(profile):
        return "not_applicable"
    if pregnancy_symptom_list(profile):
        return "warning_symptoms"
    if profile.provider_cleared_exercise == "no":
        return "not_cleared"
    if profile.provider_cleared_exercise == "unknown":
        return "clearance_unknown"
    return "active"


def member_age_years(member, today=None):
    birthdate = getattr(member, "birthdate", None)
    if not birthdate:
        return None
    today = today or date.today()
    return today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))


def nutrition_target_numbers(member, profile):
    age = member_age_years(member)
    weight = profile.weight_kg if profile else None
    height = profile.height_cm if profile else None
    sex = profile.sex if profile else None
    training_days = profile.training_days if profile else None
    goal = profile.nutrition_goal if profile else None
    missing = []
    for key, value in [
        ("coach_sex_label", sex),
        ("coach_current_weight_kg", weight),
        ("coach_height_cm", height),
        ("date_of_birth", age),
    ]:
        if value in (None, ""):
            missing.append(key)

    calories = None
    if age and weight and height and sex in {"male", "female"}:
        base = (10 * weight) + (6.25 * height) - (5 * age) + (5 if sex == "male" else -161)
        activity = 1.35 + min(max(training_days or 0, 0), 6) * 0.04
        calories = round((base * activity) / 50) * 50
        if goal == "muscle_gain":
            calories += 200
        elif goal == "fat_loss" and not is_pregnant_profile(profile):
            calories -= 300
        elif is_pregnant_profile(profile):
            calories += 250 if (profile.gestational_weeks or 0) >= 14 else 0

    protein = round((weight or 75) * (1.8 if goal == "muscle_gain" else 1.6))
    fat = round((weight or 75) * 0.8)
    carbs = round((calories - (protein * 4) - (fat * 9)) / 4) if calories else None
    if carbs is not None and carbs < 120:
        carbs = 120
    return {
        "calories": calories,
        "protein": protein,
        "carbs": carbs,
        "fat": fat,
        "hydration_liters": 3.0 if (training_days or 0) >= 4 else 2.5,
        "missing": missing,
    }


def nutrition_missing_profile_items(member, profile, language=None):
    language = language or current_language()
    items = []

    def add(field, label_key, action_key=None):
        items.append({
            "field": field,
            "label": translated_text(label_key, language),
            "action": translated_text(action_key or label_key, language),
        })

    if not getattr(member, "birthdate", None):
        add("birthdate", "date_of_birth", "add_date_of_birth")
    if not profile:
        add("sex", "coach_sex_label", "add_missing_profile_data")
        add("height_cm", "coach_height_cm", "add_height")
        add("weight_kg", "coach_current_weight_kg", "add_current_weight")
        add("nutrition_goal", "coach_nutrition_goal", "add_nutrition_goal")
        add("dietary_preferences", "coach_dietary_preferences", "add_food_preferences")
        add("allergies", "coach_allergies", "add_allergies")
        return items

    if profile.sex in (None, ""):
        add("sex", "coach_sex_label", "add_missing_profile_data")
    if profile.height_cm in (None, ""):
        add("height_cm", "coach_height_cm", "add_height")
    if profile.weight_kg in (None, ""):
        add("weight_kg", "coach_current_weight_kg", "add_current_weight")
    if profile.nutrition_goal in (None, ""):
        add("nutrition_goal", "coach_nutrition_goal", "add_nutrition_goal")
    if profile.dietary_preferences in (None, ""):
        add("dietary_preferences", "coach_dietary_preferences", "add_food_preferences")
    if profile.allergies in (None, ""):
        add("allergies", "coach_allergies", "add_allergies")
    if profile.sex == "female" and profile.pregnancy_status in (None, ""):
        add("pregnancy_status", "coach_pregnancy_status_label", "add_pregnancy_details")
    if is_pregnant_profile(profile):
        for field, label_key in [
            ("gestational_weeks", "coach_gestational_weeks"),
            ("multiple_pregnancy", "coach_multiple_pregnancy"),
            ("provider_cleared_exercise", "coach_provider_cleared_exercise"),
            ("pregnancy_consent", "coach_pregnancy_consent"),
        ]:
            if getattr(profile, field, None) in (None, "", False):
                add(field, label_key, "add_pregnancy_details")
        if profile.provider_cleared_exercise == "unknown":
            add("provider_cleared_exercise", "coach_provider_cleared_exercise", "update_pregnancy_clearance")
    return items


def macro_split(total, shares):
    if not total:
        return [None for _ in shares]
    return [round(total * share) for share in shares]


def personalized_nutrition_meal_plan(member, profile, language=None):
    language = language or current_language()
    targets = nutrition_target_numbers(member, profile)
    missing_items = nutrition_missing_profile_items(member, profile, language)
    calorie_parts = macro_split(targets["calories"], [0.24, 0.31, 0.33, 0.12])
    protein_parts = macro_split(targets["protein"], [0.25, 0.30, 0.30, 0.15])
    carb_parts = macro_split(targets["carbs"], [0.25, 0.35, 0.30, 0.10])
    fat_parts = macro_split(targets["fat"], [0.25, 0.25, 0.35, 0.15])
    goal = profile.nutrition_goal or "healthier"
    is_pregnant = is_pregnant_profile(profile)
    meals = [
        {
            "title": translated_text("meal_breakfast", language),
            "name": translated_text("meal_breakfast_name", language),
            "foods": translated_text("meal_breakfast_foods", language),
            "portions": translated_text("meal_breakfast_portions", language),
        },
        {
            "title": translated_text("meal_lunch", language),
            "name": translated_text("meal_lunch_name", language),
            "foods": translated_text("meal_lunch_foods", language),
            "portions": translated_text("meal_lunch_portions", language),
        },
        {
            "title": translated_text("meal_dinner", language),
            "name": translated_text("meal_dinner_name", language),
            "foods": translated_text("meal_dinner_foods", language),
            "portions": translated_text("meal_dinner_portions", language),
        },
        {
            "title": translated_text("meal_snacks", language),
            "name": translated_text("meal_snacks_name", language),
            "foods": translated_text("meal_snacks_foods", language),
            "portions": translated_text("meal_snacks_portions", language),
        },
    ]
    for index, meal in enumerate(meals):
        meal.update({
            "calories": calorie_parts[index],
            "protein": protein_parts[index],
            "carbs": carb_parts[index],
            "fat": fat_parts[index],
        })
    return {
        "summary": translated_text(
            "meal_plan_personalized_summary",
            language,
            goal=coach_label("nutrition", goal, language).lower(),
            days=profile.training_days or translated_text("not_available", language),
        ),
        "missing_data": [item["label"] for item in missing_items],
        "missing_items": missing_items,
        "targets": targets,
        "meals": meals,
        "alternatives": [
            {
                "title": translated_text("meal_plan_budget_alternative", language),
                "body": translated_text("meal_plan_budget_body", language),
            },
            {
                "title": translated_text("meal_plan_vegetarian_alternative", language),
                "body": translated_text("meal_plan_vegetarian_body", language),
            },
            {
                "title": translated_text("meal_plan_quick_alternative", language),
                "body": translated_text("meal_plan_quick_body", language),
            },
        ],
        "pregnancy_note": (
            translated_text("meal_plan_pregnancy_note", language)
            if is_pregnant else ""
        ),
        "profile_note": translated_text(
            "meal_plan_profile_note",
            language,
            preference=profile.dietary_preferences or translated_text("not_available", language),
            allergies=profile.allergies or translated_text("not_available", language),
        ),
    }


def pregnancy_context_summary(profile):
    if not is_pregnant_profile(profile):
        if not profile:
            return "biological_sex=unknown"
        if profile.sex == "female":
            return f"biological_sex=female, pregnancy_status={profile.pregnancy_status or 'unknown'}"
        return f"biological_sex={profile.sex or 'unknown'}"
    symptoms = pregnancy_symptom_list(profile)
    trimester = pregnancy_trimester(profile)
    return (
        "biological_sex=female, pregnancy_status=pregnant, "
        f"gestational_weeks={profile.gestational_weeks}, trimester={trimester or 'unknown'}, "
        f"expected_due_date={profile.expected_due_date or 'unknown'}, "
        f"pre_pregnancy_weight={profile.pre_pregnancy_weight_kg or 'unknown'}, current_weight={profile.weight_kg or 'unknown'}, "
        f"multiple_pregnancy={profile.multiple_pregnancy or 'unknown'}, provider_cleared_exercise={profile.provider_cleared_exercise or 'unknown'}, "
        f"provider_restrictions={profile.provider_restrictions or 'none'}, warning_symptoms={', '.join(symptoms) if symptoms else 'none'}, "
        f"consent_to_use_pregnancy_info={bool(profile.pregnancy_consent)}"
    )


def coach_label(key, value, language=None):
    if not value:
        return translated_text("not_available", language or current_language())
    return translated_text(f"coach_{key}_{value}", language or current_language())


def translated_coach_focus_label(value, language=None):
    if not value:
        return ""
    target = str(value).strip().lower()
    if not target:
        return ""
    lang = language or current_language()
    for translations in TRANSLATIONS.values():
        for key, label in translations.items():
            if not key.startswith("coach_focus_"):
                continue
            if str(label).strip().lower() == target:
                return translated_text(key, lang)
    return value


def coach_profile_completion(profile, member=None):
    if not profile:
        return 0
    fields = [
        getattr(member, "birthdate", None) if member is not None else "not applicable",
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
        profile.sex,
    ]
    if is_pregnant_profile(profile):
        fields.extend([
            profile.gestational_weeks,
            profile.multiple_pregnancy,
            profile.provider_cleared_exercise if profile.provider_cleared_exercise != "unknown" else None,
            profile.pregnancy_consent,
        ])
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
        "sex",
    ]
    if profile_data.get("training_place") in ("home", "both"):
        required_fields.append("home_equipment")
    if profile_data.get("sex") == "female":
        required_fields.append("pregnancy_status")
    if profile_data.get("sex") == "female" and profile_data.get("pregnancy_status") == "pregnant":
        required_fields.extend(["gestational_weeks", "multiple_pregnancy", "provider_cleared_exercise", "pregnancy_consent"])
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


def coach_pregnancy_safety_plan(profile, language=None):
    language = language or current_language()
    status = pregnancy_safety_status(profile)
    if status == "not_applicable":
        return None

    title_key = {
        "warning_symptoms": "coach_pregnancy_safety_warning_title",
        "not_cleared": "coach_pregnancy_safety_not_cleared_title",
        "clearance_unknown": "coach_pregnancy_safety_unknown_title",
        "active": "coach_pregnancy_safety_active_title",
    }.get(status, "coach_pregnancy_safety_active_title")
    body_key = {
        "warning_symptoms": "coach_pregnancy_safety_warning_body",
        "not_cleared": "coach_pregnancy_safety_not_cleared_body",
        "clearance_unknown": "coach_pregnancy_safety_unknown_body",
        "active": "coach_pregnancy_safety_active_body",
    }.get(status, "coach_pregnancy_safety_active_body")
    return [
        {
            "title": translated_text("coach_plan_training_title", language),
            "sessions": [
                {
                    "number": 1,
                    "focus": translated_text(title_key, language),
                    "minutes": profile.session_minutes or 30,
                    "warmup": translated_text("coach_pregnancy_warmup", language),
                    "main": translated_text(body_key, language),
                    "cooldown": translated_text("coach_pregnancy_cooldown", language),
                    "exercises": [] if status in {"warning_symptoms", "not_cleared"} else [
                        {
                            "name": translated_text("coach_pregnancy_mobility", language),
                            "equipment": translated_text("coach_equipment_bodyweight", language),
                            "sets": "2",
                            "reps": "6-8",
                            "rest": "60 sec",
                            "load": translated_text("coach_pregnancy_load", language),
                            "cue": translated_text("coach_pregnancy_mobility_cue", language),
                        },
                        {
                            "name": translated_text("coach_pregnancy_walk", language),
                            "equipment": translated_text("coach_equipment_cardio", language),
                            "sets": "1",
                            "reps": "10-20 min",
                            "rest": "as needed",
                            "load": translated_text("coach_pregnancy_talk_test", language),
                            "cue": translated_text("coach_pregnancy_walk_cue", language),
                        },
                    ],
                }
            ],
        },
        {
            "title": translated_text("coach_plan_nutrition_title", language),
            "items": [
                translated_text("coach_pregnancy_nutrition_no_cutting", language),
                translated_text("coach_pregnancy_nutrition_balance", language),
                translated_text("coach_pregnancy_nutrition_provider", language),
            ],
        },
        {
            "title": translated_text("coach_plan_notes_title", language),
            "items": [
                translated_text("coach_pregnancy_safety_notice", language),
                translated_text("coach_pregnancy_provider_priority", language),
                pregnancy_context_summary(profile),
            ],
        },
    ]


def coach_personal_plan(profile, member=None, language=None):
    language = language or current_language()
    if not profile:
        return None
    pregnancy_plan = coach_pregnancy_safety_plan(profile, language)
    if pregnancy_plan and pregnancy_safety_status(profile) in {"warning_symptoms", "not_cleared", "clearance_unknown"}:
        return pregnancy_plan

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
    if is_pregnant_profile(profile) and profile.nutrition_goal == "fat_loss":
        nutrition_items[0] = translated_text("coach_pregnancy_nutrition_no_cutting", language)
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
    if is_pregnant_profile(profile):
        nutrition_items.append(translated_text("coach_pregnancy_nutrition_balance", language))
        nutrition_items.append(translated_text("coach_pregnancy_nutrition_provider", language))

    habits_items = [
        translated_text("coach_plan_train_at", language, place=coach_label("place", profile.training_place, language)),
        translated_text("coach_plan_home_equipment", language, equipment=profile.home_equipment or translated_text("not_available", language)),
        translated_text("coach_plan_preferences", language, preferences=profile.dietary_preferences or translated_text("not_available", language)),
        translated_text("coach_plan_limitations", language, limitations=profile.injuries or translated_text("not_available", language)),
        translated_text("coach_plan_allergies", language, allergies=profile.allergies or translated_text("not_available", language)),
    ]
    if is_pregnant_profile(profile):
        habits_items.insert(0, translated_text("coach_pregnancy_summary", language, weeks=profile.gestational_weeks or translated_text("not_available", language), trimester=pregnancy_trimester(profile) or translated_text("not_available", language)))
        habits_items.insert(1, translated_text("coach_pregnancy_provider_priority", language))

    return [
        {
            "title": translated_text("coach_plan_training_title", language),
            "sessions": sessions,
        },
        {
            "title": translated_text("coach_plan_nutrition_title", language),
            "items": nutrition_items,
            "meal_plan": personalized_nutrition_meal_plan(member, profile, language) if member else None,
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


def normalize_ai_meal_plan(value, fallback):
    if not isinstance(value, dict):
        return fallback
    plan = dict(fallback or {})
    if isinstance(value.get("summary"), str) and value["summary"].strip():
        plan["summary"] = clean_coach_plan_text(value["summary"], plan.get("summary"), 420)
    if isinstance(value.get("targets"), dict) and isinstance(plan.get("targets"), dict):
        targets = dict(plan["targets"])
        for key in ["calories", "protein", "carbs", "fat", "hydration_liters"]:
            if value["targets"].get(key) not in (None, ""):
                targets[key] = value["targets"].get(key)
        plan["targets"] = targets
    if isinstance(value.get("meals"), list) and value["meals"]:
        meals = []
        fallback_meals = plan.get("meals") or []
        for index, source in enumerate(value["meals"][:5]):
            if not isinstance(source, dict):
                continue
            fallback_meal = fallback_meals[index] if index < len(fallback_meals) else {}
            meal = dict(fallback_meal)
            for key in ["title", "name", "foods", "portions"]:
                meal[key] = clean_coach_plan_text(source.get(key), meal.get(key), 260)
            for key in ["calories", "protein", "carbs", "fat"]:
                if source.get(key) not in (None, ""):
                    meal[key] = source.get(key)
            meals.append(meal)
        if meals:
            plan["meals"] = meals
    if isinstance(value.get("alternatives"), list) and value["alternatives"]:
        alternatives = []
        fallback_alternatives = plan.get("alternatives") or []
        for index, source in enumerate(value["alternatives"][:4]):
            if not isinstance(source, dict):
                continue
            fallback_alt = fallback_alternatives[index] if index < len(fallback_alternatives) else {}
            alternatives.append({
                "title": clean_coach_plan_text(source.get("title"), fallback_alt.get("title"), 120),
                "body": clean_coach_plan_text(source.get("body"), fallback_alt.get("body"), 360),
            })
        if alternatives:
            plan["alternatives"] = alternatives
    return plan


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
            "meal_plan": normalize_ai_meal_plan(ai_nutrition.get("meal_plan"), fallback_nutrition.get("meal_plan")),
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
    if pregnancy_safety_status(profile) in {"warning_symptoms", "not_cleared"}:
        return None, coach_context_summary(member, profile)

    recent_workouts = "\n".join(recent_coach_workout_summary(member.member_id)) or "No previous workouts logged."
    context = coach_context_summary(member, profile)
    pregnancy_rules = (
        "Biological sex is required for training safety. Never mention pregnancy, prenatal training, pregnancy_status, pregnancy weight, or prenatal nutrition unless biological_sex=female and pregnancy_status=pregnant. "
        "Pregnancy safety rules only apply when biological_sex=female and pregnancy_status=pregnant: never prescribe aggressive fat loss, cutting, crash diets, max effort, PR attempts, high-impact/contact sport, high fall-risk exercises, overheating, dehydration, or prolonged supine exercises after 16 weeks. "
        "Use low-to-moderate intensity, talk-test pacing, hydration, safe strength, mobility, posture, breathing and pelvic floor focus. Provider restrictions always override the plan. "
        "If warning_symptoms are present or provider_cleared_exercise=no, do not generate a workout; advise the member to contact their doctor, midwife or healthcare provider before exercise. "
        "If provider_cleared_exercise=unknown, keep guidance cautious and low/moderate while advising medical clearance. Distinguish current_weight from pre_pregnancy_weight."
    )
    prompt = (
        "You create the Dreamz Fitness member coach plan. Return ONLY valid JSON, no markdown and no prose outside JSON. "
        "Do not mention AI. Personalize the plan to the profile, goal, experience, schedule, injuries, available home equipment, preferences and recent workout history. "
        "Use safe, practical exercise selection for Dreamz Fitness or the selected training place. "
        "If training place is both, split or clearly adapt the sessions for Dreamz Fitness and home. If training place is home, use only the listed home equipment; if none is listed, use bodyweight and simple household-safe options. "
        "Nutrition must be realistic for Bonaire: budget-aware, common supermarket foods, simple repeatable meals, enough protein. Allergies and foods to avoid are strict constraints: never suggest those foods or close substitutes. "
        "For injuries or medical limitations, adjust exercise choices and intensity conservatively. "
        f"{pregnancy_rules} "
        "Group classes count toward total training load. Use planned and attended group classes, class intensity, muscle focus, cardio load, strength load and recovery impact to adjust the personal workout week. "
        "Do not schedule heavy full-body or lower-body strength immediately after high recovery-impact classes such as BODYPUMP, TOTAL BODY, SPINNING or BOOTY SHAPE. Yoga and Pilates can count as mobility/recovery/core work. "
        "If schedule changes are present, explain practical future adjustments without modifying completed history. "
        "Use the member selected language for every visible value. "
        "The number of sessions must match the fallback sessions. Each session should fit the requested minutes including warm-up and cool-down. "
        "JSON schema: {"
        "\"training\":{\"title\":\"...\",\"sessions\":[{\"focus\":\"...\",\"minutes\":45,\"warmup\":\"...\",\"main\":\"...\",\"cooldown\":\"...\","
        "\"exercises\":[{\"name\":\"...\",\"equipment\":\"...\",\"sets\":\"...\",\"reps\":\"...\",\"rest\":\"...\",\"load\":\"...\",\"cue\":\"...\"}]}]},"
        "\"nutrition\":{\"title\":\"...\",\"items\":[\"...\"],\"meal_plan\":{\"summary\":\"...\","
        "\"targets\":{\"calories\":2500,\"protein\":150,\"carbs\":300,\"fat\":75,\"hydration_liters\":3},"
        "\"meals\":[{\"title\":\"Breakfast\",\"name\":\"...\",\"foods\":\"...\",\"portions\":\"...\",\"calories\":600,\"protein\":35,\"carbs\":70,\"fat\":18}],"
        "\"alternatives\":[{\"title\":\"Budget alternative\",\"body\":\"...\"}]}},"
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

    fallback_plan = coach_personal_plan(profile, member=member, language=language)
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


def nutrition_plan_context(member, profile=None, language=None):
    language = language or current_language()
    profile = profile or coach_profile_for_member(member)
    if not member or not profile:
        return {
            "nutrition_section": None,
            "meal_plan": None,
            "nutrition_next_meal": None,
            "profile": profile,
            "meal_logs": [],
        }

    personal_plan = coach_plan_for_member(member, profile, language=language)
    nutrition_section = personal_plan[1] if personal_plan and len(personal_plan) > 1 else None
    meal_plan = nutrition_section.get("meal_plan") if isinstance(nutrition_section, dict) else None
    if not meal_plan:
        meal_plan = personalized_nutrition_meal_plan(member, profile, language)
        if isinstance(nutrition_section, dict):
            nutrition_section = dict(nutrition_section)
            nutrition_section["meal_plan"] = meal_plan
    nutrition_next_meal = meal_plan["meals"][0] if meal_plan and meal_plan.get("meals") else None
    meal_logs = (
        MealLog.query
        .filter_by(member_id=member.member_id)
        .order_by(MealLog.logged_at.desc(), MealLog.id.desc())
        .limit(8)
        .all()
    )
    return {
        "nutrition_section": nutrition_section,
        "meal_plan": meal_plan,
        "nutrition_next_meal": nutrition_next_meal,
        "profile": profile,
        "personal_plan": personal_plan,
        "meal_logs": meal_logs,
    }


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
    attended_classes = (
        MemberClassAttendance.query
        .filter_by(member_id=member_id)
        .order_by(MemberClassAttendance.class_date.desc(), MemberClassAttendance.id.desc())
        .limit(limit)
        .all()
    )
    for attendance in attended_classes:
        summaries.append(
            f"{attendance.class_date}: group class attended {attendance.occurrence.class_type.name}, "
            f"intensity={attendance.occurrence.class_type.intensity}, "
            f"cardio_load={attendance.occurrence.class_type.cardio_load or '-'}, "
            f"strength_load={attendance.occurrence.class_type.strength_load or '-'}, "
            f"recovery_impact={attendance.occurrence.class_type.recovery_impact or '-'}"
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


def floating_coach_recent_interactions(member_id, limit=4):
    if not member_id or is_staff_user():
        return []
    try:
        return coach_recent_interactions(member_id, limit=limit)
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not load floating Dreamz Coach conversation preview.")
        return []


def floating_coach_recent_threads(member_id, limit=4):
    if not member_id or is_staff_user():
        return []
    try:
        rows = (
            CoachInteraction.query
            .filter_by(member_id=member_id)
            .order_by(CoachInteraction.created_at.desc(), CoachInteraction.id.desc())
            .limit(max(limit * 3, 8))
            .all()
        )
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not load floating Dreamz Coach conversation threads.")
        return []

    exchanges = []
    current = None
    for item in reversed(rows):
        if item.actor == "member":
            current = {"member_message": item, "coach_message": None, "created_at": item.created_at}
            exchanges.append(current)
            continue
        if current and current.get("coach_message") is None:
            current["coach_message"] = item
            current["created_at"] = item.created_at or current.get("created_at")
        else:
            exchanges.append({"member_message": None, "coach_message": item, "created_at": item.created_at})
            current = None

    exchanges.sort(key=lambda entry: entry.get("created_at") or datetime.min, reverse=True)
    return exchanges[:limit]


def portal_context_value(label, default, factory):
    try:
        with db.session.no_autoflush:
            return factory()
    except Exception:
        app.logger.exception("Dreamz Coach portal context failed: %s", label)
        return default


def portal_context_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return "unknown"


def portal_context_bool(value):
    if value is None:
        return "unknown"
    return "yes" if bool(value) else "no"


def portal_member_context_summary(member, profile, language=None):
    language = language or (current_language() if has_request_context() else DEFAULT_LANGUAGE)
    if not member:
        return "member_portal_context=unavailable"

    policy = portal_context_value(
        "cancellation_policy",
        None,
        lambda: cancellation_policy_for_member(member),
    )
    payment_status = portal_context_value(
        "payment_status",
        {},
        lambda: payment_status_for_member(member),
    )
    account_notifications = portal_context_value(
        "account_notifications",
        0,
        lambda: member_account_notification_count(member.member_id),
    )
    document_groups = portal_context_value(
        "member_documents",
        [],
        lambda: member_document_groups(member),
    )
    signed_documents = portal_context_value(
        "signed_documents",
        [],
        lambda: (
            MemberSignedDocument.query
            .filter_by(member_id=member.member_id)
            .order_by(MemberSignedDocument.signed_at.desc(), MemberSignedDocument.uploaded_at.desc())
            .limit(8)
            .all()
        ),
    )
    applications = portal_context_value(
        "membership_applications",
        [],
        lambda: (
            MembershipApplication.query
            .filter(
                db.or_(
                    MembershipApplication.email == member.email,
                    MembershipApplication.phone == (member.mobile or member.phone),
                )
            )
            .order_by(MembershipApplication.created_at.desc(), MembershipApplication.id.desc())
            .limit(5)
            .all()
        ) if (member.email or member.mobile or member.phone) else [],
    )
    pricing_items = portal_context_value(
        "member_pricing",
        [],
        lambda: [
            item for item in active_pricing_items_for_visibility(["public", "members"])
            if item.member_eligible and item.category_key != "external_trainer_b2b"
        ][:14],
    )
    meal_plan = portal_context_value(
        "nutrition_meal_plan",
        None,
        lambda: personalized_nutrition_meal_plan(member, profile, language) if profile else None,
    )
    meal_logs = portal_context_value(
        "meal_logs",
        [],
        lambda: (
            MealLog.query
            .filter_by(member_id=member.member_id)
            .order_by(MealLog.logged_at.desc(), MealLog.id.desc())
            .limit(8)
            .all()
        ),
    )
    targets = meal_plan.get("targets") if isinstance(meal_plan, dict) else {}
    missing_nutrition = (
        meal_plan.get("missing_data")
        if isinstance(meal_plan, dict) else
        [item["label"] for item in nutrition_missing_profile_items(member, profile, language)]
    )
    progress = portal_context_value(
        "progress_entries",
        [],
        lambda: (
            CoachProgressEntry.query
            .filter_by(member_id=member.member_id)
            .order_by(CoachProgressEntry.created_at.desc(), CoachProgressEntry.id.desc())
            .limit(5)
            .all()
        ),
    )
    next_session = portal_context_value(
        "next_session",
        None,
        lambda: coach_personal_plan(profile, member=member, language=language)[0]["sessions"][0]
        if profile and coach_personal_plan(profile, member=member, language=language) else None,
    )
    recent_workouts = portal_context_value(
        "recent_workouts",
        [],
        lambda: recent_coach_workout_summary(member.member_id, limit=5),
    )

    policy_status = policy.status if policy else "unknown"
    policy_window = (
        f"current_term_end={portal_context_date(policy.current_term_end)}, "
        f"window_open={portal_context_date(policy.window_open)}, "
        f"window_close={portal_context_date(policy.last_request_date)}, "
        f"can_request={portal_context_bool(policy.can_request)}"
        if policy else
        "current_term_end=unknown, window_open=unknown, window_close=unknown, can_request=unknown"
    )
    document_text = "; ".join(
        f"{group.get('document_type')} count={len(group.get('documents', []))}"
        for group in document_groups
    ) or "none"
    signed_text = "; ".join(
        f"{doc.document_type} status={doc.status} signed_at={portal_context_date(doc.signed_at)}"
        for doc in signed_documents
    ) or "none"
    application_text = "; ".join(
        f"application_id={application.id} status={application.status} membership={application.selected_membership_type or 'unknown'} payment={application.selected_payment_method or 'unknown'}"
        for application in applications
    ) or "none"
    pricing_text = "; ".join(
        f"{item.name} ${item.price_amount:.2f} {item.billing_interval} category={item.category_key} frontdesk={portal_context_bool(item.requires_front_desk_handling)} online_payment={portal_context_bool(item.online_payment_available)}"
        for item in pricing_items
    ) or "unavailable"
    progress_text = "; ".join(
        f"{portal_context_date(entry.created_at)} type={entry.entry_type} weight={entry.weight_kg or 'unknown'} notes={entry.notes or 'none'}"
        for entry in progress
    ) or "none"
    meal_logs_text = "; ".join(
        f"{portal_context_date(log.logged_at)} {log.meal_key} {log.log_type} foods={log.food_items or 'not specified'}"
        for log in (meal_logs or [])[:5]
    ) or "none"
    next_session_text = "none"
    if next_session:
        session = next_session
        next_session_text = (
            f"session={session.get('number')} focus={session.get('focus')} "
            f"minutes={session.get('minutes')} exercises={len(session.get('exercises') or [])}"
        )

    return (
        "member_portal_context="
        f"member_id={member.member_id}, name={display_member_name(member.name) or 'unknown'}, "
        f"email_present={portal_context_bool(member.email)}, phone_present={portal_context_bool(member.mobile or member.phone)}, "
        f"birthdate={portal_context_date(member.birthdate)}, visits={member.visits if member.visits is not None else 'unknown'}; "
        "account_membership="
        f"plan={member.plan_type or 'unknown'}, contract_type={member.contract_type or 'unknown'}, "
        f"billing_status={member.billing_status or 'unknown'}, active={portal_context_bool(member.is_active)}, "
        f"signup_date={portal_context_date(member.signup_date)}, contract_start={portal_context_date(member.start_date)}, contract_end={portal_context_date(member.end_date)}, "
        f"billing_amount=${member.billing_amount or 0:.2f}, last_payment={portal_context_date(member.last_payment)}, next_payment={portal_context_date(member.next_payment)}; "
        "account_balance="
        f"open_gym_balance=${member.balance or 0:.2f}, due_date={portal_context_date(member.due_date)}, "
        f"payment_status={payment_status.get('status', 'unknown')}, payment_reason={payment_status.get('reason', 'unknown')}, "
        "payment_method=frontdesk_for_gym_balance, online_payment_available_for_gym_balance=no, "
        f"account_notifications={account_notifications}; "
        "cancellation="
        f"status={policy_status}, {policy_window}; "
        "documents_and_agreements="
        f"document_groups={document_text}; signed_documents={signed_text}; relevant_legal_documents={','.join(member_relevant_legal_document_types(member))}; "
        f"membership_applications={application_text}; "
        "pricing_catalog="
        f"{pricing_text}; "
        "nutrition="
        f"targets_calories={targets.get('calories', 'unknown')}, targets_protein={targets.get('protein', 'unknown')}, "
        f"targets_carbs={targets.get('carbs', 'unknown')}, targets_fat={targets.get('fat', 'unknown')}, "
        f"missing_nutrition_data={', '.join(missing_nutrition or []) or 'none'}, meal_logs={meal_logs_text}; "
        "training_and_progress="
        f"next_session={next_session_text}; recent_workouts={' | '.join(recent_workouts) or 'none'}; progress={progress_text}; "
        "member_routes="
        "/dashboard, /coach, /nutrition, /progress, /group-classes, /account, /account/membership, /account/billing, /account/agreements, /account/documents, /pricing"
    )


def coach_context_summary(member, profile):
    portal_context = portal_member_context_summary(member, profile)
    if not profile:
        return (
            f"Member {member.member_id}: no coach profile yet; "
            f"{portal_context}; "
            f"{coach_today_group_class_context(member)}"
        )
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
        f"allergies={profile.allergies or 'none'}, home_equipment={profile.home_equipment or 'none'}, "
        f"{pregnancy_context_summary(profile)}"
        f"{progress_text}; "
        f"{portal_context}; "
        f"{coach_today_group_class_context(member)}; "
        f"{group_class_training_load_context(member, profile)}"
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
    if user_message and is_group_class_schedule_question(user_message):
        return coach_today_group_class_reply(member, language)
    if pregnancy_safety_status(profile) in {"warning_symptoms", "not_cleared"}:
        return translated_text("coach_pregnancy_reply_medical_first", language)
    if pregnancy_safety_status(profile) == "clearance_unknown":
        return translated_text("coach_pregnancy_reply_clearance_unknown", language)
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


def is_group_class_schedule_question(message):
    text = (message or "").lower()
    class_terms = (
        "groepsles", "groepslessen", "group class", "group classes", "klasnan",
        "clases", "classes", "les vandaag", "lessen vandaag", "rooster", "schedule",
    )
    today_terms = ("vandaag", "today", "awe", "hoy", "vanavond", "tonight", "awor")
    return any(term in text for term in class_terms) and (
        any(term in text for term in today_terms) or "welke" in text or "what" in text or "kiko" in text
    )


def coach_today_group_class_reply(member, language=None):
    language = language or current_language()
    rows = coach_today_group_class_rows(member)
    heading = translated_text("coach_today_classes_heading", language)
    if not rows:
        return "\n".join([
            heading,
            "",
            translated_text("coach_today_classes_none", language),
            translated_text("coach_today_classes_open_schedule", language),
        ])
    status_key = {
        "past": "class_status_ended",
        "live": "class_status_live",
        "upcoming": "class_status_upcoming",
        "cancelled": "class_status_cancelled",
    }
    lines = [
        heading,
        "",
        translated_text("coach_today_classes_summary", language),
    ]
    for row in rows:
        occurrence = row["occurrence"]
        status = translated_text(status_key.get(row.get("time_status"), "class_status_upcoming"), language)
        lines.append(
            f"- {occurrence.start_time.strftime('%H:%M')}-{occurrence.end_time.strftime('%H:%M')} "
            f"{occurrence.class_type.name} - {occurrence.room} ({status})"
        )
    lines.extend(["", translated_text("coach_today_classes_open_schedule", language)])
    return "\n".join(lines)


def is_balance_or_billing_question(message):
    text = (message or "").lower()
    terms = (
        "schuld", "schulden", "saldo", "open balance", "balance", "gym balance",
        "owe", "debt", "payment", "betaling", "betalen", "factuur", "rekening",
        "debo", "saldo habri", "pago", "deuda", "saldo abierto",
    )
    return any(term in text for term in terms)


def is_membership_or_contract_question(message):
    text = (message or "").lower()
    terms = (
        "membership", "lidmaatschap", "contract", "abonnement", "membership type",
        "plan", "opzeg", "opzeggen", "cancellation", "cancel", "renewal", "verleng",
        "membresia", "contrato", "cancelacion", "renovacion",
    )
    return any(term in text for term in terms)


def is_pricing_question(message):
    text = (message or "").lower()
    terms = (
        "prijs", "prijzen", "kosten", "kost", "tarief", "tarieven", "pricing",
        "price", "cost", "fee", "fees", "membership options", "opciones",
        "precio", "precios", "kuantu", "preis",
    )
    return any(term in text for term in terms)


def coach_portal_direct_reply(member, profile, user_message, language=None):
    language = language or current_language()
    if not member or not user_message:
        return None
    balance = member.balance or 0
    if is_balance_or_billing_question(user_message):
        if balance > 0:
            return translated_text(
                "coach_portal_balance_open",
                language,
                amount=f"${balance:.2f}",
                due_date=fmt_policy_date(member.due_date or member.next_payment, language),
            )
        return translated_text(
            "coach_portal_balance_clear",
            language,
            next_payment=fmt_policy_date(member.next_payment, language),
        )
    if is_membership_or_contract_question(user_message):
        policy = portal_context_value("direct_membership_policy", None, lambda: cancellation_policy_for_member(member))
        if policy:
            return translated_text(
                "coach_portal_membership_summary",
                language,
                plan=member.plan_type or translated_text("not_available", language),
                contract=member.contract_type or translated_text("not_available", language),
                term_end=fmt_policy_date(policy.current_term_end, language),
                window_open=fmt_policy_date(policy.window_open, language),
                window_close=fmt_policy_date(policy.last_request_date, language),
            )
    if is_pricing_question(user_message):
        items = portal_context_value(
            "direct_pricing_items",
            [],
            lambda: [
                item for item in active_pricing_items_for_visibility(["public", "members"])
                if item.member_eligible and item.category_key != "external_trainer_b2b"
            ][:8],
        )
        if items:
            lines = [
                translated_text("coach_portal_pricing_summary", language),
                "",
            ]
            lines.extend(
                f"- {item.name}: ${item.price_amount:.2f} ({translated_text('pricing_interval_' + item.billing_interval, language)})"
                for item in items
            )
            lines.extend(["", translated_text("coach_portal_pricing_open_catalog", language)])
            return "\n".join(lines)
    return None


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
    if user_message and is_group_class_schedule_question(user_message):
        return coach_today_group_class_reply(member, language), "portal_context", context
    if user_message:
        direct_reply = coach_portal_direct_reply(member, profile, user_message, language=language)
        if direct_reply:
            return direct_reply, "portal_context", context
    recent_workouts = "\n".join(recent_coach_workout_summary(member.member_id)) or "No previous workouts logged."
    workout_text = ""
    if workout_logs:
        workout_text = "\n".join(
            f"- {log.exercise_name}: planned {log.planned_sets} sets x {log.planned_reps}, actual {log.weight_used or '-'} x {log.reps_completed or '-'}"
            for log in workout_logs
        )
    prompt = (
        "You are the Dreamz Fitness digital coach and part of the Dreamz coaching service. "
        "Before answering any member question, first inspect and use the member_portal_context below as the primary source of truth. "
        "This context represents the member-facing portal only: dashboard, coach/training, nutrition, progress, group classes, account, membership, billing/gym balance, pricing, agreements, documents, applications and cancellation. Do not use or invent staff/admin data. "
        "If the answer is present in the portal context, answer specifically from that data and point the member to the relevant app route when useful. "
        "If a field is missing or unknown in the portal context, say exactly what is missing and what the member can update in the portal. "
        "Reply in the member's selected language. Give direct, practical coaching that uses the profile, recent workouts and current plan. "
        "Be specific about next training actions, weights/reps progression, food choices, recovery and what to log next. "
        "For account, membership, pricing, agreements, documents, cancellation, gym balance or payment questions, use the portal context first. Gym purchase balances are paid at the front desk; do not say Pay now unless online_payment_available=yes is explicitly present. "
        "Do not say you are an AI. Do not refer routine questions to Dreamz staff when the portal context already answers them. "
        "For serious red flags such as chest pain, fainting, severe injury, severe dizziness or medical emergencies, tell the member to stop and seek qualified medical help. "
        "Never mention pregnancy, prenatal training, pregnancy_status, pregnancy weight, or prenatal nutrition unless biological_sex=female and pregnancy_status=pregnant. "
        "Pregnancy safety rules only apply when biological_sex=female and pregnancy_status=pregnant: do not advise aggressive fat loss/cutting, max-effort/PR training, high-impact/contact sport, high fall-risk exercises, overheating, dehydration, or prolonged supine exercises after 16 weeks. Use talk-test moderate intensity, safe strength, mobility, breathing, pelvic floor and hydration guidance. Provider restrictions always override. If warning symptoms are present or provider_cleared_exercise=no, do not give a workout progression; advise contacting doctor/midwife/healthcare provider before exercise. If clearance is unknown, keep advice cautious and low/moderate while recommending clearance. "
        "Group classes count toward total training load. Use planned and attended group classes, intensity, muscle focus, cardio load, strength load, recovery impact and schedule changes when giving next-session, recovery, nutrition and progress advice. Never silently change completed history. "
        "The portal context can include today's group class schedule with times, room and status. If the member asks which classes are today, answer from that schedule and point them to the Group Classes schedule in the app. Do not say you do not have live schedule data when today_group_class_schedule is present. "
        "If the member planned BODYPUMP, TOTAL BODY, SPINNING or BOOTY SHAPE this week, adjust nearby strength/cardio volume accordingly. "
        "Use Bonaire-friendly, realistic and budget-aware training and nutrition advice. "
        "Format the reply for a mobile app: start with a short Coach Summary, then 3-5 concrete action bullets, then an optional Details section. Keep it concise and avoid long essays.\n\n"
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

    legacy_documents = [
        ("signup_form", "signup-form", "form_path", 10),
        ("contract", "contract", "contract_path", 20),
        ("direct_debit_mandate", "mandate", "mandate_path", 30),
    ]
    for document_type, route_type, field_name, display_order in legacy_documents:
        if document_type in groups:
            continue
        document_path = getattr(member, field_name, None)
        if not document_path:
            continue
        group = groups.setdefault(
            document_type,
            {
                "document_type": document_type,
                "documents": [],
                "first_order": display_order,
                "first_id": 0,
            },
        )
        normalized_path = document_path.replace("\\", "/")
        group["documents"].append({
            "route_type": route_type,
            "source_filename": Path(normalized_path).name,
        })

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
    try:
        ensure_runtime_schema()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while loading staff users; using configured staff fallback.")
        return configured_staff_user_fallbacks()
    try:
        users = {
            user.username.strip().lower(): user
            for user in StaffUser.query.filter_by(is_active=True).all()
            if user.username
        }
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Staff user table unavailable; using configured staff fallback.")
        return configured_staff_user_fallbacks()
    return users or configured_staff_user_fallbacks()


def current_staff_role():
    role = session.get("staff_role")
    return role if role in {"admin", "manager"} else None


def current_staff_username():
    username = session.get("staff_username")
    return username if isinstance(username, str) else None


def current_staff_user():
    username = current_staff_username()
    if not username:
        return None
    return StaffUser.query.filter_by(username=username).first()


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
                "due_date": due_date,
                "balance": balance,
            }
        return {
            "status": "stale_payment_data",
            "label": "Payment data may be stale",
            "severity": "warning",
            "reason": f"Next payment/due date is {due_date.isoformat()}, before today, but balance is $0.00.",
            "due_date": due_date,
        }

    if due_date == today:
        return {
            "status": "due_today",
            "label": "Due today",
            "severity": "info",
            "reason": "Next payment/due date is today.",
            "due_date": due_date,
        }

    if balance > 0:
        return {
            "status": "balance_open",
            "label": "Open balance",
            "severity": "warning",
            "reason": f"Outstanding balance is ${balance:.2f}.",
            "balance": balance,
            "due_date": due_date or date(today.year, today.month, calendar.monthrange(today.year, today.month)[1]),
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


def language_cookie_name():
    return app.config["LANGUAGE_COOKIE_NAME"]


def valid_language_choice(language):
    return language if language in LANGUAGES else None


def language_choice_from_request():
    session_language = valid_language_choice(session.get("language"))
    if session_language:
        return session_language
    if has_request_context():
        return valid_language_choice(request.cookies.get(language_cookie_name()))
    return None


def current_language():
    return language_choice_from_request() or DEFAULT_LANGUAGE


def t(key):
    return translate(key, current_language())


def safe_local_next_url(value, fallback=None):
    fallback = fallback or url_for("login")
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def redirect_with_language_cookie(next_url, language):
    response = redirect(next_url)
    response.set_cookie(
        language_cookie_name(),
        language,
        max_age=app.config["LANGUAGE_COOKIE_DAYS"] * 24 * 60 * 60,
        samesite="Lax",
        secure=app.config["SESSION_COOKIE_SECURE"],
    )
    return response


@app.before_request
def load_language_cookie():
    cookie_language = valid_language_choice(request.cookies.get(language_cookie_name()))
    if cookie_language and not valid_language_choice(session.get("language")):
        session["language"] = cookie_language


@app.before_request
def require_language_selection_for_entry():
    if request.method != "GET":
        return None
    if session.get("member_id") or session.get("staff_role"):
        return None
    if valid_language_choice(session.get("language")) or valid_language_choice(request.cookies.get(language_cookie_name())):
        return None
    if request.endpoint in {
        "choose_language",
        "set_language",
        "static",
        "web_manifest",
        "service_worker",
    }:
        return None
    if request.endpoint in {"home", "login"}:
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("choose_language", next=safe_local_next_url(next_url)))
    return None

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
    member_id = session.get("member_id")
    return {
        "csrf_token": get_csrf_token,
        "t": t,
        "current_language": current_language(),
        "available_languages": LANGUAGES,
        "language_flags": LANGUAGE_FLAGS,
        "current_member_id": member_id,
        "current_staff_role": current_staff_role(),
        "current_staff_username": current_staff_username(),
        "account_notification_count": member_account_notification_count(member_id) if member_id and not is_staff_user() else 0,
        "floating_coach_interactions": floating_coach_recent_interactions(member_id) if member_id and not is_staff_user() else [],
        "floating_coach_threads": floating_coach_recent_threads(member_id) if member_id and not is_staff_user() else [],
        "open_cancellation_count": open_cancellation_count() if is_staff_user() else 0,
        "open_email_log_count": open_email_log_count() if is_staff_user() else 0,
        "t_document_title": lambda document_type: translated_document_title(document_type, current_language()),
        "t_document_explanation": lambda document_type: translated_document_explanation(document_type, current_language()),
        "t_coach_focus": lambda value: translated_coach_focus_label(value, current_language()),
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


@app.template_filter("coach_message_html")
def coach_message_html(value):
    escaped = escape_html(value or "")
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    blocks = []
    for line in escaped.splitlines():
        line = line.strip()
        if not line:
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            blocks.append(
                '<div class="dreamz-coach-line-bullet">'
                '<span aria-hidden="true"></span>'
                f"<p>{bullet.group(1)}</p>"
                "</div>"
            )
        else:
            blocks.append(f"<p>{line}</p>")
    return Markup("".join(blocks))

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


def agreement_admin_recipients():
    recipients = []
    for key in ("notification_to", "frontdesk_email", "admin_email"):
        for email in parse_email_list(setting_value(key, "")):
            append_unique_email(recipients, email)
    return recipients


def build_application_notification_email(application, recipient_type="member"):
    language = normalize_language(application.language or DEFAULT_LANGUAGE)
    applicant_name = f"{application.applicant_first_name or ''} {application.applicant_last_name or ''}".strip()
    if recipient_type == "member":
        subject = translated_text("email_application_member_subject", language)
        intro = translated_text("email_application_member_intro", language, name=applicant_name or application.email)
        note = translated_text("email_application_member_note", language)
    else:
        subject = translated_text("email_application_admin_subject", language, name=applicant_name or application.email)
        intro = translated_text("email_application_admin_intro", language, name=applicant_name or application.email)
        note = translated_text("email_application_admin_note", language)
    body = f"""{intro}

{translated_text('application_title', language)}: #{application.id}
{translated_text('email', language)}: {application.email or ''}
{translated_text('current_membership', language)}: {application.selected_membership_type or ''}
{translated_text('payment_direct_debit', language)}: {application.selected_payment_method or ''}

{note}

{translated_text('email_signoff', language)}
Dreamz Fitness Bonaire
"""
    html_body = email_html_layout(
        subject,
        intro,
        [
            (translated_text("application_title", language), f"#{application.id}"),
            (translated_text("email", language), application.email or ""),
            (translated_text("current_membership", language), application.selected_membership_type or ""),
            (translated_text("payment_direct_debit", language), application.selected_payment_method or ""),
        ],
        note=note,
        signoff=translated_text("email_signoff", language),
        tone="success",
    )
    return subject, body, html_body


def send_application_notifications(application):
    now = datetime.now()
    statuses = []
    if application.email:
        subject, body, html_body = build_application_notification_email(application, recipient_type="member")
        statuses.append(("member", deliver_email([application.email], subject, body, html_body=html_body)))
        SignedPdfRecord.query.filter_by(application_id=application.id).update({"emailed_to_member_at": now})
    admin_recipients = agreement_admin_recipients()
    if admin_recipients:
        subject, body, html_body = build_application_notification_email(application, recipient_type="admin")
        statuses.append(("admin", deliver_email(admin_recipients, subject, body, html_body=html_body)))
        SignedPdfRecord.query.filter_by(application_id=application.id).update({"emailed_to_admin_at": now})
    db.session.add(MembershipApplicationAuditEvent(
        application_id=application.id,
        event_type="application_notifications_sent",
        actor_type="system",
        message="Application notifications generated for member/admin.",
        metadata_json=json.dumps({"statuses": statuses}),
    ))
    db.session.commit()
    return statuses


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


@app.get("/choose-language")
def choose_language():
    next_url = safe_local_next_url(request.args.get("next"), url_for("login"))
    if next_url == "/":
        next_url = url_for("login")
    if language_choice_from_request():
        return redirect(next_url)
    language_cards = [
        {
            "code": code,
            "label": label,
            "flag": LANGUAGE_FLAGS.get(code, ""),
            "title": translated_text("choose_language_title", code),
            "action": translated_text("continue_in_language", code),
            "aria_label": translated_text("choose_language_card_aria", code, display_language=label),
        }
        for code, label in LANGUAGES.items()
    ]
    return render_template("language_select.html", next_url=next_url, language_cards=language_cards)


@app.get("/pricing")
def public_pricing():
    return render_template("pricing.html", **public_pricing_context())


@app.route("/apply", methods=["GET", "POST"])
def membership_application():
    ensure_runtime_schema()
    language = current_language()
    seed_pricing_catalog()
    seed_legal_documents()
    membership_options = [
        item for item in active_pricing_items_for_visibility(["public", "members"])
        if item.category_key in {"memberships", "mcb_direct_debit_contracts", "day_week_passes", "under_18"}
        and item.member_eligible
    ]
    addon_options = [
        item for item in active_pricing_items_for_visibility(["public", "members"])
        if item.category_key in {"group_class_add_ons", "personal_training"}
        and item.member_eligible
    ]
    preview_contract_term = request.form.get("selected_contract_term", "none") if request.method == "POST" else "none"
    preview_payment_method = request.form.get("selected_payment_method", "frontdesk_payment") if request.method == "POST" else "frontdesk_payment"
    preview_addons = request.form.getlist("selected_add_ons") if request.method == "POST" else []
    required_rows = current_versions_for_document_types(
        application_required_document_types(preview_contract_term, preview_payment_method, preview_addons)
    )

    if request.method == "POST":
        validate_csrf_token()
        account_type = request.form.get("mcb_account_type", "").strip()
        payment_method = request.form.get("selected_payment_method", "").strip()
        if payment_method == "mcb_direct_debit_monthly":
            if account_type != "current" or request.form.get("direct_debit_confirmed_current_account") != "1":
                flash(translated_text("application_direct_debit_blocked", language))
                return render_template(
                    "membership_application.html",
                    membership_options=membership_options,
                    addon_options=addon_options,
                    required_agreements=required_rows,
                    form_data=request.form,
                ), 400

        selected_add_ons = request.form.getlist("selected_add_ons")
        required_rows = current_versions_for_document_types(
            application_required_document_types(
                request.form.get("selected_contract_term", "none").strip(),
                payment_method,
                selected_add_ons,
            )
        )
        required_version_ids = [str(version.id) for _, version in required_rows]
        accepted_version_ids = set(request.form.getlist("accepted_version_id"))
        if not required_version_ids or set(required_version_ids) - accepted_version_ids:
            flash(translated_text("application_accept_required_agreements", language))
            return render_template(
                "membership_application.html",
                membership_options=membership_options,
                addon_options=addon_options,
                required_agreements=required_rows,
                form_data=request.form,
            ), 400

        signature_name = request.form.get("signature_text_name", "").strip()
        first_name = request.form.get("applicant_first_name", "").strip()
        last_name = request.form.get("applicant_last_name", "").strip()
        email = request.form.get("email", "").strip()
        if not first_name or not last_name or not email or not signature_name or request.form.get("information_true") != "1":
            flash(translated_text("application_signature_required", language))
            return render_template(
                "membership_application.html",
                membership_options=membership_options,
                addon_options=addon_options,
                required_agreements=required_rows,
                form_data=request.form,
            ), 400

        now = datetime.now()
        application = MembershipApplication(
            applicant_first_name=first_name,
            applicant_last_name=last_name,
            date_of_birth=parse_optional_date(request.form.get("date_of_birth")),
            place_of_birth=request.form.get("place_of_birth", "").strip() or None,
            address=request.form.get("address", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            email=email,
            emergency_contact_first_name=request.form.get("emergency_contact_first_name", "").strip() or None,
            emergency_contact_last_name=request.form.get("emergency_contact_last_name", "").strip() or None,
            emergency_contact_relationship=request.form.get("emergency_contact_relationship", "").strip() or None,
            emergency_contact_phone=request.form.get("emergency_contact_phone", "").strip() or None,
            selected_membership_type=request.form.get("selected_membership_type", "").strip(),
            selected_contract_term=request.form.get("selected_contract_term", "none").strip() or "none",
            selected_add_ons=json.dumps(selected_add_ons),
            selected_payment_method=payment_method,
            mcb_account_holder_name=request.form.get("mcb_account_holder_name", "").strip() or None,
            mcb_account_number=request.form.get("mcb_account_number", "").strip() or None,
            mcb_account_type=account_type or None,
            direct_debit_confirmed_current_account=request.form.get("direct_debit_confirmed_current_account") == "1",
            status="pending_frontdesk_payment",
            language=language,
            submitted_at=now,
            signed_at=now,
        )
        db.session.add(application)
        db.session.flush()

        audit_reference = f"APP-{now.strftime('%Y%m%d')}-{application.id:05d}-{secrets.token_hex(3).upper()}"
        signature = DigitalSignatureRecord(
            application_id=application.id,
            full_legal_name=signature_name,
            email=email,
            phone=application.phone,
            date_of_birth=application.date_of_birth,
            signature_text_name=signature_name,
            signed_at=now,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
            user_agent=request.headers.get("User-Agent"),
            verification_method="email_link",
            verification_reference=email,
            audit_reference_number=audit_reference,
        )
        db.session.add(signature)
        db.session.flush()

        db.session.add(MembershipApplicationStatus(application_id=application.id, status=application.status, note="Application submitted and signed."))
        db.session.add(MembershipApplicationAuditEvent(
            application_id=application.id,
            event_type="application_signed",
            actor_type="applicant",
            actor_id=email,
            message="This document was signed electronically through the Dreamz Fitness member portal.",
            metadata_json=json.dumps({"audit_reference_number": audit_reference}),
        ))
        db.session.add(DigitalSignatureAuditTrail(
            signature_record_id=signature.id,
            event_type="signature_created",
            message="This document was signed electronically through the Dreamz Fitness member portal.",
            ip_address=signature.ip_address,
            user_agent=signature.user_agent,
        ))

        for document, version in required_rows:
            pdf_hash = create_application_pdf_hash(application, document.document_type, audit_reference)
            db.session.add(MembershipApplicationDocument(
                application_id=application.id,
                legal_document_version_id=version.id,
                document_type=document.document_type,
                status="signed",
                file_url=f"/applications/{application.id}/documents/{document.document_type}.pdf",
                pdf_hash=pdf_hash,
                language=language,
            ))
            db.session.add(SignedPdfRecord(
                application_id=application.id,
                document_type=document.document_type,
                legal_document_version_id=version.id,
                language=language,
                pdf_url=f"/applications/{application.id}/documents/{document.document_type}.pdf",
                pdf_hash=pdf_hash,
                audit_reference_number=audit_reference,
                status="generated",
            ))
        db.session.commit()
        try:
            send_application_notifications(application)
        except Exception as exc:
            db.session.add(MembershipApplicationAuditEvent(
                application_id=application.id,
                event_type="application_notification_failed",
                actor_type="system",
                message=str(exc),
            ))
            db.session.commit()
        return redirect(url_for("membership_application_confirmation", application_id=application.id))

    return render_template(
        "membership_application.html",
        membership_options=membership_options,
        addon_options=addon_options,
        required_agreements=required_rows,
        form_data={},
    )


@app.get("/apply/confirmation/<int:application_id>")
def membership_application_confirmation(application_id):
    ensure_runtime_schema()
    application = db.session.get(MembershipApplication, application_id)
    if not application:
        abort(404)
    documents = MembershipApplicationDocument.query.filter_by(application_id=application.id).all()
    return render_template("membership_application_confirmation.html", application=application, documents=documents)


@app.get("/applications/<int:application_id>/documents/<document_type>.pdf")
def membership_application_document_pdf(application_id, document_type):
    require_staff_access()
    ensure_runtime_schema()
    application = db.session.get(MembershipApplication, application_id)
    if not application:
        abort(404)
    pdf_bytes = printable_agreement_pdf(document_type, application.language or DEFAULT_LANGUAGE, application=application)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename={secure_filename(document_type)}-{application.id}.pdf"},
    )


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
    language = normalize_language(request.args.get("lang"))
    session["language"] = language
    next_url = safe_local_next_url(request.args.get("next") or request.referrer, url_for("login"))
    return redirect_with_language_cookie(next_url, language)


def optional_dashboard_value(label, fallback, factory):
    try:
        return factory()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Dashboard optional section failed: %s", label)
        return fallback


def member_dashboard_context(member, staff_admin_view=False):
    policy = cancellation_policy_for_member(member)
    language = current_language()
    policy_text = cancellation_message(policy, language=language)
    policy_summary, policy_detail = cancellation_message_parts(policy, language=language)
    payment_status = localized_payment_status(payment_status_for_member(member), language)
    gym_balance = member.balance or 0.0
    account_section = request.args.get("section", "").strip()
    account_section_aliases = {
        "balance": "billing",
        "gym-balance": "billing",
        "membership-billing": "billing",
        "membership": "membership-options",
        "membership-options": "membership-options",
        "agreements-rules": "agreements",
        "agreements": "agreements",
        "documents": "documents",
        "profile": "profile",
        "preferences": "preferences",
        "security": "security",
    }
    account_section = account_section_aliases.get(account_section, account_section)
    cancellation_request = optional_dashboard_value(
        "cancellation_request",
        None,
        lambda: active_cancellation_request_for_member(member),
    )
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
    coach_profile = optional_dashboard_value(
        "coach_profile",
        None,
        lambda: coach_profile_for_member(member),
    )
    coach_completion = coach_profile_completion(coach_profile, member)
    coach_next_session = optional_dashboard_value(
        "coach_next_session",
        None,
        lambda: coach_next_session_context(coach_profile, member.member_id, language=language),
    )
    coach_latest = optional_dashboard_value(
        "coach_latest_workout",
        None,
        lambda: coach_latest_workout(member.member_id),
    )
    nutrition_context = optional_dashboard_value(
        "nutrition_context",
        {"meal_plan": None, "nutrition_next_meal": None},
        lambda: nutrition_plan_context(member, coach_profile, language) if coach_profile else {"meal_plan": None, "nutrition_next_meal": None},
    )
    nutrition_dashboard_plan = nutrition_context.get("meal_plan")
    nutrition_next_meal = nutrition_context.get("nutrition_next_meal")
    coach_counts = (
        optional_dashboard_value("coach_data_counts", {"total": 0}, lambda: coach_data_counts(member.member_id))
        if staff_admin_view else {"total": 0}
    )
    today_group_class_sections = optional_dashboard_value(
        "today_group_classes",
        {"current": [], "earlier": []},
        lambda: member_today_group_class_sections(member.member_id),
    ) if not staff_admin_view else {"current": [], "earlier": []}
    documents = optional_dashboard_value(
        "member_documents",
        [],
        lambda: member_documents(member),
    )
    document_groups = optional_dashboard_value(
        "member_document_groups",
        [],
        lambda: member_document_groups(member),
    )

    return {
        "member": member,
        "display_name": display_member_name(member.name),
        "member_photo_available": member_photo_available,
        "documents": documents,
        "document_groups": document_groups,
        "payment_status": payment_status,
        "gym_balance": gym_balance,
        "account_section": account_section,
        "info": info,
        "sub": sub,
        "pay": pay,
        "cancellation_info": policy_text,
        "cancellation_summary": policy_summary,
        "cancellation_detail": policy_detail,
        "cancellation_policy": policy,
        "current_term_end": fmt_policy_date(policy.current_term_end, language),
        "membership_renewal_status": translated_text("automatic_renewal", language) if is_contract_member_record(member) else translated_text("not_available", language),
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
        "nutrition_dashboard_plan": nutrition_dashboard_plan,
        "nutrition_next_meal": nutrition_next_meal,
        "coach_data_counts": coach_counts,
        "coach_data_total": coach_counts.get("total", 0),
        "today_group_classes": today_group_class_sections["current"],
        "today_earlier_group_classes": today_group_class_sections["earlier"],
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
    staff_warning = try_staff_runtime_schema("staff_settings_nav")

    if request.method == "POST":
        if staff_warning:
            flash(staff_warning)
            return redirect(url_for("staff_settings"))
        validate_csrf_token()
        action = request.form.get("action", "save_settings")
        if action == "send_test_email":
            test_email = request.form.get("test_email", "").strip()
            try:
                mail_status = send_staff_test_email(test_email)
                if mail_status == "logged":
                    flash(translated_text("staff_test_email_logged", current_language()))
                else:
                    flash(translated_text("staff_test_email_sent", current_language(), email=test_email))
            except Exception as exc:
                flash(translated_text("staff_test_email_failed", current_language(), error=exc))
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
                flash(translated_text("staff_new_user_password_required", current_language()))
                return redirect(url_for("staff_settings"))
            if StaffUser.query.filter_by(username=new_username).first():
                flash(translated_text("staff_username_exists", current_language()))
                return redirect(url_for("staff_settings"))
            db.session.add(StaffUser(
                username=new_username,
                role=new_role,
                email=new_email,
                password_hash=generate_password_hash(new_password),
                is_active=True,
            ))

        db.session.commit()
        flash(translated_text("staff_settings_updated", current_language()))
        return redirect(url_for("staff_settings"))

    try:
        settings = {
            key: setting_value(key, value)
            for key, value in DEFAULT_SETTINGS.items()
        }
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Staff settings table unavailable; using configured defaults.")
        settings = dict(DEFAULT_SETTINGS)
        staff_warning = staff_warning or staff_data_warning("staff_settings_nav")
    try:
        staff_users = StaffUser.query.order_by(StaffUser.role.asc(), StaffUser.username.asc()).all()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Staff user table unavailable while rendering settings.")
        staff_users = []
        staff_warning = staff_warning or staff_data_warning("staff_settings_nav")
    return render_template(
        "staff_settings.html",
        settings=settings,
        staff_users=staff_users,
        mail_config=mail_config_status(),
        staff_page_warning=staff_warning,
    )


@app.get("/staff/cancellations")
def staff_cancellations():
    staff_role = require_staff_access()
    admin_status = request.args.get("admin_status", "").strip()
    query, status = cancellation_request_query()
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
    selected_status = request.args.get("status", "").strip()
    review_filter = request.args.get("review", "").strip()
    staff_warning = try_staff_runtime_schema("staff_email_log_nav")
    try:
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
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Email log data unavailable while rendering staff email log.")
        logs = []
        counts = {"open": 0, "failed": 0, "sent": 0, "logged": 0}
        staff_warning = staff_warning or staff_data_warning("staff_email_log_nav")
    return render_template(
        "staff_email_log.html",
        logs=logs,
        counts=counts,
        selected_status=selected_status,
        review_filter=review_filter,
        email_log_requires_review=email_log_requires_review,
        staff_role=staff_role,
        staff_page_warning=staff_warning,
    )


@app.get("/staff/sync")
def staff_sync_status():
    require_staff_access()
    staff_warning = try_staff_runtime_schema("staff_sync_nav")
    if not staff_warning:
        mark_stale_sync_runs()
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    try:
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
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Sync run data unavailable while rendering staff sync status.")
        total_runs = 0
        total_pages = 1
        page = 1
        start_index = 0
        runs = []
        latest = None
        run_changes = {}
        staff_warning = staff_warning or staff_data_warning("staff_sync_nav")
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
        staff_page_warning=staff_warning,
    )


@app.get("/staff/changes")
def staff_daily_changes():
    require_staff_access()
    staff_warning = try_staff_runtime_schema("staff_changes_nav")
    if not staff_warning:
        mark_stale_sync_runs()
    today = local_datetime(datetime.now(timezone.utc)).date()
    raw_date = request.args.get("date", "").strip()
    try:
        selected_date = date.fromisoformat(raw_date) if raw_date else today
    except ValueError:
        selected_date = today
    try:
        changes = daily_sync_changes(selected_date)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Daily sync change data unavailable while rendering staff changes.")
        changes = {"runs": [], "new_members": [], "changed_members": [], "document_changes": []}
        staff_warning = staff_warning or staff_data_warning("staff_changes_nav")
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
        staff_page_warning=staff_warning,
    )


@app.get("/staff/coach")
def staff_coach_activity():
    require_staff_access(required_role="admin")
    staff_warning = try_staff_runtime_schema("staff_coach_nav")
    try:
        interactions = (
            CoachInteraction.query
            .order_by(CoachInteraction.created_at.desc(), CoachInteraction.id.desc())
            .limit(100)
            .all()
        )
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Coach interaction data unavailable while rendering staff coach activity.")
        interactions = []
        staff_warning = staff_warning or staff_data_warning("staff_coach_nav")
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
        staff_page_warning=staff_warning,
    )


@app.get("/staff/group-classes")
def staff_group_classes():
    staff_role = require_staff_access()
    staff_warning = None
    try:
        context = group_class_admin_context()
    except Exception:
        db.session.rollback()
        app.logger.exception("Group class admin data unavailable while rendering staff group classes.")
        staff_warning = staff_data_warning("staff_group_classes_nav")
        context = empty_group_class_admin_context()
    return render_template(
        "staff_group_classes.html",
        staff_role=staff_role,
        staff_page_warning=staff_warning,
        **context,
    )


@app.post("/staff/group-classes/class-types")
def staff_group_class_type_save():
    validate_csrf_token()
    require_staff_access()
    ensure_runtime_schema()

    class_type_id = parse_optional_int(request.form.get("class_type_id"))
    class_type = db.session.get(GroupClassType, class_type_id) if class_type_id else None
    if not class_type:
        class_type = GroupClassType()
        db.session.add(class_type)

    name = request.form.get("name", "").strip().upper()
    category = request.form.get("category", "").strip()
    intensity = request.form.get("intensity", "").strip()
    if not name or not category or not intensity:
        abort(400, translated_text("group_class_required_fields", current_language()))

    class_type.name = name
    class_type.category = category
    class_type.intensity = intensity
    class_type.muscle_focus = request.form.get("muscle_focus", "").strip() or None
    class_type.cardio_load = request.form.get("cardio_load", "").strip() or None
    class_type.strength_load = request.form.get("strength_load", "").strip() or None
    class_type.recovery_impact = request.form.get("recovery_impact", "").strip() or None
    class_type.impact_level = request.form.get("impact_level", "").strip() or None
    class_type.pregnancy_safety_level = request.form.get("pregnancy_safety_level", "").strip() or None
    class_type.default_bookable = request.form.get("default_bookable") == "1"
    class_type.default_publish = request.form.get("default_publish") == "1"
    class_type.description = request.form.get("description", "").strip() or None
    class_type.updated_at = datetime.now()
    db.session.commit()
    flash(translated_text("group_class_type_saved", current_language()))
    return redirect(url_for("staff_group_classes"))


@app.post("/staff/group-classes/occurrences")
def staff_group_class_occurrence_save():
    validate_csrf_token()
    require_staff_access()
    ensure_runtime_schema()

    occurrence_id = parse_optional_int(request.form.get("occurrence_id"))
    occurrence = db.session.get(GroupClassOccurrence, occurrence_id) if occurrence_id else None
    is_new = occurrence is None
    if is_new:
        schedule = GroupClassSchedule.query.filter_by(name=GROUP_CLASS_SCHEDULE_NAME).first()
        if not schedule:
            schedule = seed_group_class_schedule()
        occurrence = GroupClassOccurrence(schedule_id=schedule.id)
        db.session.add(occurrence)

    old_data = group_class_occurrence_snapshot(occurrence) if not is_new else None
    class_type_id = parse_optional_int(request.form.get("class_type_id"))
    class_type = db.session.get(GroupClassType, class_type_id) if class_type_id else None
    if not class_type:
        abort(400, translated_text("group_class_required_fields", current_language()))

    try:
        day_of_week = int(request.form.get("day_of_week", ""))
    except ValueError:
        day_of_week = -1
    if day_of_week not in GROUP_CLASS_DAY_KEYS:
        abort(400, translated_text("group_class_required_fields", current_language()))

    start_time = parse_group_class_time(request.form.get("start_time"))
    end_time = parse_group_class_time(request.form.get("end_time"))
    if end_time <= start_time:
        abort(400, translated_text("group_class_invalid_time_range", current_language()))

    status = request.form.get("status", "scheduled").strip()
    if status not in GROUP_CLASS_OCCURRENCE_STATUSES:
        status = "scheduled"
    if class_type.name == "RESERVED":
        status = "reserved"

    occurrence.class_type = class_type
    occurrence.day_of_week = day_of_week
    occurrence.start_time = start_time
    occurrence.end_time = end_time
    occurrence.room = request.form.get("room", "").strip().upper()
    occurrence.instructor = request.form.get("instructor", "").strip() or None
    occurrence.capacity = parse_optional_int(request.form.get("capacity"))
    occurrence.note = request.form.get("note", "").strip() or None
    occurrence.status = status
    occurrence.blocks_room = True
    occurrence.is_bookable = (
        request.form.get("is_bookable") == "1"
        and status == "scheduled"
        and class_type.name != "RESERVED"
    )
    occurrence.is_published = request.form.get("is_published") == "1"
    occurrence.updated_at = datetime.now()
    if not occurrence.room:
        abort(400, translated_text("group_class_required_fields", current_language()))

    affected_count = 0
    if not is_new:
        new_data = group_class_occurrence_snapshot(occurrence)
        change_type = group_class_change_type(old_data, new_data)
        affected_count = create_group_class_change_notifications(occurrence, change_type, old_data, new_data)

    db.session.commit()
    if affected_count:
        flash(translated_text("group_class_saved_with_members", current_language(), count=affected_count))
    else:
        flash(translated_text("group_class_saved", current_language()))
    return redirect(url_for("staff_group_classes"))


@app.post("/staff/group-classes/publish")
def staff_group_class_publish():
    validate_csrf_token()
    require_staff_access()
    ensure_runtime_schema()
    schedule = GroupClassSchedule.query.filter_by(name=GROUP_CLASS_SCHEDULE_NAME).first()
    if not schedule:
        schedule = seed_group_class_schedule()
    schedule.status = "published"
    schedule.published_at = datetime.now()
    schedule.updated_at = datetime.now()
    db.session.commit()
    flash(translated_text("group_class_schedule_published", current_language()))
    return redirect(url_for("staff_group_classes"))


@app.get("/staff/pricing-products")
def staff_pricing_products():
    staff_role = require_staff_access()
    staff_warning = None
    try:
        context = pricing_admin_context()
    except Exception:
        db.session.rollback()
        app.logger.exception("Pricing admin data unavailable while rendering staff pricing products.")
        staff_warning = staff_data_warning("staff_pricing_nav")
        context = empty_pricing_admin_context()
    return render_template(
        "staff_pricing_products.html",
        staff_role=staff_role,
        staff_page_warning=staff_warning,
        **context,
    )


@app.get("/staff/terms-agreements")
def staff_terms_agreements():
    staff_role = require_staff_access()
    staff_warning = None
    try:
        context = terms_admin_context()
    except Exception:
        db.session.rollback()
        app.logger.exception("Terms admin data unavailable while rendering staff terms agreements.")
        staff_warning = staff_data_warning("staff_terms_nav")
        context = empty_terms_admin_context()
    return render_template(
        "staff_terms_agreements.html",
        staff_role=staff_role,
        staff_page_warning=staff_warning,
        **context,
    )


@app.get("/staff/terms-agreements/templates/<document_type>/<language>/print")
def staff_printable_agreement_template(document_type, language):
    require_staff_access()
    ensure_runtime_schema()
    return render_template("printable_agreement.html", **printable_agreement_context(document_type, language))


@app.get("/staff/terms-agreements/templates/<document_type>/<language>.pdf")
def staff_printable_agreement_pdf(document_type, language):
    require_staff_access()
    ensure_runtime_schema()
    pdf_bytes = printable_agreement_pdf(document_type, language)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename={secure_filename(document_type)}-{normalize_language(language)}.pdf"},
    )


@app.post("/staff/terms-agreements/versions")
def staff_legal_version_save():
    validate_csrf_token()
    require_staff_access(required_role="admin")
    ensure_runtime_schema()

    document_id = parse_optional_int(request.form.get("document_id"))
    document = db.session.get(LegalDocument, document_id) if document_id else None
    if not document:
        document_type = request.form.get("document_type", "").strip()
        title = request.form.get("title", "").strip()
        category_key = request.form.get("category_key", "").strip()
        category = AgreementCategory.query.filter_by(key=category_key).first()
        if not document_type or not title or not category:
            abort(400, translated_text("terms_required_fields", current_language()))
        document = LegalDocument(
            document_type=document_type,
            title=title,
            category_key=category_key,
            active=True,
            required_for=json.dumps([]),
            legal_review_needed=True,
        )
        db.session.add(document)
        db.session.flush()

    version_value = request.form.get("version", "").strip()
    full_legal_text = request.form.get("full_legal_text", "").strip()
    if not version_value or not full_legal_text:
        abort(400, translated_text("terms_required_fields", current_language()))

    make_current = request.form.get("is_current") == "1"
    if make_current:
        LegalDocumentVersion.query.filter_by(document_id=document.id, is_current=True).update({"is_current": False})

    version = LegalDocumentVersion(
        document_id=document.id,
        version=version_value,
        effective_from=parse_optional_date(request.form.get("effective_from")) or date.today(),
        effective_to=parse_optional_date(request.form.get("effective_to")),
        source_language=request.form.get("source_language", DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE,
        full_legal_text=full_legal_text,
        short_summary=request.form.get("short_summary", "").strip(),
        plain_language_summary=request.form.get("plain_language_summary", "").strip(),
        pdf_template_key=request.form.get("pdf_template_key", "").strip() or document.document_type,
        is_current=make_current,
        legal_review_status=request.form.get("legal_review_status", "draft").strip() or "draft",
    )
    db.session.add(version)
    document.legal_review_needed = request.form.get("legal_review_needed") == "1"
    document.updated_at = datetime.now()
    db.session.commit()
    flash(translated_text("terms_version_saved", current_language()))
    return redirect(url_for("staff_terms_agreements"))


@app.post("/staff/terms-agreements/translations")
def staff_legal_translation_save():
    validate_csrf_token()
    require_staff_access(required_role="admin")
    ensure_runtime_schema()

    version_id = parse_optional_int(request.form.get("version_id"))
    version = db.session.get(LegalDocumentVersion, version_id) if version_id else None
    language = normalize_language(request.form.get("language", DEFAULT_LANGUAGE))
    if not version:
        abort(400, translated_text("terms_required_fields", current_language()))
    translation = LegalTranslation.query.filter_by(version_id=version.id, language=language).first()
    if not translation:
        translation = LegalTranslation(version_id=version.id, language=language, title="")
        db.session.add(translation)

    title = request.form.get("title", "").strip()
    full_legal_text = request.form.get("full_legal_text", "").strip()
    if not title or not full_legal_text:
        abort(400, translated_text("terms_required_fields", current_language()))
    translation.title = title
    translation.short_summary = request.form.get("short_summary", "").strip()
    translation.plain_language_summary = request.form.get("plain_language_summary", "").strip()
    translation.full_legal_text = full_legal_text
    translation.translation_status = request.form.get("translation_status", "draft").strip() or "draft"
    translation.updated_at = datetime.now()
    db.session.commit()
    flash(translated_text("terms_translation_saved", current_language()))
    return redirect(url_for("staff_terms_agreements"))


@app.post("/staff/terms-agreements/required-rules")
def staff_required_agreement_rule_save():
    validate_csrf_token()
    require_staff_access(required_role="admin")
    ensure_runtime_schema()

    required_types = [
        line.strip()
        for line in request.form.get("required_legal_document_types", "").splitlines()
        if line.strip()
    ]
    if not required_types:
        abort(400, translated_text("terms_required_fields", current_language()))
    db.session.add(RequiredAgreementRule(
        applies_to_membership_type=request.form.get("applies_to_membership_type", "").strip() or None,
        applies_to_contract_term=request.form.get("applies_to_contract_term", "").strip() or None,
        applies_to_payment_method=request.form.get("applies_to_payment_method", "").strip() or None,
        applies_to_add_on=request.form.get("applies_to_add_on", "").strip() or None,
        applies_to_under18=request.form.get("applies_to_under18") == "1" if "applies_to_under18" in request.form else None,
        required_legal_document_types=json.dumps(required_types),
        active=request.form.get("active", "1") == "1",
    ))
    db.session.commit()
    flash(translated_text("terms_required_rule_saved", current_language()))
    return redirect(url_for("staff_terms_agreements"))


@app.post("/staff/terms-agreements/signed-documents")
def staff_signed_document_save():
    validate_csrf_token()
    require_staff_access()
    ensure_runtime_schema()

    member_id = request.form.get("member_id", "").strip()
    document_type = request.form.get("document_type", "").strip()
    file_url = request.form.get("file_url", "").strip()
    if not member_id or not document_type or not file_url:
        abort(400, translated_text("terms_required_fields", current_language()))
    db.session.add(MemberSignedDocument(
        member_id=member_id,
        application_id=parse_optional_int(request.form.get("application_id")),
        document_type=document_type,
        file_url=file_url,
        storage_reference=file_url,
        pdf_hash=request.form.get("pdf_hash", "").strip() or None,
        signed_at=parse_optional_datetime(request.form.get("signed_at")) or datetime.now(),
        related_contract_id=request.form.get("related_contract_id", "").strip() or None,
        staff_user_id=current_staff_user().id if current_staff_user() else None,
        language=normalize_language(request.form.get("language", DEFAULT_LANGUAGE)),
        version=request.form.get("version", "").strip() or None,
        status=request.form.get("status", "archived").strip() or "archived",
    ))
    db.session.commit()
    flash(translated_text("terms_signed_document_saved", current_language()))
    return redirect(url_for("staff_terms_agreements"))


@app.post("/staff/terms-agreements/applications/<int:application_id>/status")
def staff_membership_application_status_save(application_id):
    validate_csrf_token()
    require_staff_access()
    ensure_runtime_schema()

    application = db.session.get(MembershipApplication, application_id)
    if not application:
        abort(404)
    status = request.form.get("status", "").strip()
    if status not in MEMBERSHIP_APPLICATION_STATUSES:
        abort(400, translated_text("terms_required_fields", current_language()))
    application.status = status
    application.internal_notes = request.form.get("internal_notes", application.internal_notes or "").strip() or application.internal_notes
    application.updated_at = datetime.now()
    if status == "active" and not application.activated_at:
        application.activated_at = datetime.now()
        staff_user = current_staff_user()
        application.activated_by_staff_user_id = staff_user.id if staff_user else None
    if status == "rejected":
        application.rejection_reason = request.form.get("rejection_reason", "").strip() or application.rejection_reason
    db.session.add(MembershipApplicationStatus(
        application_id=application.id,
        status=status,
        note=request.form.get("note", "").strip(),
        changed_by_staff_user_id=current_staff_user().id if current_staff_user() else None,
    ))
    db.session.commit()
    flash(translated_text("terms_application_status_saved", current_language()))
    return redirect(url_for("staff_terms_agreements"))


@app.post("/staff/pricing-products/items")
def staff_pricing_item_save():
    validate_csrf_token()
    require_staff_access()
    ensure_runtime_schema()

    item_id = parse_optional_int(request.form.get("item_id"))
    item = db.session.get(PricingItem, item_id) if item_id else None
    is_new = item is None
    if is_new:
        item = PricingItem(
            seed_key=None,
            source="staff/admin",
            effective_from=date.today(),
        )
        db.session.add(item)

    old_snapshot = pricing_item_snapshot(item) if not is_new else None
    name = request.form.get("name", "").strip()
    category_key = request.form.get("category_key", "").strip()
    category = PricingCategory.query.filter_by(key=category_key).first()
    price_amount = parse_optional_float(request.form.get("price_amount"))
    billing_interval = request.form.get("billing_interval", "").strip()
    visibility = [
        option for option in PRICING_VISIBILITY_OPTIONS
        if request.form.get(f"visibility_{option}") == "1"
    ]
    if not name or not category or price_amount is None or billing_interval not in PRICING_BILLING_INTERVALS:
        abort(400, translated_text("pricing_required_fields", current_language()))
    if not visibility:
        visibility = ["staff_only"]

    item.name = name
    item.category_key = category_key
    item.description = request.form.get("description", "").strip() or None
    item.price_amount = price_amount
    item.currency = (request.form.get("currency", "").strip().upper() or PRICING_CATALOG_CURRENCY)[:8]
    item.billing_interval = billing_interval
    item.duration = request.form.get("duration", "").strip() or None
    item.visibility = pricing_visibility_value(visibility)
    item.is_active = request.form.get("is_active") == "1"
    item.effective_from = parse_optional_date(request.form.get("effective_from"))
    item.effective_to = parse_optional_date(request.form.get("effective_to"))
    item.sort_order = parse_optional_int(request.form.get("sort_order")) or 0
    item.terms = pricing_terms_from_form(request.form.get("terms"))
    item.requires_front_desk_handling = request.form.get("requires_front_desk_handling") == "1"
    item.online_payment_available = request.form.get("online_payment_available") == "1"
    item.member_eligible = request.form.get("member_eligible") == "1"
    item.contract_only = request.form.get("contract_only") == "1"
    item.notes = request.form.get("notes", "").strip() or None
    item.internal_notes = request.form.get("internal_notes", "").strip() or None
    item.updated_at = datetime.now()
    ensure_pricing_business_rules(item)

    db.session.flush()
    new_snapshot = pricing_item_snapshot(item)
    if is_new or old_snapshot != new_snapshot:
        db.session.add(PricingChangeLog(
            pricing_item_id=item.id,
            changed_by=current_staff_username() or "staff",
            change_type="created" if is_new else "updated",
            old_value=json.dumps(old_snapshot, ensure_ascii=True) if old_snapshot else None,
            new_value=json.dumps(new_snapshot, ensure_ascii=True),
            note=request.form.get("change_note", "").strip() or None,
        ))

    db.session.commit()
    flash(translated_text("pricing_item_saved", current_language()))
    return redirect(url_for("staff_pricing_products"))


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
    documents = optional_dashboard_value("staff_member_documents", [], lambda: member_documents(member))
    coach_counts = optional_dashboard_value("staff_member_coach_counts", {"total": 0}, lambda: coach_data_counts(member.member_id))
    next_payment = member.next_payment or compute_next_payment(member)
    return render_template(
        "staff_member_detail.html",
        member=member,
        display_name=display_member_name(member.name),
        member_photo_available=bool(is_s3_uri(member.photo_path) or resolved_photo_path(member.photo_path)),
        documents=documents,
        document_count=len(documents),
        coach_data_total=coach_counts.get("total", 0),
        next_payment=next_payment,
        gym_balance=member.balance or 0.0,
        payment_status=localized_payment_status(payment_status_for_member(member), current_language()),
    )


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
        close_url=url_for("member_account"),
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
            else url_for("member_account")
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


@app.get("/account")
def member_account():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    section = request.args.get("section", "").strip()
    if section:
        section_routes = {
            "balance": "member_account_billing",
            "billing": "member_account_billing",
            "gym-balance": "member_account_billing",
            "membership-billing": "member_account_billing",
            "membership": "member_account_membership",
            "membership-options": "member_account_membership",
            "agreements": "member_agreements",
            "agreements-rules": "member_agreements",
            "documents": "member_account_documents",
            "profile": "member_account_profile",
            "preferences": "member_account_preferences",
            "security": "member_account_security",
        }
        endpoint = section_routes.get(section)
        if endpoint:
            return redirect(url_for(endpoint))

    return render_template("account.html", **member_dashboard_context(member))


def account_detail_context(member, account_page):
    context = member_dashboard_context(member)
    context["account_page"] = account_page
    if account_page == "membership":
        pricing_context = member_pricing_context(member)
        context.update({
            "current_membership": pricing_context["current_membership"],
            "membership_options": pricing_context["membership_options"],
            "addon_options": pricing_context["addon_options"],
            "fee_options": pricing_context["fee_options"],
            "member_pricing_unavailable": pricing_context["member_pricing_unavailable"],
            "pricing_terms_list": pricing_context["pricing_terms_list"],
        })
    return context


@app.get("/account/membership")
def member_account_membership():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return render_template("account_detail.html", **account_detail_context(member, "membership"))


@app.get("/account/billing")
def member_account_billing():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return render_template("account_detail.html", **account_detail_context(member, "billing"))


@app.get("/account/agreements")
def member_agreements():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response

    return render_template("member_agreements.html", **member_agreements_context(member))


@app.get("/account/documents")
def member_account_documents():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return render_template("account_detail.html", **account_detail_context(member, "documents"))


@app.get("/account/profile")
def member_account_profile():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return render_template("account_detail.html", **account_detail_context(member, "profile"))


@app.get("/account/preferences")
def member_account_preferences():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return render_template("account_detail.html", **account_detail_context(member, "preferences"))


@app.get("/account/security")
def member_account_security():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return render_template("account_detail.html", **account_detail_context(member, "security"))


@app.get("/account/signout")
def member_account_signout():
    return redirect(url_for("logout"))


@app.get("/membership-options")
def member_membership_options():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    return redirect(url_for("member_account_membership"))


@app.get("/group-classes")
def member_group_classes():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    try:
        ensure_runtime_schema()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while rendering member group classes.")
    selected_day = request.args.get("day", "").strip()
    selected_type = request.args.get("class_type", "").strip()
    try:
        class_types = (
            GroupClassType.query
            .join(GroupClassOccurrence)
            .filter(GroupClassOccurrence.status == "scheduled")
            .filter(GroupClassOccurrence.is_published.is_(True))
            .filter(GroupClassOccurrence.is_bookable.is_(True))
            .distinct()
            .order_by(GroupClassType.name.asc())
            .all()
        )
        preference = member_group_class_preference(member.member_id)
        rows = member_group_class_schedule_rows(member.member_id, selected_day=selected_day, selected_type=selected_type)
        favorite_ids = member_favorite_class_type_ids(member.member_id)
    except Exception:
        db.session.rollback()
        app.logger.exception("Member group class page fell back to an empty schedule.")
        class_types = []
        preference = None
        rows = []
        favorite_ids = set()
    return render_template(
        "group_classes.html",
        member=member,
        rows=rows,
        class_types=class_types,
        selected_day=selected_day,
        selected_type=selected_type,
        day_options=[(day, group_class_day_label(day)) for day in range(7)],
        preference=preference,
        favorite_ids=favorite_ids,
        time_label=group_class_time_label,
        day_label=group_class_day_label,
    )


@app.get("/nutrition")
def member_nutrition():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    try:
        ensure_runtime_schema()
    except Exception:
        db.session.rollback()
        app.logger.exception("Runtime schema unavailable while rendering member nutrition.")

    context = nutrition_plan_context(member, language=current_language())
    meal_plan = context.get("meal_plan")
    nutrition_missing_items = meal_plan.get("missing_items", []) if meal_plan else nutrition_missing_profile_items(member, context.get("profile"), current_language())
    return render_template(
        "nutrition.html",
        member=member,
        display_name=display_member_name(member.name),
        member_photo_available=bool(is_s3_uri(member.photo_path) or resolved_photo_path(member.photo_path)),
        nutrition_missing_items=nutrition_missing_items,
        nutrition_profile_complete=not nutrition_missing_items,
        **context,
    )


@app.post("/coach/date-of-birth")
def save_coach_date_of_birth():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    return_to = request.form.get("return_to", "").strip()
    if return_to == "coach":
        redirect_target = url_for("member_coach")
    else:
        redirect_target = url_for("member_nutrition") + "#nutrition-plan"
    birthdate = parse_optional_date(request.form.get("birthdate") or request.form.get("date_of_birth"))
    if not birthdate:
        flash(translated_text("date_of_birth_required", current_language()), "error")
        return redirect(redirect_target)

    member.birthdate = birthdate
    CoachPlan.query.filter_by(member_id=member.member_id).delete()
    db.session.commit()
    flash(translated_text("date_of_birth_saved", current_language()), "success")
    return redirect(redirect_target)


@app.post("/nutrition/meal-log")
def save_meal_log():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    log_type = request.form.get("log_type", "followed").strip() or "followed"
    if log_type not in {"followed", "different", "adjust"}:
        log_type = "followed"
    db.session.add(MealLog(
        member_id=member.member_id,
        meal_key=request.form.get("meal_key", "").strip() or "meal",
        meal_title=request.form.get("meal_title", "").strip() or None,
        log_type=log_type,
        food_items=request.form.get("food_items", "").strip() or None,
        portion=request.form.get("portion", "").strip() or None,
        calories=parse_optional_float(request.form.get("calories")),
        protein=parse_optional_float(request.form.get("protein")),
        carbs=parse_optional_float(request.form.get("carbs")),
        fat=parse_optional_float(request.form.get("fat")),
        language=current_language(),
    ))
    db.session.commit()
    flash(translated_text("meal_log_saved", current_language()), "success")
    return redirect(url_for("member_nutrition") + "#nutrition-plan")


@app.post("/nutrition/regenerate")
def regenerate_nutrition_plan():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    CoachPlan.query.filter_by(member_id=member.member_id).delete()
    db.session.commit()
    flash(translated_text("nutrition_plan_regenerated", current_language()), "success")
    return redirect(url_for("member_nutrition") + "#nutrition-plan")


@app.post("/group-classes/preferences")
def member_group_class_preferences_save():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    preferred_count = parse_optional_int(request.form.get("preferred_classes_per_week"))
    plan_mode = request.form.get("plan_mode", "supplement").strip()
    if plan_mode not in {"supplement", "replace_or_supplement"}:
        plan_mode = "supplement"
    preference = member_group_class_preference(member.member_id)
    if not preference:
        preference = MemberClassPreference(member_id=member.member_id, class_type_id=None)
        db.session.add(preference)
    preference.preferred_classes_per_week = preferred_count
    preference.plan_mode = plan_mode
    preference.updated_at = datetime.now()

    favorite_ids = {
        parse_optional_int(value)
        for value in request.form.getlist("favorite_class_type_id")
    }
    favorite_ids.discard(None)
    existing = {
        pref.class_type_id: pref
        for pref in MemberClassPreference.query
        .filter_by(member_id=member.member_id)
        .filter(MemberClassPreference.class_type_id.isnot(None))
        .all()
    }
    for class_type in GroupClassType.query.all():
        pref = existing.get(class_type.id)
        if class_type.id in favorite_ids and not pref:
            pref = MemberClassPreference(member_id=member.member_id, class_type_id=class_type.id)
            db.session.add(pref)
        if pref:
            pref.is_favorite = class_type.id in favorite_ids
            pref.updated_at = datetime.now()
    db.session.commit()
    flash(translated_text("group_class_preferences_saved", current_language()))
    return redirect(url_for("member_group_classes"))


@app.post("/group-classes/plan")
def member_group_class_plan_add():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    occurrence = db.session.get(GroupClassOccurrence, parse_optional_int(request.form.get("occurrence_id")))
    if not occurrence or occurrence.status != "scheduled" or not occurrence.is_published or not occurrence.is_bookable:
        abort(404, translated_text("group_class_not_available", current_language()))
    try:
        class_date = date.fromisoformat(request.form.get("class_date", ""))
    except ValueError:
        abort(400, translated_text("group_class_required_fields", current_language()))
    today = local_datetime(datetime.now(timezone.utc)).date()
    if class_date < today:
        abort(400, translated_text("group_class_past_plan_blocked", current_language()))

    plan = MemberClassPlan.query.filter_by(
        member_id=member.member_id,
        occurrence_id=occurrence.id,
        class_date=class_date,
    ).first()
    if not plan:
        preference = member_group_class_preference(member.member_id)
        plan = MemberClassPlan(
            member_id=member.member_id,
            occurrence_id=occurrence.id,
            class_date=class_date,
            status="planned",
            replaces_personal_workout=bool(preference and preference.plan_mode == "replace_or_supplement"),
            source="member",
        )
        db.session.add(plan)
        db.session.commit()
    flash(translated_text("group_class_added_to_plan", current_language()))
    return redirect(request.referrer or url_for("member_group_classes"))


@app.post("/group-classes/plan/<int:plan_id>/remove")
def member_group_class_plan_remove(plan_id):
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    plan = MemberClassPlan.query.filter_by(id=plan_id, member_id=member.member_id).first_or_404()
    today = local_datetime(datetime.now(timezone.utc)).date()
    if plan.class_date < today or plan.status == "attended":
        abort(400, translated_text("group_class_completed_locked", current_language()))
    db.session.delete(plan)
    db.session.commit()
    flash(translated_text("group_class_removed_from_plan", current_language()))
    return redirect(request.referrer or url_for("member_group_classes"))


@app.post("/group-classes/attendance")
def member_group_class_attendance_save():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    validate_csrf_token()
    plan = MemberClassPlan.query.filter_by(
        id=parse_optional_int(request.form.get("plan_id")),
        member_id=member.member_id,
    ).first()
    occurrence = db.session.get(GroupClassOccurrence, parse_optional_int(request.form.get("occurrence_id")))
    if not occurrence:
        abort(404, translated_text("group_class_not_available", current_language()))
    try:
        class_date = date.fromisoformat(request.form.get("class_date", ""))
    except ValueError:
        abort(400, translated_text("group_class_required_fields", current_language()))
    if not plan:
        plan = MemberClassPlan(
            member_id=member.member_id,
            occurrence_id=occurrence.id,
            class_date=class_date,
            status="attended",
            source="member",
        )
        db.session.add(plan)
        db.session.flush()
    plan.status = "attended"
    plan.updated_at = datetime.now()
    if not MemberClassAttendance.query.filter_by(member_id=member.member_id, occurrence_id=occurrence.id, class_date=class_date).first():
        db.session.add(MemberClassAttendance(
            member_id=member.member_id,
            plan_id=plan.id,
            occurrence_id=occurrence.id,
            class_date=class_date,
            source="member",
        ))
    db.session.commit()
    flash(translated_text("group_class_marked_attended", current_language()))
    return redirect(request.referrer or url_for("member_group_classes"))


@app.get("/progress")
def member_progress():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response
    today = local_datetime(datetime.now(timezone.utc)).date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    return render_template(
        "progress.html",
        member=member,
        display_name=display_member_name(member.name),
        member_photo_available=bool(is_s3_uri(member.photo_path) or resolved_photo_path(member.photo_path)),
        profile=coach_profile_for_member(member),
        workout_history=coach_workout_history(member.member_id),
        progress_entries=coach_progress_history(member.member_id),
        group_class_attended_this_week=member_group_class_attendance_count(member.member_id, week_start, week_end),
        group_class_attended_total=member_group_class_attendance_count(member.member_id),
    )


@app.route("/coach", methods=["GET", "POST"])
def member_coach():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response

    profile = coach_profile_for_member(member)
    if request.method == "POST":
        validate_csrf_token()
        sex = request.form.get("sex", "").strip() or None
        if sex not in COACH_SEX_VALUES:
            sex = None
        raw_pregnancy_status = request.form.get("pregnancy_status", "").strip()
        if sex == "female":
            pregnancy_status = raw_pregnancy_status if raw_pregnancy_status in PREGNANCY_STATUSES else None
        else:
            pregnancy_status = "not_pregnant"
        is_pregnant = sex == "female" and pregnancy_status == "pregnant"
        gestational_weeks = parse_optional_int(request.form.get("gestational_weeks"))
        multiple_pregnancy = request.form.get("multiple_pregnancy", "unknown").strip() or "unknown"
        if multiple_pregnancy not in PREGNANCY_MULTIPLE_VALUES:
            multiple_pregnancy = "unknown"
        provider_cleared_exercise = request.form.get("provider_cleared_exercise", "unknown").strip() or "unknown"
        if provider_cleared_exercise not in PREGNANCY_PROVIDER_CLEARANCE_VALUES:
            provider_cleared_exercise = "unknown"
        pregnancy_consent = request.form.get("pregnancy_consent") == "yes"
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
            "sex": sex,
            "pregnancy_status": pregnancy_status,
            "gestational_weeks": gestational_weeks if is_pregnant else None,
            "expected_due_date": parse_optional_date(request.form.get("expected_due_date")) if is_pregnant else None,
            "pre_pregnancy_weight_kg": parse_optional_float(request.form.get("pre_pregnancy_weight_kg")) if is_pregnant else None,
            "multiple_pregnancy": multiple_pregnancy if is_pregnant else None,
            "provider_cleared_exercise": provider_cleared_exercise if is_pregnant else None,
            "provider_restrictions": (request.form.get("provider_restrictions", "").strip() or None) if is_pregnant else None,
            "pregnancy_consent": pregnancy_consent if is_pregnant else False,
        }
        if is_pregnant:
            if gestational_weeks is None or gestational_weeks < 1 or gestational_weeks > 42 or not pregnancy_consent:
                flash(translated_text("coach_pregnancy_required", current_language()), "error")
                return redirect(url_for("member_coach"))
        if coach_profile_missing_fields(profile_data):
            flash(translated_text("coach_complete_required", current_language()), "error")
            return redirect(url_for("member_coach"))

        if not profile:
            profile = CoachProfile(member_id=member.member_id)
            db.session.add(profile)

        for key, value in profile_data.items():
            setattr(profile, key, value)
        set_pregnancy_symptoms(profile, request.form.getlist("pregnancy_symptoms") if is_pregnant else [])
        profile.updated_at = datetime.now()
        CoachPlan.query.filter_by(member_id=member.member_id).delete()
        db.session.commit()
        flash(translated_text("coach_profile_saved", current_language()))
        if request.form.get("return_to") == "nutrition":
            return redirect(url_for("member_nutrition") + "#nutrition-plan")
        return redirect(url_for("member_coach"))

    personal_plan = coach_plan_for_member(member, profile) if profile else None
    training_calendar = coach_week_calendar(member.member_id, profile, personal_plan) if profile and personal_plan else None
    return render_template(
        "coach.html",
        member=member,
        display_name=display_member_name(member.name),
        profile=profile,
        completion=coach_profile_completion(profile, member),
        starter_guidance=coach_starter_guidance(profile),
        personal_plan=personal_plan,
        coach_goals=COACH_GOALS,
        coach_experience_levels=COACH_EXPERIENCE_LEVELS,
        coach_training_days=COACH_TRAINING_DAYS,
        coach_session_minutes=COACH_SESSION_MINUTES,
        coach_training_places=COACH_TRAINING_PLACES,
        coach_nutrition_goals=COACH_NUTRITION_GOALS,
        coach_sex_values=COACH_SEX_VALUES,
        pregnancy_statuses=PREGNANCY_STATUSES,
        pregnancy_multiple_values=PREGNANCY_MULTIPLE_VALUES,
        pregnancy_provider_clearance_values=PREGNANCY_PROVIDER_CLEARANCE_VALUES,
        pregnancy_warning_symptoms=PREGNANCY_WARNING_SYMPTOMS,
        selected_pregnancy_symptoms=pregnancy_symptom_list(profile),
        pregnancy_safety_status=pregnancy_safety_status(profile),
        pregnancy_trimester=pregnancy_trimester(profile),
        coach_label=coach_label,
        workout_history=coach_workout_history(member.member_id),
        progress_entries=coach_progress_history(member.member_id),
        training_calendar=training_calendar,
        planned_group_classes=member_planned_group_classes(member.member_id),
        coach_interactions=coach_recent_interactions(member.member_id),
        coach_next_session=coach_next_session_context(profile, member.member_id),
        focus_field=request.args.get("field", "").strip(),
        return_to=request.args.get("return_to", "").strip(),
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
        return redirect(url_for("member_account_membership"))

    if existing_request:
        flash(translated_text("cancel_already_reviewing", current_language()))
        return redirect(url_for("member_account_membership"))

    if not reason:
        flash(translated_text("cancel_choose_reason", current_language()))
        return redirect(url_for("member_account_membership"))

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
        return redirect(url_for("member_account_membership"))

    request_record = create_cancellation_request(
        member,
        policy,
        reason,
        status="accepted",
        mail_status="pending",
        language=current_language(),
    )
    db.session.flush()
    create_cancellation_confirmation_record(request_record, language=current_language())
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
    return redirect(url_for("member_account_membership"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        validate_csrf_token()
        step = request.form.get("step", "email")

        if step == "password":
            email = normalize_email(request.form.get("email"))
            password = request.form.get("password", "")
            member = member_by_email(email)
            if member and member.password_hash and check_password_hash(member.password_hash, password):
                start_member_session(member, password_verified=True)
                return redirect(url_for("dashboard", id=member.member_id))
            flash(translated_text("member_password_login_failed", current_language()), "error")
            return redirect(url_for("login"))

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
            start_member_session(member, password_verified=True)
            if not member.password_hash:
                flash(translated_text("member_password_required_after_code", current_language()), "success")
                return redirect(url_for("set_member_password"))
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


@app.route("/set-password", methods=["GET", "POST"])
def set_member_password():
    member, redirect_response = current_member_or_redirect()
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        validate_csrf_token()
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirm", "")
        error_key = validate_member_password(password, confirmation)
        if error_key:
            flash(translated_text(error_key, current_language()), "error")
            return redirect(url_for("set_member_password"))
        member.password_hash = generate_password_hash(password)
        member.password_set_at = datetime.now()
        db.session.commit()
        session["member_password_verified"] = True
        flash(translated_text("member_password_saved", current_language()), "success")
        return redirect(url_for("dashboard", id=member.member_id))

    return render_template(
        "set_password.html",
        member=member,
        is_update=bool(member.password_hash),
        min_length=app.config["MEMBER_PASSWORD_MIN_LENGTH"],
    )


@app.get("/member")
def member_login_alias():
    return redirect(url_for("login"))

@app.get("/logout")
def logout():
    session.pop("member_id", None)
    session.pop("member_password_verified", None)
    flash(translated_text("logged_out", current_language()))
    return redirect(url_for("login"))
