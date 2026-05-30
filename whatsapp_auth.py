import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


WHATSAPP_LOGIN_FLAG = "WHATSAPP_LOGIN_ENABLED"
DEFAULT_COUNTRY_CODE = "599"
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def whatsapp_login_enabled(config=None):
    value = None
    if config is not None:
        value = config.get(WHATSAPP_LOGIN_FLAG)
    if value is None:
        value = os.getenv(WHATSAPP_LOGIN_FLAG, "false")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_phone_number(raw_value, default_country_code=DEFAULT_COUNTRY_CODE):
    raw = (raw_value or "").strip()
    if not raw:
        return ""

    has_plus = raw.startswith("+")
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""

    country_code = re.sub(r"\D+", "", str(default_country_code or DEFAULT_COUNTRY_CODE)) or DEFAULT_COUNTRY_CODE
    if has_plus:
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith(country_code):
        return f"+{digits}"
    return f"+{country_code}{digits}"


def phone_digits(value):
    return re.sub(r"\D+", "", value or "")


def generate_otp_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def otp_hash(code, secret):
    payload = str(code or "").strip().encode("utf-8")
    key = str(secret or "").encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_otp_code(code, expected_hash, secret):
    if not code or not expected_hash:
        return False
    supplied_hash = otp_hash(code, secret)
    return hmac.compare_digest(supplied_hash, expected_hash)


def otp_expires_at(now=None, ttl_minutes=OTP_TTL_MINUTES):
    return (now or datetime.now()) + timedelta(minutes=ttl_minutes)


def token_fingerprint(token):
    if not token:
        return ""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


@dataclass
class WhatsAppSendResult:
    status: str
    provider_message_id: str = ""
    error: str = ""


class WhatsAppCloudApiClient:
    def __init__(
        self,
        access_token="",
        phone_number_id="",
        api_version="v19.0",
        template_name="",
        template_language="en_US",
        timeout_seconds=10,
    ):
        self.access_token = access_token or ""
        self.phone_number_id = phone_number_id or ""
        self.api_version = api_version or "v19.0"
        self.template_name = template_name or ""
        self.template_language = template_language or "en_US"
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self):
        return bool(self.access_token and self.phone_number_id and self.template_name)

    def send_otp(self, phone_e164, code, language="en"):
        if not self.configured:
            return WhatsAppSendResult(status="not_configured")

        to_number = phone_digits(phone_e164)
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": code}],
                    },
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": code}],
                    },
                ],
            },
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            return WhatsAppSendResult(status="failed", error=error_body or str(exc))
        except (URLError, TimeoutError, ValueError) as exc:
            return WhatsAppSendResult(status="failed", error=str(exc))

        messages = body.get("messages") or []
        provider_id = messages[0].get("id", "") if messages else ""
        return WhatsAppSendResult(status="sent", provider_message_id=provider_id)
