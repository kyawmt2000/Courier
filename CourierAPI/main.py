import base64
import hashlib
import json
import math
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from cryptography.hazmat.primitives import padding as symmetric_padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, ConfigDict, Field

try:
    from google.cloud import storage
    from google.oauth2 import id_token as google_id_token
    from google.oauth2 import service_account
    from google.auth.transport import requests as google_auth_requests
except ImportError:
    storage = None
    google_id_token = None
    service_account = None
    google_auth_requests = None


app = FastAPI(title="Courier API", version="1.0.0")
CURRENT_TERMS_VERSION = "2026-07-20"
RIDER_DEPOSIT_CONFIRM_WINDOW = timedelta(minutes=5)
DELIVERY_PROMOTION_ENABLED = os.getenv("DELIVERY_PROMOTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
DELIVERY_PROMOTION_FEE_MMK = float(os.getenv("DELIVERY_PROMOTION_FEE_MMK", "1000") or 1000)
DELIVERY_PROMOTION_START_AT = os.getenv("DELIVERY_PROMOTION_START_AT", "").strip()
DELIVERY_PROMOTION_END_AT = os.getenv("DELIVERY_PROMOTION_END_AT", "2026-08-31T23:59:59+06:30").strip()
PLATFORM_KPAY_QR_IMAGE_URL = os.getenv(
    "PLATFORM_KPAY_QR_IMAGE_URL",
    "https://storage.googleapis.com/courierblink/platform-pay-qr.jpg",
).strip()
PLATFORM_KPAY_ACCOUNT_NAME = os.getenv("PLATFORM_KPAY_ACCOUNT_NAME", "Blink").strip()
PLATFORM_KPAY_ACCOUNT_NOTE = os.getenv("PLATFORM_KPAY_ACCOUNT_NOTE", "KPay Payment QR").strip()
MAX_GOODS_AMOUNT_MMK = float(os.getenv("MAX_GOODS_AMOUNT_MMK", "200000") or 200000)
ANDROID_USER_LATEST_VERSION_CODE = int(os.getenv("ANDROID_USER_LATEST_VERSION_CODE", "1") or 1)
ANDROID_USER_LATEST_VERSION_NAME = os.getenv("ANDROID_USER_LATEST_VERSION_NAME", "1.0").strip()
ANDROID_USER_APK_URL = os.getenv("ANDROID_USER_APK_URL", "").strip()
ANDROID_USER_FORCE_UPDATE = os.getenv("ANDROID_USER_FORCE_UPDATE", "false").lower() in {"1", "true", "yes", "on"}
ANDROID_RIDER_LATEST_VERSION_CODE = int(os.getenv("ANDROID_RIDER_LATEST_VERSION_CODE", "1") or 1)
ANDROID_RIDER_LATEST_VERSION_NAME = os.getenv("ANDROID_RIDER_LATEST_VERSION_NAME", "1.0").strip()
ANDROID_RIDER_APK_URL = os.getenv("ANDROID_RIDER_APK_URL", "").strip()
ANDROID_RIDER_FORCE_UPDATE = os.getenv("ANDROID_RIDER_FORCE_UPDATE", "false").lower() in {"1", "true", "yes", "on"}
IOS_USER_LATEST_BUILD_NUMBER = int(os.getenv("IOS_USER_LATEST_BUILD_NUMBER", "1") or 1)
IOS_USER_LATEST_VERSION_NAME = os.getenv("IOS_USER_LATEST_VERSION_NAME", "1.0").strip()
IOS_USER_APP_STORE_URL = os.getenv("IOS_USER_APP_STORE_URL", "").strip()
IOS_USER_FORCE_UPDATE = os.getenv("IOS_USER_FORCE_UPDATE", "false").lower() in {"1", "true", "yes", "on"}
IOS_RIDER_LATEST_BUILD_NUMBER = int(os.getenv("IOS_RIDER_LATEST_BUILD_NUMBER", "1") or 1)
IOS_RIDER_LATEST_VERSION_NAME = os.getenv("IOS_RIDER_LATEST_VERSION_NAME", "1.0").strip()
IOS_RIDER_APP_STORE_URL = os.getenv("IOS_RIDER_APP_STORE_URL", "").strip()
IOS_RIDER_FORCE_UPDATE = os.getenv("IOS_RIDER_FORCE_UPDATE", "false").lower() in {"1", "true", "yes", "on"}
logger = logging.getLogger("courier-api")
ADMIN_CHAT_SENDER_NAME = "Customer Service"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


OrderStatus = Literal[
    "matching",
    "accepted",
    "picking_up",
    "delivering",
    "completed",
    "cancelled",
]

ChatSenderType = Literal["user", "rider", "admin"]
PaymentMode = Literal["cod", "prepaid"]
PaymentStatus = Literal["not_required", "unpaid", "pending", "confirmed", "rejected"]
SettlementStatus = Literal["pending", "paid_to_user", "paid_to_rider", "completed"]


class HealthResponse(BaseModel):
    status: str
    service: str
    timestamp: datetime


class EmptyResponse(BaseModel):
    ok: bool = True


class PlatformPaymentConfigResponse(BaseModel):
    kpay_qr_image_url: str | None = None
    kpay_account_name: str | None = None
    kpay_account_note: str | None = None
    max_goods_amount_mmk: float = 200000


class AppUpdateConfigResponse(BaseModel):
    latest_version_code: int = 1
    latest_version_name: str = "1.0"
    download_url: str | None = None
    force_update: bool = False
    message: str | None = None


class UserProfile(BaseModel):
    id: str
    phone: str
    email: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    payment_qr_url: str | None = None
    terms_accepted_at: str | None = None
    terms_version: str | None = None


class LoginRequest(BaseModel):
    phone: str = Field(min_length=6)
    code: str = Field(min_length=4)


class OAuthLoginRequest(BaseModel):
    provider: Literal["apple", "google"]
    id_token: str | None = None
    email: str | None = None
    name: str | None = None
    subject: str | None = None


class LoginResponse(BaseModel):
    token: str
    user: UserProfile


class UpdateProfileRequest(BaseModel):
    nickname: str | None = None
    avatar_url: str | None = None
    payment_qr_url: str | None = None


class SendSMSCodeRequest(BaseModel):
    phone: str = Field(min_length=6)


class SendSMSCodeResponse(BaseModel):
    phone: str
    expires_at: datetime


class CreateOrderRequest(BaseModel):
    pickup_address: str
    dropoff_address: str
    parcel_type: str
    weight_kg: float = Field(gt=0)
    note: str = ""
    distance_km: float = Field(gt=0)
    payment_mode: PaymentMode = "cod"
    goods_amount: float = Field(default=0, ge=0)
    goods_image_url: str | None = None
    kpay_transaction_id: str | None = None
    payment_proof_url: str | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None


class DeliveryPromotionResponse(BaseModel):
    active: bool
    text: str | None = None
    discount_fee: float | None = None
    original_fee: float | None = None
    payable_fee: float | None = None
    requires_invite_email: bool = False
    eligible: bool = False
    invite_email: str | None = None
    message: str | None = None


class CreatePrepaidPaymentRequest(BaseModel):
    amount: float = Field(gt=0)
    distance_km: float = Field(gt=0)
    goods_amount: float = Field(default=0, ge=0)
    payment_proof_url: str
    payment_mode: PaymentMode = "cod"
    promo_invite_email: str | None = None


class CreateDingerPaymentRequest(BaseModel):
    amount: float = Field(gt=0)
    distance_km: float = Field(gt=0)
    payment_mode: PaymentMode = "cod"
    provider_name: str = "KBZPAY"
    method_name: str = "QR"
    customer_name: str | None = None


class DingerCallbackRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payment_result: str = Field(alias="paymentResult")
    checksum: str | None = Field(default=None, alias="checkSum")


class PrepaidPaymentResponse(BaseModel):
    id: str
    user_phone: str
    amount: float
    distance_km: float
    goods_amount: float = 0
    payment_mode: PaymentMode = "cod"
    status: PaymentStatus = "pending"
    created_at: datetime
    confirmed_at: datetime | None = None
    payment_proof_url: str | None = None
    dinger_transaction_num: str | None = None
    dinger_form_token: str | None = None
    dinger_qr_code: str | None = None
    dinger_provider_name: str | None = None
    dinger_method_name: str | None = None
    original_delivery_fee: float | None = None
    promotion_applied: bool = False
    promo_invite_email: str | None = None


class OrderResponse(BaseModel):
    id: str
    pickup_address: str
    dropoff_address: str
    parcel_type: str
    weight_kg: float
    note: str
    distance_km: float
    price: float
    delivery_fee: float
    platform_delivery_fee: float = 0
    rider_delivery_fee: float = 0
    payment_mode: PaymentMode = "cod"
    goods_amount: float = 0
    goods_image_url: str | None = None
    user_payment_status: PaymentStatus = "not_required"
    rider_deposit_status: PaymentStatus = "unpaid"
    rider_deposit_due_at: datetime | None = None
    rider_deposit_submitted_at: datetime | None = None
    settlement_status: SettlementStatus = "pending"
    kpay_transaction_id: str | None = None
    payment_proof_url: str | None = None
    status: OrderStatus
    rider_name: str | None = None
    created_at: datetime
    accepted_at: datetime | None = None
    pickup_lat: float | None = None
    pickup_lng: float | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None
    rider_lat: float | None = None
    rider_lng: float | None = None
    rider_location_updated_at: datetime | None = None
    rider_settlement_name: str | None = None
    rider_settlement_phone: str | None = None
    rider_settlement_qr_url: str | None = None
    rider_settlement_requested_at: datetime | None = None
    rider_settlement_paid_at: datetime | None = None
    rider_settlement_bill_title: str | None = None
    rider_settlement_bill_message: str | None = None
    rider_settlement_bill_amount: float | None = None
    rider_settlement_bill_created_at: datetime | None = None
    user_settlement_name: str | None = None
    user_settlement_qr_url: str | None = None
    user_settlement_requested_at: datetime | None = None
    user_settlement_paid_at: datetime | None = None
    user_settlement_bill_title: str | None = None
    user_settlement_bill_message: str | None = None
    user_settlement_bill_amount: float | None = None
    user_settlement_bill_created_at: datetime | None = None
    original_delivery_fee: float | None = None
    promotion_applied: bool = False
    promo_invite_email: str | None = None
    cancellation_actor: str | None = None
    cancellation_reason: str | None = None
    cancellation_compensation_amount: float | None = None
    cancelled_at: datetime | None = None


class SignedUploadRequest(BaseModel):
    file_name: str
    content_type: str
    folder: str = "uploads"


class SignedUploadResponse(BaseModel):
    upload_url: str
    public_url: str


class DistanceEstimateRequest(BaseModel):
    pickup_location: str
    dropoff_location: str


class DistanceEstimateResponse(BaseModel):
    distance_km: float
    price: float
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    route_polyline: str | None = None


class AcceptOrderRequest(BaseModel):
    rider_name: str


class UpdateOrderStatusRequest(BaseModel):
    status: OrderStatus


class UpdateRiderLocationRequest(BaseModel):
    lat: float
    lng: float


class RiderSettlementRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    qr_url: str


class UserSettlementRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    qr_url: str


class CreateChatMessageRequest(BaseModel):
    text: str = Field(default="", max_length=1000)
    sender_type: ChatSenderType
    sender_name: str
    sender_phone: str | None = None
    conversation_id: str = "main"
    image_url: str | None = None


class AdminChatReplyRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=120)
    text: str = Field(default="", max_length=1000)
    image_url: str | None = None
    image_data: str | None = None
    image_content_type: str | None = None
    image_file_name: str | None = None


class ChatMessageResponse(BaseModel):
    id: str
    conversation_id: str
    text: str
    sender_type: ChatSenderType
    sender_name: str
    sender_phone: str | None = None
    image_url: str | None = None
    created_at: datetime


class AdminUpdateOrderRequest(BaseModel):
    status: OrderStatus | None = None
    user_payment_status: PaymentStatus | None = None
    rider_deposit_status: PaymentStatus | None = None
    settlement_status: SettlementStatus | None = None


class AdminUpdatePrepaidPaymentRequest(BaseModel):
    status: PaymentStatus | None = None


sms_codes: dict[str, tuple[str, datetime]] = {}


def test_login_phone() -> str:
    return normalize_myanmar_phone(os.getenv("TEST_LOGIN_PHONE", "+959777777777"))


def test_login_code() -> str:
    return os.getenv("TEST_LOGIN_CODE", "000000").strip()


def is_test_login(phone: str, code: str | None = None) -> bool:
    if phone != test_login_phone():
        return False
    return code is None or code == test_login_code()


def allow_unverified_oauth_login() -> bool:
    return os.getenv("ALLOW_UNVERIFIED_OAUTH_LOGIN", "false").strip().lower() in {"1", "true", "yes"}


def oauth_audiences(env_name: str) -> list[str]:
    return [
        item.strip()
        for item in os.getenv(env_name, "").split(",")
        if item.strip()
    ]


def resolve_db_path() -> Path:
    configured = (os.getenv("COURIER_DB_PATH") or os.getenv("CHAT_DB_PATH") or "").strip()
    if configured:
        return Path(configured)

    render_disk = Path("/var/data")
    if render_disk.exists():
        return render_disk / "courier_data.sqlite3"

    return Path("courier_data.sqlite3")


db_path = resolve_db_path()


def connect_db() -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def add_column_if_missing(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    if column_name not in table_columns(connection, table_name):
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_storage() -> None:
    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                text TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                sender_phone TEXT,
                image_url TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created "
            "ON chat_messages (conversation_id, created_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_phone TEXT NOT NULL,
                rider_phone TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user_created "
            "ON orders (user_phone, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_rider_status_created "
            "ON orders (rider_phone, status, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status_created "
            "ON orders (status, created_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                phone TEXT PRIMARY KEY,
                email TEXT,
                nickname TEXT,
                avatar_url TEXT,
                payment_qr_url TEXT,
                terms_accepted_at TEXT,
                terms_version TEXT,
                app_role TEXT,
                app_role_updated_at TEXT,
                app_deleted_at TEXT,
                app_data_hidden_before TEXT,
                last_login_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS prepaid_payments (
                id TEXT PRIMARY KEY,
                user_phone TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepaid_payments_user_created "
            "ON prepaid_payments (user_phone, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_prepaid_payments_status_created "
            "ON prepaid_payments (status, created_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_promotion_redemptions (
                id TEXT PRIMARY KEY,
                user_phone TEXT NOT NULL,
                invitee_email TEXT,
                payment_id TEXT,
                order_id TEXT,
                original_delivery_fee REAL NOT NULL,
                discounted_delivery_fee REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_promo_user_created "
            "ON delivery_promotion_redemptions (user_phone, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_promo_invitee "
            "ON delivery_promotion_redemptions (invitee_email)"
        )
        add_column_if_missing(connection, "chat_messages", "conversation_id", "TEXT NOT NULL DEFAULT 'main'")
        add_column_if_missing(connection, "chat_messages", "sender_phone", "TEXT")
        add_column_if_missing(connection, "chat_messages", "image_url", "TEXT")
        add_column_if_missing(connection, "orders", "user_phone", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "orders", "rider_phone", "TEXT")
        add_column_if_missing(connection, "orders", "status", "TEXT NOT NULL DEFAULT 'matching'")
        add_column_if_missing(connection, "orders", "created_at", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "orders", "payload", "TEXT NOT NULL DEFAULT '{}'")
        add_column_if_missing(connection, "accounts", "nickname", "TEXT")
        add_column_if_missing(connection, "accounts", "email", "TEXT")
        add_column_if_missing(connection, "accounts", "avatar_url", "TEXT")
        add_column_if_missing(connection, "accounts", "payment_qr_url", "TEXT")
        add_column_if_missing(connection, "accounts", "terms_accepted_at", "TEXT")
        add_column_if_missing(connection, "accounts", "terms_version", "TEXT")
        add_column_if_missing(connection, "accounts", "app_role", "TEXT")
        add_column_if_missing(connection, "accounts", "app_role_updated_at", "TEXT")
        add_column_if_missing(connection, "accounts", "app_deleted_at", "TEXT")
        add_column_if_missing(connection, "accounts", "app_data_hidden_before", "TEXT")
        add_column_if_missing(connection, "accounts", "last_login_at", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "prepaid_payments", "user_phone", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "prepaid_payments", "status", "TEXT NOT NULL DEFAULT 'pending'")
        add_column_if_missing(connection, "prepaid_payments", "created_at", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "prepaid_payments", "payload", "TEXT NOT NULL DEFAULT '{}'")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "user_phone", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "invitee_email", "TEXT")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "payment_id", "TEXT")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "order_id", "TEXT")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "original_delivery_fee", "REAL NOT NULL DEFAULT 0")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "discounted_delivery_fee", "REAL NOT NULL DEFAULT 0")
        add_column_if_missing(connection, "delivery_promotion_redemptions", "created_at", "TEXT NOT NULL DEFAULT ''")


init_storage()


def normalize_myanmar_phone(phone: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if cleaned.startswith("+95"):
        local = cleaned[3:]
    elif cleaned.startswith("95"):
        local = cleaned[2:]
    elif cleaned.startswith("09"):
        local = cleaned[1:]
    elif cleaned.startswith("9"):
        local = cleaned
    else:
        raise HTTPException(status_code=400, detail="请输入缅甸手机号，格式如 09xxxxxxx 或 +959xxxxxxx")

    if not re.fullmatch(r"9\d{7,10}", local):
        raise HTTPException(status_code=400, detail="缅甸手机号格式不正确，请检查号码")

    return f"+95{local}"


def oauth_account_id(provider: str, subject: str) -> str:
    normalized_provider = provider.strip().lower()
    normalized_subject = subject.strip().lower()
    if normalized_provider not in {"apple", "google"} or not normalized_subject:
        raise HTTPException(status_code=400, detail="第三方登录资料不完整")
    digest = hashlib.sha256(f"{normalized_provider}:{normalized_subject}".encode("utf-8")).hexdigest()[:24]
    return f"oauth:{normalized_provider}:{digest}"


def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        raise HTTPException(status_code=401, detail="第三方登录凭证无效")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="第三方登录凭证无效")


def verified_google_payload(token: str) -> dict:
    audiences = oauth_audiences("GOOGLE_CLIENT_IDS") or oauth_audiences("GOOGLE_CLIENT_ID")
    if google_id_token is None or google_auth_requests is None:
        if audiences:
            raise HTTPException(status_code=500, detail="服务器未安装 Google 登录验证依赖")
        return decode_jwt_payload(token)

    if not audiences:
        return decode_jwt_payload(token)

    last_error: Exception | None = None
    for audience in audiences:
        try:
            return google_id_token.verify_oauth2_token(
                token,
                google_auth_requests.Request(),
                audience,
            )
        except ValueError as error:
            last_error = error

    logger.warning("Google ID token verification failed: %s", last_error)
    raise HTTPException(status_code=401, detail="Gmail 登录凭证无效")


def require_oauth_identity(request: OAuthLoginRequest) -> tuple[str, str | None, str | None]:
    if request.id_token:
        payload = verified_google_payload(request.id_token) if request.provider == "google" else decode_jwt_payload(request.id_token)
        subject = clean_optional_text(str(payload.get("sub") or ""))
        if not subject:
            raise HTTPException(status_code=401, detail="第三方登录凭证缺少账号 ID")

        issuer = clean_optional_text(str(payload.get("iss") or ""))
        if request.provider == "apple" and issuer != "https://appleid.apple.com":
            raise HTTPException(status_code=401, detail="Apple 登录凭证无效")
        if request.provider == "google" and issuer not in {"https://accounts.google.com", "accounts.google.com"}:
            raise HTTPException(status_code=401, detail="Gmail 登录凭证无效")

        expected_audiences = oauth_audiences("APPLE_BUNDLE_IDS") or oauth_audiences("APPLE_BUNDLE_ID")
        if request.provider == "google":
            expected_audiences = oauth_audiences("GOOGLE_CLIENT_IDS") or oauth_audiences("GOOGLE_CLIENT_ID")
        token_audience = payload.get("aud")
        token_audiences = token_audience if isinstance(token_audience, list) else [token_audience]
        if expected_audiences and not any(audience in token_audiences for audience in expected_audiences):
            raise HTTPException(status_code=401, detail="第三方登录凭证不属于 Blink")

        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and datetime.fromtimestamp(exp, timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="第三方登录凭证已过期")

        email = clean_optional_text(str(payload.get("email") or "")) or clean_optional_text(request.email)
        return subject, email, clean_optional_text(request.name)

    if allow_unverified_oauth_login():
        subject = clean_optional_text(request.subject) or clean_optional_text(request.email)
        if subject:
            return subject, clean_optional_text(request.email), clean_optional_text(request.name)

    raise HTTPException(status_code=400, detail="请提供 Apple 或 Gmail 登录凭证")


def create_sms_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def send_sms_code(phone: str, code: str) -> None:
    message = f"Your Courier verification code is {code}. It expires in 5 minutes."

    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_from = os.getenv("TWILIO_FROM_NUMBER")
    twilio_messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID")

    if twilio_sid and twilio_token and (twilio_from or twilio_messaging_service_sid):
        data = {
            "To": phone,
            "Body": message,
        }
        if twilio_messaging_service_sid:
            data["MessagingServiceSid"] = twilio_messaging_service_sid
        else:
            data["From"] = twilio_from

        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
                data=data,
                auth=(twilio_sid, twilio_token),
            )

        if response.status_code >= 400:
            logger.warning("Twilio SMS send failed: status=%s body=%s", response.status_code, response.text)
            raise HTTPException(status_code=502, detail="短信发送失败，请稍后再试")

        try:
            payload = response.json()
            logger.warning(
                "Twilio SMS accepted: sid=%s status=%s to=%s",
                payload.get("sid"),
                payload.get("status"),
                phone,
            )
        except ValueError:
            logger.warning("Twilio SMS accepted: status=%s to=%s", response.status_code, phone)
        return

    gateway_url = os.getenv("SMS_GATEWAY_URL")
    if gateway_url:
        headers = {"Content-Type": "application/json"}
        gateway_token = os.getenv("SMS_GATEWAY_TOKEN")
        if gateway_token:
            headers["Authorization"] = f"Bearer {gateway_token}"

        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                gateway_url,
                headers=headers,
                json={
                    "to": phone,
                    "message": message,
                    "code": code,
                },
            )

        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="短信发送失败，请稍后再试")
        return

    raise HTTPException(status_code=500, detail="短信服务未配置，请先配置真实短信网关")


def estimate_price(distance_km: float, weight_kg: float) -> float:
    return round(distance_km * 1000, 2)


def parse_config_datetime(value: str) -> datetime | None:
    cleaned = clean_optional_text(value)
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Invalid delivery promotion datetime: %s", cleaned)
        return None
    return utc_datetime(parsed)


def delivery_promotion_is_active(now: datetime | None = None) -> bool:
    if not DELIVERY_PROMOTION_ENABLED or DELIVERY_PROMOTION_FEE_MMK <= 0:
        return False
    current = utc_datetime(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    start_at = parse_config_datetime(DELIVERY_PROMOTION_START_AT)
    end_at = parse_config_datetime(DELIVERY_PROMOTION_END_AT)
    if start_at and current < start_at:
        return False
    if end_at and current > end_at:
        return False
    return True


def normalize_email(value: str | None) -> str | None:
    email = clean_optional_text(value)
    if not email:
        return None
    return email.lower()


def delivery_promotion_text() -> str:
    return f"优惠期间送货费 = {DELIVERY_PROMOTION_FEE_MMK:.0f} MMK"


def delivery_promotion_redemption_count(user_phone: str) -> int:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM delivery_promotion_redemptions WHERE user_phone = ?",
            (user_phone,),
        ).fetchone()
    return int(row["count"] if row else 0)


def delivery_promotion_invitee_email_used(email: str) -> bool:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM delivery_promotion_redemptions
            WHERE lower(invitee_email) = ?
            LIMIT 1
            """,
            (email,),
        ).fetchone()
    return row is not None


def delivery_promotion_invitee_has_completed_settled_order(email: str) -> bool:
    # Friend email is valid only when it belongs to the ordering user of a
    # completed order with backend settlement activity. Rider emails do not
    # qualify. Some order types settle only one side, so paid_to_user and
    # paid_to_rider are also successful settled states here.
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT orders.payload
            FROM accounts
            JOIN orders ON orders.user_phone = accounts.phone
            WHERE lower(trim(accounts.email)) = ?
            """,
            (email,),
        ).fetchall()
    for row in rows:
        try:
            order = order_from_row(row)
        except Exception:
            continue
        if order.status == "completed" and order.settlement_status in ("paid_to_user", "paid_to_rider", "completed"):
            return True
    return False


def validate_delivery_promotion_invite_email(user_phone: str, invite_email: str | None) -> str | None:
    if delivery_promotion_redemption_count(user_phone) == 0:
        return None
    email = normalize_email(invite_email)
    if not email:
        raise HTTPException(status_code=400, detail="请填写已完成订单好友的邮箱，才能继续享受优惠")
    own_profile = load_account_profile(user_phone)
    if normalize_email(own_profile.email if own_profile else None) == email:
        raise HTTPException(status_code=400, detail="不能填写自己的邮箱")
    if delivery_promotion_invitee_email_used(email):
        raise HTTPException(status_code=400, detail="这个好友邮箱已经使用过，不能重复使用")
    if not delivery_promotion_invitee_has_completed_settled_order(email):
        raise HTTPException(status_code=400, detail="这个邮箱还没有完成并结算成功的订单")
    return email


def delivery_promotion_quote(user_phone: str, distance_km: float, invite_email: str | None = None) -> DeliveryPromotionResponse:
    original_fee = estimate_price(distance_km, 1)
    if not delivery_promotion_is_active():
        return DeliveryPromotionResponse(
            active=False,
            original_fee=original_fee,
            payable_fee=original_fee,
            message="优惠未开启",
        )

    requires_invite = delivery_promotion_redemption_count(user_phone) > 0
    normalized_invite_email = None
    eligible = not requires_invite
    message = None
    if requires_invite:
        try:
            normalized_invite_email = validate_delivery_promotion_invite_email(user_phone, invite_email)
            eligible = True
        except HTTPException as exc:
            message = str(exc.detail)

    return DeliveryPromotionResponse(
        active=True,
        text=delivery_promotion_text(),
        discount_fee=round(DELIVERY_PROMOTION_FEE_MMK, 2),
        original_fee=original_fee,
        payable_fee=round(DELIVERY_PROMOTION_FEE_MMK if eligible else original_fee, 2),
        requires_invite_email=requires_invite,
        eligible=eligible,
        invite_email=normalized_invite_email,
        message=message,
    )


def save_delivery_promotion_redemption(
    user_phone: str,
    payment_id: str,
    order_id: str,
    original_delivery_fee: float,
    discounted_delivery_fee: float,
    invite_email: str | None,
) -> None:
    normalized_invite_email = validate_delivery_promotion_invite_email(user_phone, invite_email)
    with connect_db() as connection:
        existing = connection.execute(
            """
            SELECT 1
            FROM delivery_promotion_redemptions
            WHERE payment_id = ? OR order_id = ?
            LIMIT 1
            """,
            (payment_id, order_id),
        ).fetchone()
        if existing:
            return
        if normalized_invite_email and delivery_promotion_invitee_email_used(normalized_invite_email):
            raise HTTPException(status_code=400, detail="这个好友邮箱已经使用过，不能重复使用")
        connection.execute(
            """
            INSERT INTO delivery_promotion_redemptions (
                id, user_phone, invitee_email, payment_id, order_id,
                original_delivery_fee, discounted_delivery_fee, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                user_phone,
                normalized_invite_email,
                payment_id,
                order_id,
                round(original_delivery_fee, 2),
                round(discounted_delivery_fee, 2),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def phone_from_authorization(authorization: str | None) -> str | None:
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("dev-token-"):
        return None

    account = token.removeprefix("dev-token-")
    if account.startswith("oauth:"):
        return account

    try:
        return normalize_myanmar_phone(account)
    except HTTPException:
        return None


def normalize_chat_sender_account(value: str | None) -> str | None:
    account = clean_optional_text(value)
    if not account:
        return None
    if account.startswith("oauth:"):
        return account
    if "@" in account:
        return account
    try:
        return normalize_myanmar_phone(account)
    except HTTPException:
        return None


def require_account_phone(authorization: str | None) -> str:
    phone = phone_from_authorization(authorization)
    if not phone:
        raise HTTPException(status_code=401, detail="请先登录")
    return phone


def account_conversation_id(conversation_id: str, authorization: str | None, fallback_phone: str | None = None) -> str:
    conversation_id = conversation_id.strip()
    if conversation_id != "main":
        return conversation_id.lower()

    phone = phone_from_authorization(authorization) or clean_optional_text(fallback_phone)
    if not phone:
        return conversation_id

    if not phone.startswith("oauth:"):
        try:
            phone = normalize_myanmar_phone(phone)
        except HTTPException:
            pass
    return f"account:{phone}"


def upload_folder(value: str) -> str:
    normalized = value.strip().lower().replace("_", " ")
    folders = {
        "goods": "Goods",
        "kpay ss": "kpay ss",
        "payment proof": "kpay ss",
        "chat": "Chats",
        "chats": "Chats",
        "nrc": "NRC",
        "profile picture": "profile picture",
        "payment qr": "payment qr",
        "payment qr code": "payment qr",
        "rider settlement": "rider settlement",
        "user settlement": "user settlement",
    }
    if normalized not in folders:
        raise HTTPException(status_code=400, detail="不支持的上传文件夹")
    return folders[normalized]


def safe_upload_name(file_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", Path(file_name).name.strip())
    if not cleaned or cleaned in [".", ".."]:
        raise HTTPException(status_code=400, detail="文件名不正确")
    return cleaned


def gcs_bucket_name() -> str:
    return os.getenv("GCS_BUCKET") or os.getenv("GCS_BUCKET_NAME", "courierblink")


def gcs_credentials_info() -> dict | None:
    credentials_json = clean_optional_text(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
    if credentials_json:
        try:
            return json.loads(credentials_json)
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"GCS 凭证 JSON 格式不正确：{error}") from error

    credentials_base64 = clean_optional_text(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_BASE64"))
    if credentials_base64:
        try:
            decoded = base64.b64decode(credentials_base64).decode("utf-8")
            return json.loads(decoded)
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"GCS base64 凭证格式不正确：{error}") from error

    credentials_value = clean_optional_text(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    if credentials_value and credentials_value.startswith("{"):
        try:
            return json.loads(credentials_value)
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"GCS 凭证 JSON 格式不正确：{error}") from error

    return None


def gcs_client():
    if service_account is None:
        raise HTTPException(status_code=500, detail="服务器未安装 Google service account 依赖")

    credentials_info = gcs_credentials_info()
    if credentials_info:
        credentials = service_account.Credentials.from_service_account_info(credentials_info)
        return storage.Client(credentials=credentials, project=credentials.project_id)

    if not clean_optional_text(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")):
        raise HTTPException(
            status_code=500,
            detail="GCS 未配置凭证。请在 Render 设置 GOOGLE_APPLICATION_CREDENTIALS_JSON 或 GOOGLE_APPLICATION_CREDENTIALS_BASE64。",
        )

    return storage.Client()


def gcs_object_name_from_url(value: str | None) -> str | None:
    if not value:
        return None

    parsed = urlparse(value)
    bucket_name = gcs_bucket_name()
    if parsed.netloc == "storage.googleapis.com":
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and parts[0] == bucket_name:
            return unquote(parts[1])

    if parsed.netloc == f"{bucket_name}.storage.googleapis.com":
        return unquote(parsed.path.lstrip("/"))

    return None


def signed_gcs_read_url(value: str | None) -> str | None:
    object_name = gcs_object_name_from_url(value)
    if not object_name or storage is None:
        return value

    try:
        bucket = gcs_client().bucket(gcs_bucket_name())
        blob = bucket.blob(object_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=30),
            method="GET",
        )
    except Exception:
        logger.exception("GCS signed read URL creation failed")
        return value


def upload_base64_image(image_data: str, content_type: str | None, file_name: str | None, folder: str = "chat") -> str:
    if storage is None:
        raise HTTPException(status_code=500, detail="google-cloud-storage is not installed")

    content_type = (content_type or "image/jpeg").strip()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="只能上传图片")

    try:
        data = base64.b64decode(image_data, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="图片数据不正确") from exc

    if not data:
        raise HTTPException(status_code=400, detail="图片数据为空")
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片最多 8MB")

    safe_name = safe_upload_name(file_name or f"chat-{uuid4().hex}.jpg")
    object_name = f"{upload_folder(folder)}/{uuid4().hex}-{safe_name}"

    try:
        bucket_name = gcs_bucket_name()
        blob = gcs_client().bucket(bucket_name).blob(object_name)
        blob.upload_from_string(data, content_type=content_type)
    except Exception as exc:
        logger.exception("GCS image upload failed")
        raise HTTPException(status_code=500, detail="图片上传失败") from exc

    return f"https://storage.googleapis.com/{bucket_name}/{quote(object_name)}"


def delivery_platform_fee(delivery_fee: float) -> float:
    rate = 0.08 if delivery_fee >= 10_000 else 0.10
    return round(delivery_fee * rate)


def delivery_payout_fee(delivery_fee: float) -> float:
    return max(delivery_fee - delivery_platform_fee(delivery_fee), 0)


def order_for_response(order: OrderResponse) -> OrderResponse:
    delivery_fee = order.delivery_fee or order.price
    platform_fee = order.platform_delivery_fee if order.promotion_applied else order.platform_delivery_fee or delivery_platform_fee(delivery_fee)
    rider_fee = order.rider_delivery_fee or delivery_payout_fee(order.original_delivery_fee or delivery_fee)
    return order.model_copy(
        update={
            "platform_delivery_fee": platform_fee,
            "rider_delivery_fee": rider_fee,
            "goods_image_url": signed_gcs_read_url(order.goods_image_url),
            "payment_proof_url": signed_gcs_read_url(order.payment_proof_url),
            "rider_settlement_qr_url": signed_gcs_read_url(order.rider_settlement_qr_url),
            "user_settlement_qr_url": signed_gcs_read_url(order.user_settlement_qr_url),
        }
    )


def order_from_row(row: sqlite3.Row) -> OrderResponse:
    return OrderResponse.model_validate(json.loads(row["payload"]))


def prepaid_payment_from_row(row: sqlite3.Row) -> PrepaidPaymentResponse:
    payload = json.loads(row["payload"])
    payload.setdefault("payment_mode", "cod")
    return PrepaidPaymentResponse.model_validate(payload)


def save_prepaid_payment(payment: PrepaidPaymentResponse) -> None:
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO prepaid_payments (id, user_phone, status, created_at, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_phone = excluded.user_phone,
                status = excluded.status,
                created_at = excluded.created_at,
                payload = excluded.payload
            """,
            (
                payment.id,
                payment.user_phone,
                payment.status,
                payment.created_at.isoformat(),
                json.dumps(payment.model_dump(mode="json"), ensure_ascii=False),
            ),
        )


def load_prepaid_payment(payment_id: str) -> PrepaidPaymentResponse | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT payload FROM prepaid_payments WHERE id = ?",
            (payment_id,),
        ).fetchone()

    if not row:
        return None
    return prepaid_payment_from_row(row)


def load_admin_prepaid_payments() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT payload
            FROM prepaid_payments
            ORDER BY created_at DESC
            """
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        payment = prepaid_payment_from_row(row)
        data = payment.model_dump(mode="json")
        data["payment_proof_url"] = signed_gcs_read_url(payment.payment_proof_url)
        result.append(data)
    return result


def dinger_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=503, detail=f"Dinger 未配置：缺少 {name}")
    return value


def dinger_base_url() -> str:
    return os.getenv("DINGER_BASE_URL", "https://staging.dinger.asia/").rstrip("/") + "/"


def rsa_encrypt_for_dinger(payload: dict) -> str:
    public_key_text = dinger_env("DINGER_PUBLIC_KEY")
    if "BEGIN PUBLIC KEY" not in public_key_text:
        public_key_text = (
            "-----BEGIN PUBLIC KEY-----\n"
            + public_key_text.replace("\\n", "\n")
            + "\n-----END PUBLIC KEY-----"
        )
    else:
        public_key_text = public_key_text.replace("\\n", "\n")

    public_key = serialization.load_pem_public_key(public_key_text.encode("utf-8"))
    encrypted = public_key.encrypt(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        asymmetric_padding.PKCS1v15(),
    )
    return base64.b64encode(encrypted).decode("utf-8")


async def create_dinger_charge(
    payment_id: str,
    request: CreateDingerPaymentRequest,
    user_phone: str,
) -> dict:
    base_url = dinger_base_url()
    project_name = dinger_env("DINGER_PROJECT_NAME")
    api_key = dinger_env("DINGER_API_KEY")
    merchant_name = dinger_env("DINGER_MERCHANT_NAME")
    customer_name = clean_optional_text(request.customer_name) or user_phone
    amount = int(round(request.amount))

    async with httpx.AsyncClient(timeout=30) as client:
        token_response = await client.get(
            f"{base_url}api/token",
            params={
                "projectName": project_name,
                "apiKey": api_key,
                "merchantName": merchant_name,
            },
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        payment_token = token_data.get("token", {}).get("paymentToken")
        if not payment_token:
            raise HTTPException(status_code=502, detail="Dinger token 获取失败")

        payload = {
            "providerName": request.provider_name,
            "methodName": request.method_name,
            "totalAmount": amount,
            "orderId": payment_id,
            "customerPhone": user_phone,
            "customerName": customer_name,
            "items": json.dumps(
                [
                    {
                        "name": "Blink Delivery Fee",
                        "amount": amount,
                    }
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        encrypted_payload = rsa_encrypt_for_dinger(payload)
        pay_response = await client.post(
            f"{base_url}api/pay",
            headers={"Authorization": f"Bearer {payment_token}"},
            data={"payload": encrypted_payload},
        )
        pay_response.raise_for_status()
        pay_data = pay_response.json()

    if str(pay_data.get("code")) not in {"0", "000"}:
        raise HTTPException(status_code=502, detail=pay_data.get("message") or "Dinger 创建付款失败")
    response = pay_data.get("response")
    if not isinstance(response, dict):
        raise HTTPException(status_code=502, detail="Dinger 付款响应异常")
    return response


def dinger_secret_key_bytes() -> bytes:
    secret = dinger_env("DINGER_SECRET_KEY")
    raw = secret.encode("utf-8")
    if len(raw) in (16, 24, 32):
        return raw
    try:
        decoded = bytes.fromhex(secret)
        if len(decoded) in (16, 24, 32):
            return decoded
    except ValueError:
        pass
    raise HTTPException(status_code=503, detail="Dinger secret key 长度不正确")


def decrypt_dinger_payment_result(payment_result: str) -> tuple[dict, str]:
    encrypted = base64.b64decode(payment_result)
    cipher = Cipher(algorithms.AES(dinger_secret_key_bytes()), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = symmetric_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    text = plaintext.decode("utf-8")
    return json.loads(text), text


def verify_dinger_checksum(result_text: str, checksum: str | None) -> None:
    if not checksum:
        return
    digest = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
    if digest.lower() != checksum.lower():
        raise HTTPException(status_code=400, detail="Dinger callback checksum 不正确")


def save_order(order: OrderResponse, user_phone: str, rider_phone: str | None = None) -> None:
    with connect_db() as connection:
        existing = connection.execute(
            "SELECT user_phone, rider_phone FROM orders WHERE id = ?",
            (order.id,),
        ).fetchone()
        stored_user_phone = existing["user_phone"] if existing else user_phone
        stored_rider_phone = rider_phone if rider_phone is not None else (existing["rider_phone"] if existing else None)
        connection.execute(
            """
            INSERT INTO orders (id, user_phone, rider_phone, status, created_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_phone = excluded.user_phone,
                rider_phone = excluded.rider_phone,
                status = excluded.status,
                created_at = excluded.created_at,
                payload = excluded.payload
            """,
            (
                order.id,
                stored_user_phone,
                stored_rider_phone,
                order.status,
                order.created_at.isoformat(),
                json.dumps(order.model_dump(mode="json"), ensure_ascii=False),
            ),
        )


def load_user_orders(user_phone: str) -> list[OrderResponse]:
    release_expired_rider_deposit_orders()
    hidden_before = account_data_hidden_before(user_phone)
    with connect_db() as connection:
        if hidden_before:
            rows = connection.execute(
                """
                SELECT payload FROM orders
                WHERE user_phone = ?
                  AND created_at > ?
                ORDER BY created_at DESC
                """,
                (user_phone, hidden_before),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT payload FROM orders
                WHERE user_phone = ?
                ORDER BY created_at DESC
                """,
                (user_phone,),
            ).fetchall()
    return [order_from_row(row) for row in rows]


def load_rider_orders(rider_phone: str) -> list[OrderResponse]:
    release_expired_rider_deposit_orders()
    hidden_before = account_data_hidden_before(rider_phone)
    with connect_db() as connection:
        if hidden_before:
            rows = connection.execute(
                """
                SELECT user_phone, payload FROM orders
                WHERE status = 'matching'
                   OR (rider_phone = ? AND created_at > ?)
                ORDER BY created_at DESC
                """,
                (rider_phone, hidden_before),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT user_phone, payload FROM orders
                WHERE status = 'matching'
                   OR rider_phone = ?
                ORDER BY created_at DESC
                """,
                (rider_phone,),
            ).fetchall()
    orders = [
        order
        for row in rows
        for order in [order_from_row(row)]
        if order.status != "matching" or app_data_visible_to_account(row["user_phone"], order.created_at)
    ]
    return [
        order
        for order in orders
        if order.status != "matching"
        or order.user_payment_status == "confirmed"
    ]


def load_order_record(order_id: str) -> tuple[OrderResponse, str, str | None] | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT user_phone, rider_phone, payload FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if not row:
        return None
    return order_from_row(row), row["user_phone"], row["rider_phone"]


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def rider_deposit_due_at(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + RIDER_DEPOSIT_CONFIRM_WINDOW


def release_expired_rider_deposit_orders() -> None:
    now = datetime.now(timezone.utc)
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT id, user_phone, payload
            FROM orders
            WHERE status = 'accepted'
            """
        ).fetchall()

        for row in rows:
            order = order_from_row(row)
            if order.rider_deposit_status in ("not_required", "confirmed"):
                continue
            due_at = utc_datetime(order.rider_deposit_due_at)
            if due_at is None:
                initialized = order.model_copy(update={"rider_deposit_due_at": rider_deposit_due_at(now)})
                connection.execute(
                    """
                    UPDATE orders
                    SET payload = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(initialized.model_dump(mode="json"), ensure_ascii=False),
                        row["id"],
                    ),
                )
                continue
            if due_at > now:
                continue

            released = order.model_copy(
                update={
                    "status": "matching",
                    "rider_name": None,
                    "rider_deposit_status": "unpaid",
                    "rider_deposit_due_at": None,
                    "rider_deposit_submitted_at": None,
                }
            )
            connection.execute(
                """
                UPDATE orders
                SET rider_phone = NULL,
                    status = ?,
                    payload = ?
                WHERE id = ?
                """,
                (
                    released.status,
                    json.dumps(released.model_dump(mode="json"), ensure_ascii=False),
                    row["id"],
                ),
            )


def sync_orders_for_prepaid_payment(payment: PrepaidPaymentResponse) -> None:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT user_phone, rider_phone, payload FROM orders"
        ).fetchall()

    for row in rows:
        order = order_from_row(row)
        if order.kpay_transaction_id != payment.id:
            continue
        if row["user_phone"] != payment.user_phone:
            continue

        updated = order.model_copy(update={"user_payment_status": payment.status})
        save_order(updated, user_phone=row["user_phone"], rider_phone=row["rider_phone"])


def user_profile_from_account(
    phone: str,
    email: str | None,
    nickname: str | None,
    avatar_url: str | None,
    payment_qr_url: str | None,
    terms_accepted_at: str | None = None,
    terms_version: str | None = None,
) -> UserProfile:
    user_id_digits = re.sub(r"\D", "", phone)
    user_id = f"user_{user_id_digits}" if user_id_digits else f"user_{hashlib.sha256(phone.encode('utf-8')).hexdigest()[:16]}"
    return UserProfile(
        id=user_id,
        phone=phone,
        email=email,
        nickname=nickname,
        avatar_url=signed_gcs_read_url(avatar_url),
        payment_qr_url=signed_gcs_read_url(payment_qr_url),
        terms_accepted_at=terms_accepted_at,
        terms_version=terms_version,
    )


def address_with_updated_contact_name(address: str, old_name: str | None, new_name: str) -> str:
    old = clean_optional_text(old_name)
    new = clean_optional_text(new_name) or new_name
    if old and address.startswith(f"{old},"):
        return f"{new}{address[len(old):]}"
    return address


def sync_user_profile_name(connection: sqlite3.Connection, phone: str, old_name: str | None, new_name: str) -> None:
    connection.execute(
        """
        UPDATE chat_messages
        SET sender_name = ?
        WHERE sender_phone = ?
            AND sender_type != 'admin'
        """,
        (new_name, phone),
    )

    rows = connection.execute(
        """
        SELECT id, user_phone, rider_phone, payload
        FROM orders
        WHERE user_phone = ?
        """,
        (phone,),
    ).fetchall()
    for row in rows:
        order = order_from_row(row)
        pickup_address = address_with_updated_contact_name(order.pickup_address, old_name, new_name)
        if pickup_address == order.pickup_address:
            continue
        updated = order.model_copy(update={"pickup_address": pickup_address})
        connection.execute(
            """
            UPDATE orders
            SET payload = ?
            WHERE id = ?
            """,
            (
                json.dumps(updated.model_dump(mode="json"), ensure_ascii=False),
                row["id"],
            ),
        )


def load_account_profile(phone: str) -> UserProfile | None:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT phone, email, nickname, avatar_url, payment_qr_url, terms_accepted_at, terms_version, app_deleted_at
            FROM accounts
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()

    if not row:
        return None
    if row["app_deleted_at"]:
        terms_accepted_at = row["terms_accepted_at"]
        terms_version = row["terms_version"]
        if not terms_accepted_at or terms_accepted_at <= row["app_deleted_at"]:
            terms_accepted_at = None
            terms_version = None
        return user_profile_from_account(
            row["phone"],
            None,
            None,
            None,
            None,
            terms_accepted_at,
            terms_version,
        )
    return user_profile_from_account(
        row["phone"],
        row["email"],
        row["nickname"],
        row["avatar_url"],
        row["payment_qr_url"],
        row["terms_accepted_at"],
        row["terms_version"],
    )


def save_account(
    phone: str,
    email: str | None = None,
    nickname: str | None = None,
    avatar_url: str | None = None,
    payment_qr_url: str | None = None,
    terms_accepted_at: str | None = None,
    terms_version: str | None = None,
    clear_app_deleted_at: bool = False,
) -> UserProfile:
    with connect_db() as connection:
        existing_account = connection.execute(
            """
            SELECT nickname
            FROM accounts
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()
        previous_nickname = clean_optional_text(existing_account["nickname"]) if existing_account else None
        connection.execute(
            """
            INSERT INTO accounts (phone, email, nickname, avatar_url, payment_qr_url, terms_accepted_at, terms_version, last_login_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                email = COALESCE(excluded.email, accounts.email),
                nickname = COALESCE(excluded.nickname, accounts.nickname),
                avatar_url = COALESCE(excluded.avatar_url, accounts.avatar_url),
                payment_qr_url = COALESCE(excluded.payment_qr_url, accounts.payment_qr_url),
                terms_accepted_at = COALESCE(excluded.terms_accepted_at, accounts.terms_accepted_at),
                terms_version = COALESCE(excluded.terms_version, accounts.terms_version),
                last_login_at = excluded.last_login_at
            """,
            (
                phone,
                email,
                nickname,
                avatar_url,
                payment_qr_url,
                terms_accepted_at,
                terms_version,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if clear_app_deleted_at:
            connection.execute("UPDATE accounts SET app_deleted_at = NULL WHERE phone = ?", (phone,))
        if nickname is not None:
            sync_user_profile_name(connection, phone, previous_nickname, nickname)
    return load_account_profile(phone) or user_profile_from_account(
        phone,
        email,
        nickname,
        avatar_url,
        payment_qr_url,
        terms_accepted_at,
        terms_version,
    )


def account_data_hidden_before(phone: str) -> str | None:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT app_data_hidden_before
            FROM accounts
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()
    return row["app_data_hidden_before"] if row else None


def app_data_visible_to_account(phone: str, created_at: datetime) -> bool:
    hidden_before = account_data_hidden_before(phone)
    return not hidden_before or created_at.isoformat() > hidden_before


def account_nickname(phone: str | None) -> str | None:
    if not phone:
        return None
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT nickname
            FROM accounts
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()
    return clean_optional_text(row["nickname"]) if row else None


def normalize_app_role(role: str | None) -> str | None:
    value = (role or "").strip().lower()
    return value if value in {"user", "rider"} else None


def mark_account_app_role(phone: str | None, role: str | None) -> None:
    app_role = normalize_app_role(role)
    if not phone or not app_role:
        return
    now = datetime.now(timezone.utc).isoformat()
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO accounts (phone, app_role, app_role_updated_at, last_login_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                app_role = excluded.app_role,
                app_role_updated_at = excluded.app_role_updated_at
            """,
            (phone, app_role, now, now),
        )


def mark_account_deleted_for_app(phone: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect_db() as connection:
        connection.execute(
            """
            UPDATE accounts
            SET app_deleted_at = ?,
                app_data_hidden_before = ?
            WHERE phone = ?
            """,
            (now, now, phone),
        )
        if connection.total_changes == 0:
            connection.execute(
                """
                INSERT INTO accounts (
                    phone, nickname, avatar_url, payment_qr_url,
                    terms_accepted_at, terms_version, app_deleted_at,
                    app_data_hidden_before, last_login_at
                )
                VALUES (?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                """,
                (phone, now, now, now),
            )
    sms_codes.pop(phone, None)


def require_admin_key(key: str | None) -> None:
    expected = clean_optional_text(os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_KEY"))
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD 未配置")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="后台密码不正确")


def load_admin_orders() -> list[dict]:
    release_expired_rider_deposit_orders()
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT
                orders.user_phone,
                orders.rider_phone,
                user_account.nickname AS user_nickname,
                user_account.email AS user_email,
                rider_account.nickname AS rider_nickname,
                rider_account.email AS rider_email,
                orders.payload
            FROM orders
            LEFT JOIN accounts AS user_account
                ON user_account.phone = orders.user_phone
            LEFT JOIN accounts AS rider_account
                ON rider_account.phone = orders.rider_phone
            ORDER BY created_at DESC
            """
        ).fetchall()

    result: list[dict] = []
    for row in rows:
        order = order_for_response(order_from_row(row)).model_dump(mode="json")
        order["user_phone"] = row["user_phone"]
        order["rider_phone"] = row["rider_phone"]
        order["user_nickname"] = row["user_nickname"]
        order["user_email"] = row["user_email"]
        order["rider_nickname"] = row["rider_nickname"]
        order["rider_email"] = row["rider_email"]
        result.append(order)
    return result


def load_admin_accounts() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT phone, email, nickname, payment_qr_url, avatar_url, app_role, app_role_updated_at, last_login_at
            FROM accounts
            ORDER BY last_login_at DESC
            """
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        account = dict(row)
        phone = account.get("phone")
        if not normalize_app_role(account.get("app_role")) and phone:
            with connect_db() as connection:
                inferred = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM orders WHERE rider_phone = ?) AS rider_orders,
                        (SELECT COUNT(*) FROM orders WHERE user_phone = ?) AS user_orders,
                        (SELECT COUNT(*) FROM chat_messages WHERE sender_phone = ? AND sender_type = 'rider') AS rider_messages,
                        (SELECT COUNT(*) FROM chat_messages WHERE sender_phone = ? AND sender_type = 'user') AS user_messages
                    """,
                    (phone, phone, phone, phone),
                ).fetchone()
            rider_score = int(inferred["rider_orders"] or 0) + int(inferred["rider_messages"] or 0)
            user_score = int(inferred["user_orders"] or 0) + int(inferred["user_messages"] or 0)
            if rider_score or user_score:
                account["app_role"] = "rider" if rider_score >= user_score else "user"
        if not normalize_app_role(account.get("app_role")):
            account["app_role"] = "user"
        account["avatar_url"] = signed_gcs_read_url(account.get("avatar_url"))
        account["payment_qr_url"] = signed_gcs_read_url(account.get("payment_qr_url"))
        result.append(account)
    return result


def load_admin_chat_messages(limit: int = 1000) -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
            FROM chat_messages
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        message = dict(row)
        if message.get("sender_type") == "admin":
            message["sender_name"] = ADMIN_CHAT_SENDER_NAME
        message["image_url"] = signed_gcs_read_url(message.get("image_url"))
        result.append(message)
    return result


ADMIN_HTML = r'''
<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>快送后台</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f4f6f8; color: #111827; }
    header { position: sticky; top: 0; z-index: 2; display: flex; gap: 12px; align-items: center; padding: 14px 18px; background: rgba(255,255,255,.92); border-bottom: 1px solid #e5e7eb; backdrop-filter: blur(16px); }
    h1 { margin: 0; font-size: 20px; }
    .version { color: #6b7280; font-size: 12px; white-space: nowrap; }
    main { padding: 18px; display: grid; gap: 16px; }
    .toolbar { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    input, select, button { font: inherit; border: 1px solid #d1d5db; border-radius: 8px; padding: 9px 10px; background: #fff; transition: border-color .16s ease, box-shadow .16s ease, background .16s ease, color .16s ease, transform .12s ease, opacity .16s ease; }
    input:focus, select:focus { outline: none; border-color: #16a34a; box-shadow: 0 0 0 3px rgba(22,163,74,.14); }
    button { cursor: pointer; background: #16a34a; color: white; border-color: #16a34a; font-weight: 700; }
    button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 8px 18px rgba(17, 24, 39, .10); }
    button:active:not(:disabled) { transform: translateY(0); box-shadow: none; }
    button:disabled { cursor: wait; opacity: .64; }
    button.secondary { background: #fff; color: #111827; border-color: #d1d5db; }
    button.tab { display: inline-flex; align-items: center; gap: 6px; background: #fff; color: #374151; border-color: #d1d5db; }
    button.tab.active { background: #111827; color: #fff; border-color: #111827; }
    .tab-badge { min-width: 18px; padding: 2px 6px; border-radius: 999px; background: #ef4444; color: #fff; font-size: 11px; line-height: 1.2; text-align: center; }
    section { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px; transition: opacity .18s ease, transform .18s ease; }
    .page { display: none; }
    .page.active { display: block; }
    .page.grid.active { display: grid; }
    section h2 { margin: 0 0 12px; font-size: 17px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    table.orders-table { table-layout: fixed; }
    table.orders-table .col-order { width: 130px; }
    table.orders-table .col-party { width: 170px; }
    table.orders-table .col-status { width: 88px; }
    table.orders-table .col-amount { width: 100px; }
    table.orders-table .col-proof { width: 118px; }
    table.orders-table .col-deposit { width: 88px; }
    table.orders-table .col-actions { width: 150px; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #eef2f7; vertical-align: top; }
    th { color: #6b7280; font-weight: 700; }
    tr { transition: background .14s ease; }
    tr:hover { background: #f9fafb; }
    tr.is-new { background: #ecfdf5; animation: freshRow 2.4s ease-out 1; }
    .address-cell { width: 100%; max-width: 100%; overflow-wrap: anywhere; word-break: break-word; line-height: 1.35; }
    .address-cell .muted { display: block; margin-top: 4px; }
    .actions-cell { width: 150px; }
    .actions-cell button { width: 100%; margin-bottom: 6px; padding: 8px 6px; white-space: normal; }
    .grid { display: grid; grid-template-columns: 1.4fr .9fr; gap: 16px; align-items: start; }
    .pill { display: inline-flex; border-radius: 999px; padding: 3px 8px; background: #eef2ff; color: #3730a3; font-size: 12px; font-weight: 700; }
    .role-pill { display: inline-flex; margin-left: 6px; padding: 2px 7px; border-radius: 999px; background: #ecfdf5; color: #047857; font-size: 11px; font-weight: 700; vertical-align: middle; }
    .role-pill.rider { background: #fff7ed; color: #c2410c; }
    .muted { color: #6b7280; }
    .detail { display: grid; gap: 10px; }
    .detail img { max-width: 100%; max-height: 260px; object-fit: contain; border-radius: 8px; background: #f3f4f6; }
    .thumb { width: 84px; height: 84px; object-fit: cover; border-radius: 8px; background: #f3f4f6; }
    .row { display: grid; grid-template-columns: 110px 1fr; gap: 10px; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .chat { max-height: 360px; overflow: auto; }
    .chat img { max-width: 180px; max-height: 180px; object-fit: contain; border-radius: 8px; background: #f3f4f6; margin-top: 8px; }
    .conversation-list { display: grid; gap: 8px; }
    .conversation-row { width: 100%; text-align: left; background: #fff; color: #111827; border: 1px solid #e5e7eb; }
    .conversation-row.active { border-color: #111827; background: #111827; color: #fff; }
    .account-row { cursor: pointer; }
    .account-row.active { background: #eef2ff; }
    .account-link { display: inline-block; padding: 0; border: 0; border-radius: 0; background: transparent; color: #111827; font: inherit; font-weight: 700; text-align: left; cursor: pointer; }
    .account-link:hover { color: #16a34a; text-decoration: underline; transform: none; box-shadow: none; }
    .accounts-layout { display: grid; grid-template-columns: 1fr; gap: 16px; align-items: start; }
    .accounts-list { min-width: 0; overflow-x: auto; }
    .account-detail { min-width: 0; max-height: calc(100vh - 180px); overflow: auto; display: grid; gap: 14px; }
    .account-detail h3 { margin: 0 0 8px; font-size: 15px; }
    .account-tabs { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .account-tab { background: #fff; color: #374151; border-color: #d1d5db; }
    .account-tab.active { background: #111827; color: #fff; border-color: #111827; }
    .account-panel { min-width: 0; }
    .mini-table { table-layout: fixed; }
    .mini-table td, .mini-table th { padding: 8px 6px; font-size: 13px; }
    .chat-thread { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; margin-bottom: 10px; background: #f9fafb; }
    .chat-thread-title { display: flex; justify-content: space-between; gap: 10px; font-weight: 700; margin-bottom: 8px; }
    .chat-line { margin: 8px 0 0; padding-top: 8px; border-top: 1px solid #e5e7eb; }
    .chat-line:first-of-type { border-top: 0; padding-top: 0; }
    .chat-line img { max-width: 160px; max-height: 160px; object-fit: contain; border-radius: 8px; background: #f3f4f6; margin-top: 6px; }
    .service-reply { display: flex; gap: 8px; margin-top: 12px; }
    .service-reply textarea { flex: 1; min-height: 44px; padding: 10px; border: 1px solid #d1d5db; border-radius: 8px; font: inherit; resize: vertical; }
    .service-reply input[type="file"] { max-width: 190px; align-self: center; }
    .empty { padding: 28px; text-align: center; color: #6b7280; background: #f9fafb; border-radius: 8px; }
    .hidden { display: none !important; }
    .summary { display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 10px; padding: 0; background: transparent; border: 0; }
    .summary-card { display: grid; gap: 4px; min-height: 70px; padding: 12px; border: 1px solid #e5e7eb; border-radius: 8px; background: #fff; }
    .summary-card strong { font-size: 22px; line-height: 1; }
    .summary-card span { color: #6b7280; font-size: 12px; font-weight: 700; }
    .filter { min-width: 128px; }
    .auto-toggle.paused { background: #fff; color: #111827; border-color: #d1d5db; }
    .refresh-state { display: inline-flex; align-items: center; gap: 6px; color: #6b7280; font-size: 12px; white-space: nowrap; }
    .refresh-dot { width: 7px; height: 7px; border-radius: 999px; background: #22c55e; box-shadow: 0 0 0 4px rgba(34,197,94,.14); }
    body.loading .refresh-dot { background: #f59e0b; box-shadow: 0 0 0 4px rgba(245,158,11,.16); animation: pulse .8s ease-in-out infinite alternate; }
    .toast { position: fixed; right: 18px; bottom: 18px; z-index: 10; max-width: min(420px, calc(100vw - 36px)); padding: 12px 14px; border-radius: 10px; background: #111827; color: #fff; box-shadow: 0 18px 40px rgba(17,24,39,.22); opacity: 0; transform: translateY(10px); pointer-events: none; transition: opacity .18s ease, transform .18s ease; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.error { background: #b91c1c; }
    .modal-backdrop { position: fixed; inset: 0; z-index: 20; display: none; align-items: center; justify-content: center; padding: 24px; background: rgba(17, 24, 39, .42); }
    .modal-backdrop.show { display: flex; }
    .modal-card { width: min(1040px, 100%); max-height: min(86vh, 920px); overflow: auto; border-radius: 12px; background: #fff; box-shadow: 0 24px 70px rgba(17,24,39,.28); }
    .modal-head { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px; border-bottom: 1px solid #eef2f7; background: #fff; }
    .modal-head h3 { margin: 0; font-size: 16px; }
    .modal-close { width: auto; min-width: 44px; padding: 8px 12px; background: #fff; color: #111827; border-color: #d1d5db; }
    .modal-body { padding: 18px; display: grid; gap: 14px; }
    @keyframes pulse { from { opacity: .55; } to { opacity: 1; } }
    @keyframes freshRow { from { box-shadow: inset 4px 0 0 #22c55e; } to { box-shadow: inset 4px 0 0 transparent; } }
    @media (max-width: 1100px) { .account-detail { max-height: none; } .summary { grid-template-columns: repeat(2, minmax(140px, 1fr)); } }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } header { flex-wrap: wrap; } .toolbar { margin-left: 0; width: 100%; flex-wrap: wrap; } }
    @media (max-width: 560px) { .summary { grid-template-columns: 1fr; } .toolbar input, .toolbar select, .toolbar button { flex: 1 1 150px; min-width: 0; } }
  </style>
</head>
<body>
  <header>
    <h1>快送后台</h1>
    <span class="version">orders-ui-v20</span>
    <div class="toolbar">
      <input id="key" type="password" placeholder="后台密码" />
      <input id="q" placeholder="搜索订单/手机号/地址" />
      <select id="statusFilter" class="filter" title="订单状态">
        <option value="all">全部状态</option>
        <option value="matching">待接单</option>
        <option value="accepted">已接单</option>
        <option value="picking_up">取件中</option>
        <option value="delivering">配送中</option>
        <option value="completed">已完成</option>
        <option value="cancelled">已取消</option>
      </select>
      <select id="paymentFilter" class="filter" title="付款状态">
        <option value="all">全部付款</option>
        <option value="pending">待确认</option>
        <option value="confirmed">已确认</option>
        <option value="unpaid">未付</option>
        <option value="rejected">已拒绝</option>
      </select>
      <span class="refresh-state"><span class="refresh-dot"></span><span id="refreshStatus">就绪</span></span>
      <select id="refreshInterval" title="自动同步速度">
        <option value="3000">3 秒</option>
        <option value="5000" selected>5 秒</option>
        <option value="10000">10 秒</option>
        <option value="30000">30 秒</option>
      </select>
      <button id="autoRefreshButton" class="auto-toggle" onclick="toggleAutoRefresh()">自动同步中</button>
      <button id="refreshButton" onclick="loadData()">刷新</button>
      <button id="tab-payments" class="tab active" onclick="showPage('payments')">订单</button>
      <button id="tab-accounts" class="tab" onclick="showPage('accounts')">账号资料</button>
      <button id="tab-settlements" class="tab" onclick="showPage('settlements')">结算</button>
      <button id="tab-service" class="tab" onclick="showPage('service')">Customer Service</button>
    </div>
  </header>
  <main>
    <section class="summary" id="summary"></section>
    <section id="page-payments" class="page active">
      <h2>订单</h2>
      <table class="orders-table">
        <colgroup>
          <col class="col-order"><col class="col-party"><col class="col-status"><col class="col-amount">
          <col class="col-proof"><col class="col-deposit"><col><col class="col-actions">
        </colgroup>
        <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>付款截图</th><th>骑手押金</th><th>地址</th><th>操作</th></tr></thead>
        <tbody id="codOrders"></tbody>
      </table>
    </section>
    <section id="detailSection" class="hidden">
      <h2>订单详情</h2>
      <div id="detail" class="detail muted"></div>
    </section>
    <section id="page-accounts" class="page">
      <h2>账号资料</h2>
      <div class="accounts-layout">
        <div class="accounts-list">
          <table><thead><tr><th>头像</th><th>昵称</th><th>收款码</th><th>登录邮箱</th><th>最近登录</th></tr></thead><tbody id="accounts"></tbody></table>
        </div>
      </div>
    </section>
    <section id="page-settlements" class="page">
      <h2>结算</h2>
      <table>
        <thead><tr><th>订单</th><th>用户/骑手</th><th>金额</th><th>收款资料</th><th>二维码</th><th>结算状态</th><th>操作</th></tr></thead>
        <tbody id="settlements"></tbody>
      </table>
    </section>
    <section id="page-service" class="page">
      <h2>Customer Service</h2>
      <div class="grid">
        <section>
          <h3>会话</h3>
          <div id="chatConversations" class="conversation-list"></div>
        </section>
        <section>
          <h3 id="chatTitle">聊天记录</h3>
          <div id="chat" class="chat"></div>
          <div class="service-reply">
            <textarea id="serviceReply" placeholder="回复用户/骑手"></textarea>
            <input id="serviceImage" type="file" accept="image/*" />
            <button onclick="sendServiceReply(this)">发送</button>
          </div>
        </section>
      </div>
    </section>
  </main>
  <div id="accountModal" class="modal-backdrop" onclick="closeAccountModal()">
    <div class="modal-card" onclick="event.stopPropagation()">
      <div class="modal-head">
        <h3>账号详情</h3>
        <button class="modal-close" onclick="closeAccountModal()">关闭</button>
      </div>
      <div id="accountDetail" class="modal-body account-detail"></div>
    </div>
  </div>
  <div id="toast" class="toast"></div>
  <script>
    let state = { orders: [], accounts: [], messages: [], payments: [] };
    let currentPage = "payments";
    let tabBadges = { payments: 0, orders: 0, accounts: 0, settlements: 0, service: 0 };
    let selectedServiceConversationId = null;
    let selectedAccountPhone = null;
    let selectedAccountPanel = "placed";
    let activeDetailId = null;
    let lastLoadStartedAt = 0;
    let searchTimer = null;
    let keyTimer = null;
    let autoRefreshTimer = null;
    let autoRefreshEnabled = localStorage.getItem("blinkAdminAutoRefresh") !== "off";
    let autoRefreshIntervalMs = Number(localStorage.getItem("blinkAdminRefreshMs") || 5000);
    let hasLoadedOnce = false;
    let highlightedIds = new Set();
    const pages = ["payments","accounts","settlements","service"];
    const pageTitles = {
      payments: "订单",
      accounts: "账号资料",
      settlements: "结算",
      service: "Customer Service"
    };
    const statusOptions = ["matching","accepted","picking_up","delivering","completed","cancelled"];
    const paymentOptions = ["not_required","unpaid","pending","confirmed","rejected"];
    const settlementOptions = ["pending","paid_to_user","paid_to_rider","completed"];
    const labels = {
      matching: "待接单", accepted: "已接单", picking_up: "取件中", delivering: "配送中", completed: "已完成", cancelled: "已取消",
      cod: "货到付款", prepaid: "货费已付款",
      not_required: "无需", unpaid: "未付", pending: "待确认", confirmed: "已确认", rejected: "已拒绝",
      paid_to_user: "已付用户", paid_to_rider: "已付骑手"
    };

    function keyParam() { return encodeURIComponent(document.getElementById("key").value); }
    function label(value) { return labels[value] || value || ""; }
    function riderDepositLabel(value) {
      if (value === "pending") return "骑手已转，待确认";
      if (value === "confirmed") return "平台已确认";
      if (value === "unpaid") return "骑手未转";
      if (value === "rejected") return "已拒绝";
      return label(value);
    }
    function money(value) { return `${Number(value || 0).toLocaleString()} MMK`; }
    function accountFor(phone) {
      return (state.accounts || []).find(item => item.phone === phone);
    }
    function accountName(phone, fallbackName = "") {
      const account = accountFor(phone);
      return account?.nickname || fallbackName || "";
    }
    function accountEmail(phone, fallbackEmail = "") {
      const account = accountFor(phone);
      return fallbackEmail || account?.email || "";
    }
    function accountLoginLabel(phone, fallbackEmail = "") {
      const email = accountEmail(phone, fallbackEmail);
      if (email) return email;
      const rawPhone = String(phone || "");
      const normalized = rawPhone.toLowerCase();
      if (normalized.startsWith("oauth:google:")) return "Gmail 登录";
      if (normalized.startsWith("oauth:apple:")) return "Apple 登录";
      if (!phone || normalized.startsWith("oauth:")) return "第三方登录";
      return rawPhone;
    }
    function accountContact(phone, fallbackEmail = "") {
      return accountLoginLabel(phone, fallbackEmail);
    }
    function accountEmailHtml(phone, fallbackEmail = "") {
      const email = accountEmail(phone, fallbackEmail);
      return email ? `邮箱：${escapeHtml(email)}` : `邮箱：未绑定`;
    }
    function accountLoginHtml(phone, fallbackEmail = "") {
      return `${escapeHtml(accountLoginLabel(phone, fallbackEmail))}<br><span class="muted">${accountEmailHtml(phone, fallbackEmail)}</span>`;
    }
    function appRoleLabel(role) {
      if (role === "rider") return "骑手";
      if (role === "user") return "用户";
      return "";
    }
    function appRoleHtml(role) {
      if (role !== "rider" && role !== "user") return "";
      const value = role === "rider" ? "rider" : "user";
      return `<span class="role-pill ${value}">${escapeHtml(appRoleLabel(role))}</span>`;
    }
    function displayAccount(phone, fallbackName = "", fallbackEmail = "") {
      const name = accountName(phone, fallbackName);
      if (!phone) return escapeHtml(name || "未接单");
      const contact = accountContact(phone, fallbackEmail);
      const primary = name || contact || "账号";
      return `
        <button class="account-link" onclick="event.stopPropagation(); selectAccount(${jsValue(phone)})">${escapeHtml(primary)}</button>
        <br><span class="muted">${accountEmailHtml(phone, fallbackEmail)}</span>`;
    }
    function deliveryFeeCell(order) {
      const gross = Number(order.delivery_fee || order.price || 0);
      const platform = Number(order.platform_delivery_fee || Math.round(gross * (gross >= 10000 ? 0.08 : 0.10)));
      const rider = Number(order.rider_delivery_fee || Math.max(gross - platform, 0));
      return `配送费 ${money(rider)}<br><span class="muted">原送货费 ${money(gross)} / 平台扣费 ${money(platform)}</span><br><span class="muted">货值 ${money(order.goods_amount)}</span>`;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[s]));
    }
    function jsValue(value) {
      return escapeHtml(JSON.stringify(String(value ?? "")));
    }
    function optionHtml(options, current) {
      return options.map(v => `<option value="${v}" ${v === current ? "selected" : ""}>${label(v)}</option>`).join("");
    }
    function dateMs(value) {
      const time = new Date(value || 0).getTime();
      return Number.isNaN(time) ? 0 : time;
    }
    function identitySets(data = state) {
      const settlementEvents = [];
      (data.orders || []).forEach(order => {
        ["user_settlement_requested_at","rider_settlement_requested_at","user_settlement_paid_at","rider_settlement_paid_at"].forEach(field => {
          if (order[field]) settlementEvents.push(`${order.id}:${field}:${order[field]}`);
        });
      });
      return {
        orders: new Set((data.orders || []).map(item => item.id).filter(Boolean)),
        payments: new Set((data.payments || []).map(item => item.id).filter(Boolean)),
        accounts: new Set((data.accounts || []).map(item => item.phone).filter(Boolean)),
        messages: new Set((data.messages || []).map(item => item.id).filter(Boolean)),
        settlements: new Set(settlementEvents)
      };
    }
    function rememberNewItems(nextState) {
      if (!hasLoadedOnce) return { orders: 0, payments: 0, accounts: 0, settlements: 0, messages: 0 };
      const previous = identitySets();
      const freshOrders = (nextState.orders || []).filter(item => item.id && !previous.orders.has(item.id));
      const freshPayments = (nextState.payments || []).filter(item => item.id && !previous.payments.has(item.id));
      const freshAccounts = (nextState.accounts || []).filter(item => item.phone && !previous.accounts.has(item.phone));
      const freshMessages = (nextState.messages || []).filter(item => item.id && !previous.messages.has(item.id));
      const nextSettlementEvents = Array.from(identitySets(nextState).settlements);
      const freshSettlements = nextSettlementEvents.filter(item => !previous.settlements.has(item));
      freshOrders.forEach(() => incrementTabBadge("payments"));
      freshPayments.forEach(() => incrementTabBadge("payments"));
      if (freshAccounts.length) incrementTabBadge("accounts", freshAccounts.length);
      if (freshSettlements.length) incrementTabBadge("settlements", freshSettlements.length);
      if (freshMessages.length) incrementTabBadge("service", freshMessages.length);
      highlightedIds = new Set([
        ...freshOrders.map(item => `order:${item.id}`),
        ...freshPayments.map(item => `payment:${item.id}`),
        ...freshMessages.map(item => `message:${item.id}`)
      ]);
      if (highlightedIds.size) {
        setTimeout(() => {
          highlightedIds.clear();
          render();
        }, 9000);
      }
      updateTabBadges();
      return {
        orders: freshOrders.length,
        payments: freshPayments.length,
        accounts: freshAccounts.length,
        settlements: freshSettlements.length,
        messages: freshMessages.length
      };
    }
    function rowClass(kind, id) {
      return highlightedIds.has(`${kind}:${id}`) ? ` class="is-new"` : "";
    }
    function newestDateMs(item, fields) {
      return Math.max(...fields.map(field => dateMs(item[field])));
    }
    function sortByDateDesc(items, fields = ["created_at"]) {
      return [...items].sort((a, b) => newestDateMs(b, fields) - newestDateMs(a, fields));
    }
    function sortByDateAsc(items, fields = ["created_at"]) {
      return [...items].sort((a, b) => newestDateMs(a, fields) - newestDateMs(b, fields));
    }
    function showPage(page, keepDetail = false) {
      currentPage = page;
      clearTabBadge(page);
      if (!keepDetail) hideDetail();
      pages.forEach(name => {
        document.getElementById(`page-${name}`)?.classList.toggle("active", name === page);
        document.getElementById(`tab-${name}`)?.classList.toggle("active", name === page);
      });
      updateTabBadges();
    }
    function incrementTabBadge(page, count = 1) {
      if (!pages.includes(page) || page === currentPage) return;
      tabBadges[page] = (tabBadges[page] || 0) + count;
    }
    function clearTabBadge(page) {
      if (!pages.includes(page)) return;
      tabBadges[page] = 0;
    }
    function updateTabBadges() {
      pages.forEach(page => {
        const button = document.getElementById(`tab-${page}`);
        if (!button) return;
        const count = tabBadges[page] || 0;
        button.innerHTML = `${escapeHtml(pageTitles[page] || page)}${count ? ` <span class="tab-badge">${count > 99 ? "99+" : count}</span>` : ""}`;
      });
    }
    function hideDetail() {
      activeDetailId = null;
      document.getElementById("detailSection")?.classList.add("hidden");
      const detail = document.getElementById("detail");
      if (detail) detail.innerHTML = "";
    }

    function showToast(message, type = "success") {
      const toast = document.getElementById("toast");
      if (!toast) return;
      toast.textContent = message;
      toast.classList.toggle("error", type === "error");
      toast.classList.add("show");
      clearTimeout(showToast.timer);
      showToast.timer = setTimeout(() => toast.classList.remove("show"), 2600);
    }

    function setLoading(isLoading, text = "") {
      document.body.classList.toggle("loading", isLoading);
      const status = document.getElementById("refreshStatus");
      if (status) status.textContent = text || (isLoading ? "同步中..." : "已同步");
      const button = document.getElementById("refreshButton");
      if (button) button.disabled = isLoading;
    }
    function refreshIntervalMs() {
      const control = document.getElementById("refreshInterval");
      return Number(control?.value || autoRefreshIntervalMs || 5000);
    }
    function updateAutoRefreshButton() {
      const button = document.getElementById("autoRefreshButton");
      if (!button) return;
      button.textContent = autoRefreshEnabled ? "自动同步中" : "已暂停自动";
      button.classList.toggle("paused", !autoRefreshEnabled);
    }
    function scheduleAutoRefresh() {
      clearTimeout(autoRefreshTimer);
      updateAutoRefreshButton();
      if (!autoRefreshEnabled) {
        setLoading(false, "自动同步已暂停");
        return;
      }
      autoRefreshTimer = setTimeout(async () => {
        await loadData({ silent: true, fromAuto: true });
        scheduleAutoRefresh();
      }, refreshIntervalMs());
    }
    function toggleAutoRefresh() {
      autoRefreshEnabled = !autoRefreshEnabled;
      localStorage.setItem("blinkAdminAutoRefresh", autoRefreshEnabled ? "on" : "off");
      if (autoRefreshEnabled) {
        loadData({ silent: true });
      }
      scheduleAutoRefresh();
    }
    function canSync() {
      return document.getElementById("key").value.trim().length > 0;
    }

    async function errorText(response) {
      const text = await response.text();
      try {
        const data = JSON.parse(text);
        return data.detail || data.message || text;
      } catch (_) {
        return text || "请求失败";
      }
    }

    function setButtonBusy(button, busy, text = "处理中...") {
      if (!button) return;
      if (busy) {
        button.dataset.label = button.textContent;
        button.textContent = text;
        button.disabled = true;
      } else {
        button.textContent = button.dataset.label || button.textContent;
        button.disabled = false;
      }
    }

    function upsertOrder(updated) {
      const index = state.orders.findIndex(order => order.id === updated.id);
      if (index >= 0) {
        state.orders[index] = updated;
      } else {
        state.orders.unshift(updated);
      }
    }

    function upsertPayment(updated) {
      const index = state.payments.findIndex(payment => payment.id === updated.id);
      if (index >= 0) {
        state.payments[index] = updated;
      } else {
        state.payments.unshift(updated);
      }
    }

    function ensureOrderTables() {
      return {
        cod: document.getElementById("codOrders"),
        prepaid: document.getElementById("codOrders")
      };
    }

    async function loadData(options = {}) {
      const { silent = false, fromAuto = false } = options;
      if (!canSync()) {
        setLoading(false, "输入密码后自动同步");
        return;
      }
      const startedAt = Date.now();
      lastLoadStartedAt = startedAt;
      setLoading(true, silent ? "自动同步中..." : "同步中...");
      try {
        const response = await fetch(`/admin/data?key=${keyParam()}`);
        if (!response.ok) {
          throw new Error(await errorText(response));
        }
        const nextState = await response.json();
        if (startedAt !== lastLoadStartedAt) return;
        const fresh = rememberNewItems(nextState);
        state = nextState;
        hasLoadedOnce = true;
        render();
        showPage(currentPage, true);
        if (activeDetailId && state.orders.some(order => order.id === activeDetailId)) {
          showDetail(activeDetailId);
        }
        const freshCount = fresh.orders + fresh.payments + fresh.accounts + fresh.settlements + fresh.messages;
        if (freshCount) {
          const parts = [];
          if (fresh.orders) parts.push(`${fresh.orders} 个新订单`);
          if (fresh.payments) parts.push(`${fresh.payments} 个新付款`);
          if (fresh.accounts) parts.push(`${fresh.accounts} 个新账号`);
          if (fresh.settlements) parts.push(`${fresh.settlements} 条新结算`);
          if (fresh.messages) parts.push(`${fresh.messages} 条新消息`);
          showToast(parts.join(" / "));
        } else if (!silent) {
          showToast("后台数据已刷新");
        }
        setLoading(false, `已同步 ${new Date().toLocaleTimeString()}`);
      } catch (error) {
        setLoading(false, "同步失败");
        if (!fromAuto || !hasLoadedOnce) showToast(error.message || "请求失败", "error");
      }
    }

    function pendingPaymentRow(payment, prepaid) {
      return `
        <tr${rowClass("payment", payment.id)}>
          <td><strong>订单 #${escapeHtml(payment.id.slice(0, 6).toUpperCase())}</strong><br><span class="pill">${label(payment.payment_mode)}</span><br><span class="muted">${escapeHtml(new Date(payment.created_at).toLocaleString())}</span></td>
          <td><span class="pill">${label(payment.payment_mode)}</span><br>${displayAccount(payment.user_phone)}<br><span class="muted">用户已上传付款截图，等待后台确认后才能下单</span></td>
          <td><span class="pill">${label(payment.status)}</span><br><span class="muted">${label(payment.payment_mode)}</span></td>
          <td>${prepaid ? "送货费" : "配送费"} ${money(payment.amount)}<br><span class="muted">${Number(payment.distance_km || 0).toFixed(1)} km</span></td>
          <td>${payment.payment_proof_url ? `<img src="${escapeHtml(payment.payment_proof_url)}" alt="KPay 转账截图" style="width:84px;height:84px;object-fit:cover;border-radius:8px;background:#f3f4f6;">` : `<span class="muted">无截图</span>`}</td>
          <td>${prepaid && payment.goods_amount ? `骑手押金 ${money(payment.goods_amount)}` : `<span class="muted">订单创建后显示</span>`}</td>
          <td class="address-cell"><span class="muted">后台确认后，用户端才可以点立即下单</span></td>
          <td class="actions-cell">${payment.status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmPrepaidPayment('${payment.id}', null, this)">确认用户付款</button>` : `<span class="pill">已确认</span>`}</td>
        </tr>`;
    }

    function orderTableRow(order, prepaid) {
      return `
        <tr${rowClass("order", order.id)} onclick="showDetail('${order.id}')">
          <td><strong>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</strong><br><span class="pill">${label(order.payment_mode)}</span><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td><span class="pill">${label(order.payment_mode)}</span><br>${displayAccount(order.user_phone, order.user_nickname, order.user_email)}<br>${displayAccount(order.rider_phone, order.rider_nickname || order.rider_name, order.rider_email)}</td>
          <td><span class="pill">${label(order.status)}</span><br><span class="muted">${label(order.payment_mode)} / 用户付款：${label(order.user_payment_status)}</span></td>
          <td>配送费 ${money(order.delivery_fee || order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td>${paymentProofCell(order)}</td>
          <td>${riderDepositLabel(order.rider_deposit_status)}</td>
          <td class="address-cell">${escapeHtml(order.pickup_address)}<br><span class="muted">${escapeHtml(order.dropoff_address)}</span></td>
          <td class="actions-cell">
            ${order.user_payment_status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmUserPayment('${order.id}', this)">${prepaid ? "确认用户付款" : "确认送货费"}</button>` : ""}
            ${order.rider_deposit_status === "pending" ? `<button onclick="event.stopPropagation(); confirmDeposit('${order.id}', this)">确认骑手押金</button>` : ""}
          </td>
        </tr>`;
    }

    function filteredAccounts() {
      const q = document.getElementById("q").value.toLowerCase();
      return sortByDateDesc(
        (state.accounts || []).filter(account => JSON.stringify(account).toLowerCase().includes(q)),
        ["last_login_at"]
      );
    }

    function renderAccountRows(accounts = filteredAccounts()) {
      if (selectedAccountPhone && !accounts.some(account => account.phone === selectedAccountPhone)) {
        selectedAccountPhone = null;
      }
      const accountsTable = document.getElementById("accounts");
      accountsTable.innerHTML = accounts.map(account => `
        <tr class="account-row ${account.phone === selectedAccountPhone ? "active" : ""}" onclick="selectAccount(${jsValue(account.phone)})">
          <td>${account.avatar_url ? `<img src="${escapeHtml(account.avatar_url)}" alt="头像" style="width:44px;height:44px;object-fit:cover;border-radius:50%;background:#f3f4f6;">` : ""}</td>
          <td>${escapeHtml(account.nickname || "")}${appRoleHtml(account.app_role)}</td>
          <td>${account.payment_qr_url ? `<a href="${escapeHtml(account.payment_qr_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()"><img src="${escapeHtml(account.payment_qr_url)}" alt="收款码" title="点击查看收款码" style="width:54px;height:54px;object-fit:cover;border-radius:8px;background:#f3f4f6;border:1px solid #e5e7eb;"></a>` : `<span class="muted">未上传</span>`}</td>
          <td>${accountLoginHtml(account.phone, account.email)}</td>
          <td>${account.last_login_at ? escapeHtml(new Date(account.last_login_at).toLocaleString()) : ""}</td>
        </tr>`).join("");
      if (!accounts.length) {
        accountsTable.innerHTML = `<tr><td colspan="5" class="muted">暂无账号资料</td></tr>`;
      }
    }

    function selectedFilter(id) {
      return document.getElementById(id)?.value || "all";
    }
    function orderMatchesFilters(order) {
      const statusFilter = selectedFilter("statusFilter");
      const paymentFilter = selectedFilter("paymentFilter");
      return (statusFilter === "all" || order.status === statusFilter)
        && (paymentFilter === "all" || order.user_payment_status === paymentFilter || order.rider_deposit_status === paymentFilter);
    }
    function paymentMatchesFilters(payment) {
      const paymentFilter = selectedFilter("paymentFilter");
      const statusFilter = selectedFilter("statusFilter");
      return statusFilter === "all" && (paymentFilter === "all" || payment.status === paymentFilter);
    }
    function renderSummary(orders, payments) {
      const summary = document.getElementById("summary");
      if (!summary) return;
      const pendingOrders = orders.filter(order => order.status === "matching").length;
      const activeOrders = orders.filter(order => ["accepted","picking_up","delivering"].includes(order.status)).length;
      const pendingPayments = payments.filter(payment => payment.status === "pending").length
        + orders.filter(order => order.user_payment_status === "pending" || order.rider_deposit_status === "pending").length;
      const completed = orders.filter(order => order.status === "completed").length;
      const serviceCount = serviceConversations().length;
      summary.innerHTML = `
        <div class="summary-card"><span>待接单</span><strong>${pendingOrders}</strong></div>
        <div class="summary-card"><span>进行中</span><strong>${activeOrders}</strong></div>
        <div class="summary-card"><span>待确认付款/押金</span><strong>${pendingPayments}</strong></div>
        <div class="summary-card"><span>已完成</span><strong>${completed}</strong></div>
        <div class="summary-card"><span>客服会话</span><strong>${serviceCount}</strong></div>
      `;
    }

    function render() {
      const q = document.getElementById("q").value.toLowerCase();
      const orders = sortByDateDesc(state.orders.filter(order => JSON.stringify(order).toLowerCase().includes(q) && orderMatchesFilters(order)));
      const accounts = filteredAccounts();
      const usedPaymentIds = new Set((state.orders || []).map(order => order.kpay_transaction_id).filter(Boolean));
      const isPrepaidPayment = payment => payment.payment_mode === "prepaid";
      const pendingPayments = sortByDateDesc((state.payments || []).filter(payment =>
        !usedPaymentIds.has(payment.id) && JSON.stringify(payment).toLowerCase().includes(q) && paymentMatchesFilters(payment)
      ));
      renderSummary(state.orders || [], state.payments || []);
      const orderRows = sortByDateDesc([
        ...pendingPayments.map(payment => ({ kind: "payment", payment, created_at: payment.created_at })),
        ...orders.map(order => ({ kind: "order", order, created_at: order.created_at }))
      ]);
      const tables = ensureOrderTables();
      const ordersTable = tables.cod;
      if (!ordersTable) {
        console.error("后台订单表格节点缺失，请刷新页面。");
        const activePage = document.querySelector(".page.active");
        if (activePage) {
          activePage.insertAdjacentHTML("beforeend", `<div class="empty">后台页面版本不完整，请重新部署最新 main.py 后刷新。</div>`);
        }
        return;
      }

      ordersTable.innerHTML = orderRows.map(row =>
        row.kind === "payment" ? pendingPaymentRow(row.payment, isPrepaidPayment(row.payment)) : orderTableRow(row.order, row.order.payment_mode === "prepaid")
      ).join("");
      if (!orderRows.length) {
        ordersTable.innerHTML = `<tr><td colspan="8" class="muted">暂无订单</td></tr>`;
      }
      renderAccountRows(accounts);
      renderAccountDetail();
      const settlementRows = sortByDateDesc(orders, [
        "user_settlement_requested_at",
        "rider_settlement_requested_at",
        "user_settlement_paid_at",
        "rider_settlement_paid_at",
        "created_at"
      ]);
      document.getElementById("settlements").innerHTML = settlementRows.map(order => `
        <tr>
          <td><strong>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td>${displayAccount(order.user_phone, order.user_nickname, order.user_email)}<br>${displayAccount(order.rider_phone, order.rider_nickname || order.rider_name, order.rider_email)}</td>
          <td>${deliveryFeeCell(order)}</td>
          <td>${settlementInfo(order)}</td>
          <td>${settlementQRCodes(order)}</td>
          <td><span class="pill">${label(order.settlement_status)}</span>${settlementPaidTimes(order)}</td>
          <td>
            ${order.payment_mode === "cod" && order.user_settlement_qr_url && !["paid_to_user","completed"].includes(order.settlement_status) ? `<button onclick="confirmUserSettlement('${order.id}', this)">确认已转账货费</button>` : ""}
            ${order.rider_settlement_qr_url && !["paid_to_rider","completed"].includes(order.settlement_status) ? `<button onclick="confirmRiderSettlement('${order.id}', this)">确认已转账送货费</button>` : ""}
          </td>
        </tr>`).join("");
      if (!settlementRows.length) {
        document.getElementById("settlements").innerHTML = `<tr><td colspan="7" class="muted">暂无结算记录</td></tr>`;
      }
      renderServiceChat();
    }

    function selectAccount(phone) {
      selectedAccountPhone = phone;
      selectedAccountPanel = "placed";
      renderAccountRows();
      renderAccountDetail();
    }

    function closeAccountModal() {
      selectedAccountPhone = null;
      renderAccountRows();
      renderAccountDetail();
    }

    function selectAccountPanel(panel) {
      selectedAccountPanel = panel;
      renderAccountDetail();
    }

    function orderShortCode(order) {
      return `#${escapeHtml(order.id.slice(0, 6).toUpperCase())}`;
    }

    function accountOrderRow(order, roleText) {
      return `
        <tr onclick="showDetail('${order.id}')">
          <td><strong>${orderShortCode(order)}</strong><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(roleText)}</td>
          <td><span class="pill">${label(order.status)}</span><br><span class="muted">${label(order.payment_mode)}</span></td>
          <td>配送费 ${money(order.delivery_fee || order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td class="address-cell">${escapeHtml(order.pickup_address)}<br><span class="muted">${escapeHtml(order.dropoff_address)}</span></td>
        </tr>`;
    }

    function accountOrdersPanel(title, orders, roleText) {
      const rows = sortByDateDesc(orders).map(order => accountOrderRow(order, roleText)).join("");
      return `
        <section class="account-panel">
          <h3>${escapeHtml(title)} (${orders.length})</h3>
          <table class="mini-table">
            <thead><tr><th>订单</th><th>身份</th><th>状态</th><th>金额</th><th>地址</th></tr></thead>
            <tbody>${rows || `<tr><td colspan="5" class="muted">暂无记录</td></tr>`}</tbody>
          </table>
        </section>`;
    }

    function accountConversationTitle(conversationId, phone) {
      if (conversationId === `account:${phone}`.toLowerCase()) {
        return "Customer Service";
      }
      if (conversationId.startsWith("order:")) {
        const orderId = conversationId.slice("order:".length);
        const order = state.orders.find(item => item.id.toLowerCase() === orderId.toLowerCase());
        if (order) {
          const otherSide = order.user_phone === phone
            ? (accountName(order.rider_phone, order.rider_nickname || order.rider_name) || "未接单骑手")
            : (accountName(order.user_phone, order.user_nickname) || accountContact(order.user_phone, order.user_email));
          return `订单 ${order.id.slice(0, 6).toUpperCase()} / 对方：${otherSide}`;
        }
      }
      return conversationId;
    }

    function serviceConversationTitle(conversationId) {
      const raw = String(conversationId || "");
      if (raw.toLowerCase().startsWith("account:")) {
        const phone = raw.slice("account:".length);
        const name = accountName(phone);
        const contact = accountContact(phone);
        return name && contact ? `${name} / ${contact}` : (name || contact || "账号会话");
      }
      if (raw.toLowerCase().startsWith("order:")) {
        return `订单 ${raw.slice("order:".length, "order:".length + 6).toUpperCase()}`;
      }
      return raw;
    }

    function accountChatThreads(phone, relatedOrders) {
      const conversationIds = new Set([`account:${phone}`.toLowerCase()]);
      relatedOrders.forEach(order => conversationIds.add(`order:${order.id}`.toLowerCase()));
      const relatedMessages = (state.messages || []).filter(message => {
        const conversationId = String(message.conversation_id || "").toLowerCase();
        return message.sender_phone === phone || conversationIds.has(conversationId);
      });
      const grouped = new Map();
      relatedMessages.forEach(message => {
        const conversationId = String(message.conversation_id || "").toLowerCase();
        if (!grouped.has(conversationId)) grouped.set(conversationId, []);
        grouped.get(conversationId).push(message);
      });
      return Array.from(grouped.entries())
        .map(([conversationId, messages]) => ({
          conversationId,
          messages: sortByDateAsc(messages),
          latestAt: Math.max(...messages.map(message => dateMs(message.created_at)))
        }))
        .sort((a, b) => b.latestAt - a.latestAt);
    }

    function accountChatSection(phone, relatedOrders) {
      const threads = accountChatThreads(phone, relatedOrders);
      const content = threads.map(thread => `
        <div class="chat-thread">
          <div class="chat-thread-title">
            <span>${escapeHtml(accountConversationTitle(thread.conversationId, phone))}</span>
            <span class="muted">${escapeHtml(new Date(thread.latestAt).toLocaleString())}</span>
          </div>
          ${thread.messages.map(message => `
            <p class="chat-line">
              <strong>${escapeHtml(accountName(message.sender_phone, message.sender_name) || message.sender_name)}</strong>
              <span class="muted">${escapeHtml(accountContact(message.sender_phone))} ${escapeHtml(new Date(message.created_at).toLocaleString())}</span><br>
              ${escapeHtml(message.text || "")}
              ${message.image_url ? `<br><img src="${escapeHtml(message.image_url)}" alt="聊天图片">` : ""}
            </p>
          `).join("")}
        </div>
      `).join("");
      return `
        <section class="account-panel">
          <h3>聊天记录 (${threads.length})</h3>
          ${content || `<div class="empty">暂无相关聊天记录</div>`}
        </section>`;
    }

    function accountTabButton(panel, text, count) {
      return `<button class="account-tab ${selectedAccountPanel === panel ? "active" : ""}" onclick="selectAccountPanel('${panel}')">${escapeHtml(text)} (${count})</button>`;
    }

    function renderAccountDetail() {
      const container = document.getElementById("accountDetail");
      const modal = document.getElementById("accountModal");
      if (!container) return;
      if (!selectedAccountPhone) {
        if (modal) modal.classList.remove("show");
        container.innerHTML = "";
        return;
      }
      const account = (state.accounts || []).find(item => item.phone === selectedAccountPhone);
      if (modal) modal.classList.add("show");
      const placedOrders = (state.orders || []).filter(order => order.user_phone === selectedAccountPhone);
      const acceptedOrders = (state.orders || []).filter(order => order.rider_phone === selectedAccountPhone);
      const relatedOrders = Array.from(new Map([...placedOrders, ...acceptedOrders].map(order => [order.id, order])).values());
      const chatThreads = accountChatThreads(selectedAccountPhone, relatedOrders);
      const panelHtml = selectedAccountPanel === "accepted"
        ? accountOrdersPanel("他接的单", acceptedOrders, "骑手")
        : selectedAccountPanel === "chat"
          ? accountChatSection(selectedAccountPhone, relatedOrders)
          : accountOrdersPanel("他下的单", placedOrders, "发货人/用户");
      container.innerHTML = `
        <section>
          <div class="row"><b>登录邮箱</b><span>${escapeHtml(accountLoginLabel(selectedAccountPhone, account?.email || ""))}</span></div>
          <div class="row"><b>邮箱</b><span>${accountEmail(selectedAccountPhone, account?.email || "") ? escapeHtml(accountEmail(selectedAccountPhone, account?.email || "")) : "未绑定"}</span></div>
          <div class="row"><b>昵称</b><span>${escapeHtml(account?.nickname || "")}${appRoleHtml(account?.app_role)}</span></div>
          <div class="row"><b>最近登录</b><span>${account?.last_login_at ? escapeHtml(new Date(account.last_login_at).toLocaleString()) : ""}</span></div>
        </section>
        <section>
          <div class="account-tabs">
            ${accountTabButton("placed", "他下的单", placedOrders.length)}
            ${accountTabButton("accepted", "他接的单", acceptedOrders.length)}
            ${accountTabButton("chat", "聊天记录", chatThreads.length)}
          </div>
        </section>
        ${panelHtml}
      `;
    }

    function serviceConversations() {
      const grouped = new Map();
      state.messages.forEach(message => {
        const existing = grouped.get(message.conversation_id);
        if (!existing || new Date(message.created_at) > new Date(existing.created_at)) {
          grouped.set(message.conversation_id, message);
        }
      });
      return sortByDateDesc(Array.from(grouped.values()));
    }

    function selectServiceConversation(conversationId) {
      selectedServiceConversationId = conversationId;
      renderServiceChat();
    }

    function renderServiceChat() {
      const conversations = serviceConversations();
      if (!selectedServiceConversationId && conversations.length) {
        selectedServiceConversationId = conversations[0].conversation_id;
      }

      const list = document.getElementById("chatConversations");
      const chat = document.getElementById("chat");
      const title = document.getElementById("chatTitle");
      if (!list || !chat || !title) return;

      list.innerHTML = conversations.map(message => `
        <button class="conversation-row ${message.conversation_id === selectedServiceConversationId ? "active" : ""}" onclick="selectServiceConversation('${escapeHtml(message.conversation_id)}')">
          <strong>${escapeHtml(serviceConversationTitle(message.conversation_id))}</strong><br>
          <span class="muted">${escapeHtml(accountName(message.sender_phone, message.sender_name) || message.sender_name)}：${escapeHtml(message.text || "[图片]")}</span><br>
          <span class="muted">${escapeHtml(new Date(message.created_at).toLocaleString())}</span>
        </button>
      `).join("");

      if (!conversations.length) {
        list.innerHTML = `<div class="empty">暂无 Customer Service 会话</div>`;
        chat.innerHTML = `<div class="empty">暂无 Customer Service 消息</div>`;
        title.textContent = "聊天记录";
        return;
      }

      const messages = state.messages
        .filter(message => message.conversation_id === selectedServiceConversationId)
        .sort((a, b) => dateMs(a.created_at) - dateMs(b.created_at));
      title.textContent = selectedServiceConversationId ? serviceConversationTitle(selectedServiceConversationId) : "聊天记录";
      chat.innerHTML = messages.map(message => `
        <p>
          <strong>${escapeHtml(accountName(message.sender_phone, message.sender_name) || message.sender_name)}</strong>
          <span class="muted">${escapeHtml(accountContact(message.sender_phone))} ${escapeHtml(new Date(message.created_at).toLocaleString())}</span><br>
          ${escapeHtml(message.text)}
          ${message.image_url ? `<br><img src="${escapeHtml(message.image_url)}" alt="聊天图片">` : ""}
        </p>
      `).join("");
      chat.scrollTop = chat.scrollHeight;
    }

    async function sendServiceReply(button = null) {
      const input = document.getElementById("serviceReply");
      const imageInput = document.getElementById("serviceImage");
      const text = input.value.trim();
      const imageFile = imageInput?.files?.[0] || null;
      if (!selectedServiceConversationId) {
        showToast("请先选择一个 Customer Service 会话", "error");
        return;
      }
      if (!text && !imageFile) {
        showToast("请输入回复内容或选择图片", "error");
        return;
      }

      setButtonBusy(button, true, "发送中...");
      try {
        let imageData = null;
        let imageContentType = null;
        let imageFileName = null;
        if (imageFile) {
          imageData = await readImageAsBase64(imageFile);
          imageContentType = imageFile.type || "image/jpeg";
          imageFileName = imageFile.name || `admin-chat-${Date.now()}.jpg`;
        }

        const response = await fetch(`/admin/chat/messages?key=${keyParam()}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            conversation_id: selectedServiceConversationId,
            text,
            image_data: imageData,
            image_content_type: imageContentType,
            image_file_name: imageFileName
          })
        });
        if (!response.ok) {
          throw new Error(await errorText(response));
        }
        const message = await response.json();
        state.messages.push(message);
        input.value = "";
        if (imageInput) imageInput.value = "";
        renderServiceChat();
        showToast("消息已发送");
        loadData({ silent: true });
      } catch (error) {
        showToast(error.message || "发送失败", "error");
      } finally {
        setButtonBusy(button, false);
      }
    }

    function readImageAsBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const value = String(reader.result || "");
          resolve(value.includes(",") ? value.split(",").pop() : value);
        };
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
    }

    function showDetail(id) {
      const order = state.orders.find(item => item.id === id);
      if (!order) return;
      activeDetailId = id;
      document.getElementById("detailSection").classList.remove("hidden");
      document.getElementById("detail").innerHTML = `
        ${order.goods_image_url ? `<img src="${order.goods_image_url}" alt="商品图">` : `<div class="muted">暂无商品图</div>`}
        <div class="row"><b>订单号</b><span>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</span></div>
        <div class="row"><b>用户</b><span>${displayAccount(order.user_phone, order.user_nickname, order.user_email)}</span></div>
        <div class="row"><b>骑手</b><span>${displayAccount(order.rider_phone, order.rider_nickname || order.rider_name, order.rider_email)}</span></div>
        <div class="row"><b>付款方式</b><span>${escapeHtml(label(order.payment_mode))}</span></div>
        <div class="row"><b>用户付款</b><span>${escapeHtml(label(order.user_payment_status))}</span></div>
        <div class="row"><b>骑手押金</b><span>${escapeHtml(riderDepositLabel(order.rider_deposit_status))}</span></div>
        ${order.payment_proof_url ? `<div class="row"><b>KPay 截图</b><span><img src="${order.payment_proof_url}" alt="KPay 转账截图"></span></div>` : ""}
        <div class="row"><b>取件</b><span>${escapeHtml(order.pickup_address)}</span></div>
        <div class="row"><b>收货</b><span>${escapeHtml(order.dropoff_address)}</span></div>
        <div class="row"><b>备注</b><span>${escapeHtml(order.note || "")}</span></div>
        <div class="actions">
          <select id="status">${optionHtml(statusOptions, order.status)}</select>
          <select id="userPayment">${optionHtml(paymentOptions, order.user_payment_status)}</select>
          <select id="riderDeposit">${optionHtml(paymentOptions, order.rider_deposit_status)}</select>
          <select id="settlement">${optionHtml(settlementOptions, order.settlement_status)}</select>
        </div>
        ${order.user_payment_status !== "confirmed" ? `<button onclick="confirmUserPayment('${order.id}', this)">确认收到送货费</button>` : ""}
        <button onclick="saveOrder('${order.id}', this)">保存订单状态</button>
      `;
    }

    function settlementPaidTimes(order) {
      const rows = [];
      if (order.user_settlement_paid_at) {
        rows.push(`货费：${new Date(order.user_settlement_paid_at).toLocaleString()}`);
      }
      if (order.rider_settlement_paid_at) {
        rows.push(`送货费：${new Date(order.rider_settlement_paid_at).toLocaleString()}`);
      }
      return rows.length ? `<br><span class="muted">${escapeHtml(rows.join(" / "))}</span>` : "";
    }

    function paymentProofCell(order) {
      const image = order.payment_proof_url
        ? `<img src="${escapeHtml(order.payment_proof_url)}" alt="KPay 转账截图" style="width:84px;height:84px;object-fit:cover;border-radius:8px;background:#f3f4f6;">`
        : `<span class="muted">无截图</span>`;
      const paymentId = order.kpay_transaction_id
        ? `<br><span class="muted">#${escapeHtml(order.kpay_transaction_id.slice(0, 6).toUpperCase())}</span>`
        : "";
      return `${image}<br><span class="pill">${label(order.user_payment_status)}</span>${paymentId}`;
    }

    function settlementInfo(order) {
      if (order.payment_mode === "cod") {
        return [
          `货费收款：${escapeHtml(order.user_settlement_name || "未提交")}${order.user_settlement_requested_at ? `<br><span class="muted">提醒：${escapeHtml(new Date(order.user_settlement_requested_at).toLocaleString())}</span>` : ""}`,
          `送货费收款：${escapeHtml(order.rider_settlement_name || "未提交")}${order.rider_settlement_requested_at ? `<br><span class="muted">提醒：${escapeHtml(new Date(order.rider_settlement_requested_at).toLocaleString())}</span>` : ""}`
        ].join("<br>");
      }
      return `骑手收款：${escapeHtml(order.rider_settlement_name || "未提交")}${order.rider_settlement_requested_at ? `<br><span class="muted">提醒：${escapeHtml(new Date(order.rider_settlement_requested_at).toLocaleString())}</span>` : ""}`;
    }

    function settlementQRCodes(order) {
      const images = [];
      if (order.payment_mode === "cod" && order.user_settlement_qr_url) {
        images.push(`<div><span class="muted">货费</span><br><img class="thumb" src="${escapeHtml(order.user_settlement_qr_url)}" alt="用户收款二维码"></div>`);
      }
      if (order.rider_settlement_qr_url) {
        images.push(`<div><span class="muted">送货费</span><br><img class="thumb" src="${escapeHtml(order.rider_settlement_qr_url)}" alt="骑手收款二维码"></div>`);
      }
      return images.length ? images.join("") : `<span class="muted">未提交</span>`;
    }

    async function patchOrder(id, body, button, successMessage) {
      setButtonBusy(button, true);
      try {
        const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        if (!response.ok) {
          throw new Error(await errorText(response));
        }
        const updated = await response.json();
        upsertOrder(updated);
        render();
        if (activeDetailId === id) showDetail(id);
        showToast(successMessage);
        loadData({ silent: true });
        return updated;
      } catch (error) {
        showToast(error.message || "请求失败", "error");
        return null;
      } finally {
        setButtonBusy(button, false);
      }
    }

    async function confirmUserPayment(id, button = null) {
      const order = state.orders.find(item => item.id === id);
      if (order?.kpay_transaction_id) {
        await confirmPrepaidPayment(order.kpay_transaction_id, id, button);
        return;
      }

      await patchOrder(id, { user_payment_status: "confirmed" }, button, "已确认收到送货费");
    }

    async function confirmPrepaidPayment(id, orderId = null, button = null) {
      setButtonBusy(button, true);
      try {
        const response = await fetch(`/admin/payments/${id}?key=${keyParam()}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "confirmed" })
        });
        if (!response.ok) {
          throw new Error(await errorText(response));
        }
        const updated = await response.json();
        upsertPayment(updated);
        if (orderId) {
          const order = state.orders.find(item => item.id === orderId);
          if (order) order.user_payment_status = "confirmed";
        }
        render();
        if (orderId && activeDetailId === orderId) showDetail(orderId);
        showToast("已确认用户付款");
        loadData({ silent: true });
      } catch (error) {
        showToast(error.message || "请求失败", "error");
      } finally {
        setButtonBusy(button, false);
      }
    }

    async function confirmDeposit(id, button = null) {
      await patchOrder(id, { rider_deposit_status: "confirmed" }, button, "已确认骑手押金");
    }

    async function saveOrder(id, button = null) {
      const body = {
        status: document.getElementById("status").value,
        user_payment_status: document.getElementById("userPayment").value,
        rider_deposit_status: document.getElementById("riderDeposit").value,
        settlement_status: document.getElementById("settlement").value
      };
      await patchOrder(id, body, button, "订单状态已保存");
    }

    async function saveSettlement(id, button = null) {
      await patchOrder(id, { settlement_status: document.getElementById(`settlement-${id}`).value }, button, "结算状态已保存");
    }

    async function confirmRiderSettlement(id, button = null) {
      const order = state.orders.find(item => item.id === id);
      const status = order?.payment_mode === "cod" && order?.settlement_status === "paid_to_user" ? "completed" : "paid_to_rider";
      await patchOrder(id, { settlement_status: status }, button, "已确认转账给骑手");
    }

    async function confirmUserSettlement(id, button = null) {
      const order = state.orders.find(item => item.id === id);
      const status = order?.settlement_status === "paid_to_rider" ? "completed" : "paid_to_user";
      await patchOrder(id, { settlement_status: status }, button, "已确认转账给用户");
    }

    document.getElementById("q").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(render, 120);
    });
    document.getElementById("statusFilter").addEventListener("change", render);
    document.getElementById("paymentFilter").addEventListener("change", render);
    document.getElementById("refreshInterval").addEventListener("change", event => {
      autoRefreshIntervalMs = Number(event.target.value || 5000);
      localStorage.setItem("blinkAdminRefreshMs", String(autoRefreshIntervalMs));
      scheduleAutoRefresh();
    });
    document.getElementById("key").addEventListener("input", () => {
      clearTimeout(keyTimer);
      keyTimer = setTimeout(() => {
        if (canSync()) loadData({ silent: true });
      }, 450);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && selectedAccountPhone) closeAccountModal();
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && autoRefreshEnabled) loadData({ silent: true, fromAuto: true });
    });
    const intervalControl = document.getElementById("refreshInterval");
    if (intervalControl) {
      intervalControl.value = String(autoRefreshIntervalMs);
      if (intervalControl.value !== String(autoRefreshIntervalMs)) {
        intervalControl.value = "5000";
        autoRefreshIntervalMs = 5000;
      }
    }
    showPage(currentPage);
    renderSummary([], []);
    scheduleAutoRefresh();
  </script>
</body>
</html>
'''


def parse_coordinate(text: str) -> tuple[float, float] | None:
    text = decoded_google_maps_text(text)
    reliable_coordinate = parse_reliable_google_maps_coordinate(text)
    if reliable_coordinate:
        return reliable_coordinate

    lite_coordinate = parse_google_lite_coordinate(text)
    if lite_coordinate:
        return lite_coordinate

    bare_match = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*", text)
    if bare_match:
        lat = float(bare_match.group(1))
        lng = float(bare_match.group(2))
        if is_valid_coordinate(lat, lng):
            return lat, lng

    return None


def parse_google_lite_coordinate(text: str) -> tuple[float, float] | None:
    text = decoded_google_maps_text(text)
    values = [float(value) for value in re.findall(r"-?\d{1,6}\.\d{4,}", text)]
    for first, second in zip(values, values[1:]):
        if is_likely_service_coordinate(first, second):
            return first, second
        if is_likely_service_coordinate(second, first):
            return second, first
    return None


def parse_reliable_google_maps_coordinate(text: str) -> tuple[float, float] | None:
    text = decoded_google_maps_text(text)
    patterns = [
        (r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"!2d(-?\d{1,3}(?:\.\d+)?)!3d(-?\d{1,2}(?:\.\d+)?)", 2, 1),
        (r"q=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"ll=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"center=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"query=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"destination=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"daddr=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
    ]
    for pattern, lat_group, lng_group in patterns:
        match = re.search(pattern, text)
        if match:
            lat = float(match.group(lat_group))
            lng = float(match.group(lng_group))
            if is_valid_coordinate(lat, lng):
                return lat, lng

    return None


def is_valid_coordinate(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180


def is_likely_service_coordinate(lat: float, lng: float) -> bool:
    return is_valid_coordinate(lat, lng) and 9 <= lat <= 29 and 92 <= lng <= 102


def is_google_maps_short_link(text: str) -> bool:
    lowered = text.lower()
    return "maps.app.goo.gl" in lowered or "goo.gl/maps" in lowered or "maps.app.goo.gl/" in lowered


def google_maps_url_text(text: str) -> str | None:
    text = decoded_google_maps_text(text)
    patterns = [
        r"https?://(?:www\.)?google\.[^\"'\s<>]+/maps[^\"'\s<>]*",
        r"https?://maps\.google\.[^\"'\s<>]+/maps[^\"'\s<>]*",
        r"https?://maps\.google\.[^\"'\s<>]+/[^\"'\s<>]*",
        r"https?://(?:www\.)?google\.[^\"'\s<>]+/search[^\"'\s<>]*",
        r"/maps\?[^\"'\s<>]+",
        r"https?://maps\.app\.goo\.gl/[^\"'\s<>]+",
        r"maps\.app\.goo\.gl/[^\"'\s<>]+",
        r"https?://goo\.gl/maps/[^\"'\s<>]+",
        r"goo\.gl/maps/[^\"'\s<>]+",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(0).replace("&amp;", "&").strip().rstrip(".,;)")
            if value.startswith(("/maps", "/search")):
                value = f"https://www.google.com{value}"
            elif value.startswith(("maps.app.goo.gl/", "goo.gl/maps/")):
                value = f"https://{value}"
            return value
    return None


def decoded_google_maps_text(text: str) -> str:
    decoded = (
        text.replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\x3d", "=")
        .replace("\\x26", "&")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    return unquote(decoded)


async def expand_location_text(text: str) -> str:
    if not is_google_maps_short_link(text):
        return text

    url_text = google_maps_url_text(text) or text.strip()
    if "://" not in url_text:
        url_text = f"https://{url_text}"

    user_agents = [
        (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    ]

    async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
        for user_agent in user_agents:
            response = await client.get(
                url_text,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            expanded_url = str(response.url)
            decoded_html = decoded_google_maps_text(response.text)
            if parse_coordinate(decoded_html):
                return decoded_html

            html_url = google_maps_url_text(decoded_html)
            if html_url and not is_google_maps_short_link(html_url):
                return html_url

            if not is_google_maps_short_link(expanded_url):
                return expanded_url

        return text


def google_maps_query_text(text: str) -> str:
    text = decoded_google_maps_text(text)
    parsed = urlparse(text)
    if "google.com" not in parsed.netloc and "maps.google" not in parsed.netloc:
        return text

    params = parse_qs(parsed.query)
    for key in ("q", "query", "destination", "daddr"):
        value = params.get(key, [""])[0].strip()
        if value:
            return value

    center = params.get("center", [""])[0].strip()
    if center:
        return center

    place_match = re.search(r"/(?:maps/)?place/([^?#]+?)(?:/data=|/[@?]|$)", parsed.path, re.IGNORECASE)
    if place_match:
        place_text = unquote(place_match.group(1)).replace("+", " ").strip()
        if place_text:
            return place_text

    return text


async def geocode_location(text: str) -> tuple[float, float]:
    expanded = await expand_location_text(text.strip())

    coordinate = parse_coordinate(expanded)
    if coordinate:
        return coordinate

    query_text = google_maps_query_text(expanded)
    coordinate = parse_coordinate(query_text)
    if coordinate:
        return coordinate

    if is_google_maps_short_link(query_text):
        short_url = google_maps_url_text(query_text) or query_text
        query_text = short_url.rsplit("/", 1)[-1].strip()

    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query_text, "key": api_key},
        )
        payload = response.json()

    if payload.get("status") != "OK" or not payload.get("results"):
        nominatim_coordinate = await nominatim_geocode_location(query_text)
        if nominatim_coordinate:
            return nominatim_coordinate
        raise HTTPException(
            status_code=400,
            detail=f"Google Map Location 不正确或无法解析：{payload.get('status', 'UNKNOWN')}",
        )

    location = payload["results"][0]["geometry"]["location"]
    return float(location["lat"]), float(location["lng"])


async def nominatim_geocode_location(text: str) -> tuple[float, float] | None:
    queries = nominatim_query_candidates(text)
    if not queries:
        return None

    async with httpx.AsyncClient(timeout=12) as client:
        for query in queries:
            try:
                response = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": query, "format": "jsonv2", "limit": "1"},
                    headers={"User-Agent": "BlinkCourier/1.0 support@blink.local"},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as error:
                logger.warning("Nominatim geocode failed for %s: %s", query, error)
                continue

            if not payload:
                continue

            try:
                return float(payload[0]["lat"]), float(payload[0]["lon"])
            except (KeyError, TypeError, ValueError):
                continue

    return None


def nominatim_query_candidates(text: str) -> list[str]:
    query = text.strip()
    if not query:
        return []

    candidates: list[str] = []

    def add_candidate(value: str) -> None:
        value = value.strip()
        if not value:
            return
        normalized = value if re.search(r"\bMyanmar\b", value, re.IGNORECASE) else f"{value}, Myanmar"
        for candidate in (normalized, re.sub("Centre", "Center", normalized, flags=re.IGNORECASE)):
            if candidate not in candidates:
                candidates.append(candidate)

    add_candidate(query)
    primary_name = query.split(",", 1)[0].strip()
    if primary_name and primary_name != query:
        add_candidate(f"{primary_name}, Yangon" if re.search(r"\bYangon\b", query, re.IGNORECASE) else primary_name)

    return candidates


def haversine_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    lat1, lon1 = origin
    lat2, lon2 = destination
    radius_km = 6371.0

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


async def route_distance_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    distance_km, _ = await route_distance_estimate(origin, destination)
    return distance_km


async def route_distance_estimate(origin: tuple[float, float], destination: tuple[float, float]) -> tuple[float, str | None]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY is not configured")

    directions_estimate = await google_directions_estimate(origin, destination, api_key)
    if directions_estimate is not None:
        distance_km, route_polyline = directions_estimate
        return normalized_google_route_distance_km(distance_km), route_polyline

    matrix_distance = await google_distance_matrix_km(origin, destination, api_key)
    if matrix_distance is not None:
        return normalized_google_route_distance_km(matrix_distance), None

    fallback_distance = haversine_km(origin, destination) * 1.3
    logger.warning(
        "Google route unavailable; using fallback distance %.2f km for %s -> %s",
        fallback_distance,
        origin,
        destination,
    )
    return normalized_google_route_distance_km(fallback_distance), None


def normalized_google_route_distance_km(route_km: float) -> float:
    if 0 < route_km < 0.1:
        return 0.1
    return route_km


async def google_directions_distance_km(
    origin: tuple[float, float],
    destination: tuple[float, float],
    api_key: str,
) -> float | None:
    estimate = await google_directions_estimate(origin, destination, api_key)
    if estimate is None:
        return None
    distance_km, _ = estimate
    return distance_km


async def google_directions_estimate(
    origin: tuple[float, float],
    destination: tuple[float, float],
    api_key: str,
) -> tuple[float, str | None] | None:
    origin_value = f"{origin[0]},{origin[1]}"
    destination_value = f"{destination[0]},{destination[1]}"

    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={
                "origin": origin_value,
                "destination": destination_value,
                "mode": "driving",
                "units": "metric",
                "key": api_key,
            },
        )
        payload = response.json()

    status = payload.get("status", "UNKNOWN")
    if status != "OK":
        logger.warning("Google Directions failed: %s", payload)
        return None

    try:
        route = payload["routes"][0]
        legs = route["legs"]
        meters = sum(float(leg["distance"]["value"]) for leg in legs)
        route_polyline = route.get("overview_polyline", {}).get("points")
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("Google Directions malformed response: %s", payload)
        return None

    return meters / 1000, route_polyline


async def google_distance_matrix_km(
    origin: tuple[float, float],
    destination: tuple[float, float],
    api_key: str,
) -> float | None:
    origin_value = f"{origin[0]},{origin[1]}"
    destination_value = f"{destination[0]},{destination[1]}"

    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": origin_value,
                "destinations": destination_value,
                "mode": "driving",
                "units": "metric",
                "key": api_key,
            },
        )
        payload = response.json()

    top_status = payload.get("status", "UNKNOWN")
    if top_status != "OK":
        logger.warning("Google Distance Matrix failed before rows: %s", payload)
        return None

    try:
        element = payload["rows"][0]["elements"][0]
    except (KeyError, IndexError, TypeError):
        logger.warning("Google Distance Matrix malformed response: %s", payload)
        return None

    if element.get("status") != "OK":
        logger.warning("Google Distance Matrix failed: %s", payload)
        return None

    meters = element["distance"]["value"]
    return float(meters) / 1000

def chat_message_from_row(row: sqlite3.Row) -> ChatMessageResponse:
    sender_phone = row["sender_phone"]
    sender_name = row["sender_name"]
    if row["sender_type"] == "admin":
        sender_name = ADMIN_CHAT_SENDER_NAME
    else:
        sender_name = account_nickname(sender_phone) or sender_name
    return ChatMessageResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        text=row["text"],
        sender_type=row["sender_type"],
        sender_name=sender_name,
        sender_phone=sender_phone,
        image_url=signed_gcs_read_url(row["image_url"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


PRIVACY_POLICY_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Blink Delivery Privacy Policy</title>
  <style>
    :root {
      color-scheme: light;
      --text: #172033;
      --muted: #667085;
      --line: #e5e7eb;
      --brand: #0f6bff;
      --bg: #f7f9fc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
    }
    main {
      max-width: 860px;
      margin: 0 auto;
      padding: 48px 20px 72px;
    }
    .page {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 34px;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(30px, 5vw, 46px);
      line-height: 1.1;
    }
    h2 {
      margin-top: 30px;
      font-size: 21px;
    }
    p, li { color: var(--muted); }
    a { color: var(--brand); }
    .updated {
      margin-top: 0;
      color: var(--muted);
    }
    ul { padding-left: 22px; }
  </style>
</head>
<body>
  <main>
    <article class="page">
      <h1>Blink Delivery Privacy Policy</h1>
      <p class="updated">Last updated: July 12, 2026</p>

      <p>
        Blink Delivery ("Blink", "we", "our", or "us") provides courier ordering,
        delivery tracking, customer support, and settlement features for users in Myanmar.
        This Privacy Policy explains how we collect, use, share, and protect information
        when you use the Blink Delivery mobile app and related services.
      </p>

      <h2>1. Information We Collect</h2>
      <p>We may collect the following information when you use Blink Delivery:</p>
      <ul>
        <li>Account information, such as your phone number, username, profile photo, and payment QR image.</li>
        <li>Delivery information, including sender and receiver names, phone numbers, pickup and drop-off addresses, city, township, building, street, notes, and map links.</li>
        <li>Order information, such as item type, item value, delivery fee, order status, rider assignment, and delivery history.</li>
        <li>Photos and uploaded content, such as parcel photos, payment screenshots, profile images, and chat images.</li>
        <li>Messages sent through customer support or order chat.</li>
        <li>Device and usage information needed to operate the app, troubleshoot problems, and improve service quality.</li>
      </ul>

      <h2>2. How We Use Information</h2>
      <p>We use information to:</p>
      <ul>
        <li>Create and manage delivery orders.</li>
        <li>Calculate delivery distance and estimated delivery fees.</li>
        <li>Match orders with riders and show delivery status updates.</li>
        <li>Process payment confirmation and settlement requests.</li>
        <li>Provide customer support and order chat.</li>
        <li>Send important notifications about orders, payments, rider updates, and account activity.</li>
        <li>Protect our users, riders, business, and platform from fraud, misuse, and operational errors.</li>
      </ul>

      <h2>3. Location and Address Information</h2>
      <p>
        Blink Delivery uses addresses, city, township, and optional Google Map links to
        calculate route distance, estimate fees, and help riders complete deliveries.
        We do not use location information for advertising.
      </p>

      <h2>4. Photos and Uploaded Content</h2>
      <p>
        Uploaded photos may be stored so the platform can verify items, confirm payments,
        update profiles, support settlement requests, and help resolve order issues.
      </p>

      <h2>5. Sharing of Information</h2>
      <p>We share information only as needed to provide the service, including:</p>
      <ul>
        <li>With riders, so they can pick up and deliver orders.</li>
        <li>With platform administrators, so they can manage orders, payments, support, and settlement.</li>
        <li>With service providers that help us host, store, secure, and operate the app.</li>
        <li>When required by law or to protect the safety, rights, and security of users, riders, or Blink.</li>
      </ul>
      <p>We do not sell personal information.</p>

      <h2>6. Payments</h2>
      <p>
        Blink Delivery may collect payment screenshots, payment status, settlement names,
        and payment QR images to confirm delivery fee payments and complete settlement.
        We do not store full bank card details in the app.
      </p>

      <h2>7. Notifications</h2>
      <p>
        If you allow notifications, Blink Delivery may send alerts about order status,
        payment confirmation, rider updates, chat messages, and service notices. You can
        manage notification permission in your device settings.
      </p>

      <h2>8. Data Storage and Security</h2>
      <p>
        We use reasonable technical and organizational measures to protect information.
        No method of transmission or storage is completely secure, but we work to keep
        user information protected and accessible only for legitimate service purposes.
      </p>

      <h2>9. Data Retention</h2>
      <p>
        We keep information for as long as needed to provide delivery services, maintain
        order records, support users, comply with legal obligations, resolve disputes,
        and improve platform operations.
      </p>

      <h2>10. Children&apos;s Privacy</h2>
      <p>
        Blink Delivery is not intended for children under 13. We do not knowingly collect
        personal information from children under 13.
      </p>

      <h2>11. Your Choices</h2>
      <p>
        You may update certain account information in the app. You may also contact us to
        request help with your account, privacy questions, or data-related requests.
      </p>

      <h2>12. Changes to This Policy</h2>
      <p>
        We may update this Privacy Policy from time to time. When we make changes, we will
        update the "Last updated" date above.
      </p>

      <h2>13. Contact Us</h2>
      <p>
        If you have questions about this Privacy Policy or need support, send us a
        WhatsApp message at <a href="https://wa.me/959424594930">+95 942 459 4930</a>.
      </p>
    </article>
  </main>
</body>
</html>
"""

ACCOUNT_DELETION_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Blink Delivery Account Deletion</title>
  <style>
    :root {
      color-scheme: light;
      --text: #172033;
      --muted: #667085;
      --line: #e5e7eb;
      --brand: #0f6bff;
      --bg: #f7f9fc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
    }
    main {
      max-width: 860px;
      margin: 0 auto;
      padding: 48px 20px 72px;
    }
    .page {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 34px;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(30px, 5vw, 46px);
      line-height: 1.1;
    }
    h2 {
      margin-top: 30px;
      font-size: 21px;
    }
    p, li { color: var(--muted); }
    a { color: var(--brand); }
    .updated {
      margin-top: 0;
      color: var(--muted);
    }
    ul { padding-left: 22px; }
  </style>
</head>
<body>
  <main>
    <article class="page">
      <h1>Blink Delivery Account Deletion</h1>
      <p class="updated">Last updated: July 22, 2026</p>

      <p>
        Blink Delivery users and riders can request deletion of their account
        and associated personal data by contacting Blink support.
      </p>

      <h2>How to Request Deletion</h2>
      <ul>
        <li>Send a WhatsApp message to <a href="https://wa.me/959424594930">+95 942 459 4930</a>.</li>
        <li>Include the phone number used for your Blink account.</li>
        <li>Write "Delete my Blink account" in your message.</li>
      </ul>
      <p>
        We may ask you to confirm your phone number or account ownership before
        processing the request. After verification, we aim to complete deletion
        within 30 days.
      </p>

      <h2>Data Deleted</h2>
      <p>When your account deletion request is completed, we delete or anonymize:</p>
      <ul>
        <li>Your profile information, such as nickname, profile photo, phone number, and payment QR image.</li>
        <li>Uploaded profile or settlement images that are no longer required.</li>
        <li>Customer support and account-related data that is no longer needed for legal, safety, or dispute purposes.</li>
      </ul>

      <h2>Data We May Keep</h2>
      <p>
        Some data may be kept where required for fraud prevention, legal
        compliance, accounting, settlement, safety, or dispute resolution. This
        may include order records, payment confirmation records, delivery
        history, settlement records, and chat records related to completed or
        disputed orders.
      </p>
      <p>
        We keep retained records only as long as reasonably necessary for these
        purposes, then delete or anonymize them when they are no longer needed.
      </p>

      <h2>Contact</h2>
      <p>
        For account deletion or privacy questions, contact Blink support on
        WhatsApp at <a href="https://wa.me/959424594930">+95 942 459 4930</a>.
      </p>
    </article>
  </main>
</body>
</html>
"""


TERMS_CONDITIONS_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Blink Delivery Terms and Conditions</title>
  <style>
    :root {
      color-scheme: light;
      --text: #172033;
      --muted: #667085;
      --line: #e5e7eb;
      --brand: #0f6bff;
      --bg: #f7f9fc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
    }
    main {
      max-width: 860px;
      margin: 0 auto;
      padding: 48px 20px 72px;
    }
    .page {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 34px;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(30px, 5vw, 46px);
      line-height: 1.1;
    }
    h2 {
      margin-top: 30px;
      font-size: 21px;
    }
    p, li { color: var(--muted); }
    a { color: var(--brand); }
    .updated {
      margin-top: 0;
      color: var(--muted);
    }
    ul { padding-left: 22px; }
  </style>
</head>
<body>
  <main>
    <article class="page">
      <h1>Blink Delivery Terms and Conditions</h1>
      <p class="updated">Last updated: July 19, 2026</p>

      <p>
        Blink Delivery ("Blink", "we", "our", or "us") provides a logistics
        matching and settlement environment for senders, receivers, and riders
        in Myanmar. By using Blink, you agree to these Terms and Conditions.
      </p>

      <h2>1. Platform Role</h2>
      <p>
        Blink acts as an intermediary platform. We help connect delivery users
        and riders, provide order management, chat, payment confirmation, and
        settlement tools, and may hold delivery fees, item value payments, and
        rider deposits until the delivery process is completed or resolved.
      </p>

      <h2>2. Allowed and Prohibited Items</h2>
      <p>
        Users and riders may use Blink only for lawful deliveries, such as food,
        daily-use goods, documents, parcels, and other products that are not
        prohibited by Myanmar law.
      </p>
      <p>
        Users must not send prohibited or illegal items, including weapons,
        military equipment, drones where restricted, drugs, controlled substances,
        or any goods prohibited by Myanmar government rules. If prohibited items
        are discovered by police or authorities, the sender, receiver, and rider
        involved are responsible. Blink is not a participant in illegal goods,
        and Blink has no responsibility for unlawful items placed into delivery.
      </p>

      <h2>3. Pickup and Drop-off Address Accuracy</h2>
      <p>
        Pickup and drop-off locations should be based mainly on the Google Map
        Location provided in the order. Written address details such as building,
        street, city, and township are supporting information only and may not be
        enough for the rider to find the correct location.
      </p>
      <p>
        Riders will primarily follow the Google Map Location for pickup and
        delivery. The user is responsible for checking that the Google Map
        Location is correct before submitting the order. If the user provides an
        incorrect Google Map Location, wrong address, or unclear delivery
        information, the user is responsible for any delivery fee loss, extra
        trip cost, delay, failed delivery, or related loss caused by that mistake.
      </p>

      <h2>4. Payments and Settlement</h2>
      <p>
        Delivery fees, item value payments, and rider deposits may be held by the
        platform during the order process. Blink releases settlement only after
        the relevant delivery, payment, and confirmation steps are completed.
        This process is designed to reduce fraud risk for users and riders.
      </p>

      <h2>5. User and Rider Responsibility</h2>
      <p>
        Users are responsible for accurate order details, lawful goods, correct
        payment screenshots, and valid settlement QR codes. Riders are responsible
        for handling accepted orders carefully, following lawful delivery
        practices, and providing correct settlement information.
      </p>

      <h2>6. Changes to These Terms</h2>
      <p>
        We may update these Terms from time to time. When we make changes, we
        will update the "Last updated" date above.
      </p>

      <h2>7. Contact Us</h2>
      <p>
        If you have questions about these Terms or need support, send us a
        WhatsApp message at <a href="https://wa.me/959424594930">+95 942 459 4930</a>.
      </p>
    </article>
  </main>
</body>
</html>
"""


@app.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="courier-api",
        timestamp=datetime.now(timezone.utc),
    )


@app.get("/config/payment", response_model=PlatformPaymentConfigResponse)
def get_platform_payment_config() -> PlatformPaymentConfigResponse:
    return PlatformPaymentConfigResponse(
        kpay_qr_image_url=clean_optional_text(signed_gcs_read_url(PLATFORM_KPAY_QR_IMAGE_URL)),
        kpay_account_name=clean_optional_text(PLATFORM_KPAY_ACCOUNT_NAME),
        kpay_account_note=clean_optional_text(PLATFORM_KPAY_ACCOUNT_NOTE),
        max_goods_amount_mmk=MAX_GOODS_AMOUNT_MMK,
    )


@app.get("/config/app-update", response_model=AppUpdateConfigResponse)
def get_app_update_config(
    app_type: Literal["user", "rider", "ios_user", "ios_rider"] = Query(alias="app"),
) -> AppUpdateConfigResponse:
    if app_type == "ios_user":
        version_code = IOS_USER_LATEST_BUILD_NUMBER
        version_name = IOS_USER_LATEST_VERSION_NAME
        download_url = IOS_USER_APP_STORE_URL
        force_update = IOS_USER_FORCE_UPDATE
    elif app_type == "ios_rider":
        version_code = IOS_RIDER_LATEST_BUILD_NUMBER
        version_name = IOS_RIDER_LATEST_VERSION_NAME
        download_url = IOS_RIDER_APP_STORE_URL
        force_update = IOS_RIDER_FORCE_UPDATE
    elif app_type == "rider":
        version_code = ANDROID_RIDER_LATEST_VERSION_CODE
        version_name = ANDROID_RIDER_LATEST_VERSION_NAME
        download_url = ANDROID_RIDER_APK_URL
        force_update = ANDROID_RIDER_FORCE_UPDATE
    else:
        version_code = ANDROID_USER_LATEST_VERSION_CODE
        version_name = ANDROID_USER_LATEST_VERSION_NAME
        download_url = ANDROID_USER_APK_URL
        force_update = ANDROID_USER_FORCE_UPDATE

    return AppUpdateConfigResponse(
        latest_version_code=version_code,
        latest_version_name=version_name,
        download_url=clean_optional_text(download_url),
        force_update=force_update,
        message="发现新版本，请更新后继续使用。" if force_update else "发现新版本，建议现在更新。",
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy_page() -> HTMLResponse:
    return HTMLResponse(
        PRIVACY_POLICY_HTML,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/account-deletion", response_class=HTMLResponse)
def account_deletion_page() -> HTMLResponse:
    return HTMLResponse(
        ACCOUNT_DELETION_HTML,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/terms", response_class=HTMLResponse)
def terms_conditions_page() -> HTMLResponse:
    return HTMLResponse(
        TERMS_CONDITIONS_HTML,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return HTMLResponse(
        ADMIN_HTML,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/admin/data")
def admin_data(key: str = Query(default="")) -> dict:
    require_admin_key(key)
    orders_data = load_admin_orders()
    accounts_data = load_admin_accounts()
    messages_data = load_admin_chat_messages()
    payments_data = load_admin_prepaid_payments()
    return {
        "orders": orders_data,
        "accounts": accounts_data,
        "messages": messages_data,
        "payments": payments_data,
    }


@app.post("/admin/chat/messages", response_model=ChatMessageResponse)
def create_admin_chat_message(
    request: AdminChatReplyRequest,
    key: str = Query(default=""),
) -> ChatMessageResponse:
    require_admin_key(key)
    message_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    conversation_id = request.conversation_id.strip().lower()
    text = request.text.strip()
    image_url = request.image_url.strip() if request.image_url else None
    if request.image_data:
        image_url = upload_base64_image(
            image_data=request.image_data,
            content_type=request.image_content_type,
            file_name=request.image_file_name,
            folder="chat",
        )
    if not text and not image_url:
        raise HTTPException(status_code=400, detail="消息不能为空")

    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO chat_messages (
                id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                text,
                "admin",
                ADMIN_CHAT_SENDER_NAME,
                None,
                image_url,
                created_at.isoformat(),
            ),
        )

    return ChatMessageResponse(
        id=message_id,
        conversation_id=conversation_id,
        text=text,
        sender_type="admin",
        sender_name=ADMIN_CHAT_SENDER_NAME,
        sender_phone=None,
        image_url=signed_gcs_read_url(image_url),
        created_at=created_at,
    )


@app.patch("/admin/payments/{payment_id}", response_model=PrepaidPaymentResponse)
def admin_update_prepaid_payment(
    payment_id: str,
    request: AdminUpdatePrepaidPaymentRequest,
    key: str = Query(default=""),
) -> PrepaidPaymentResponse:
    require_admin_key(key)
    payment = load_prepaid_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="付款记录不存在")

    updates: dict[str, object] = {}
    if request.status is not None:
        if request.status not in ("pending", "confirmed", "rejected"):
            raise HTTPException(status_code=400, detail="付款状态只能是待确认、已确认或已拒绝")
        updates["status"] = request.status
        updates["confirmed_at"] = datetime.now(timezone.utc) if request.status == "confirmed" else None

    updated = payment.model_copy(update=updates)
    save_prepaid_payment(updated)
    sync_orders_for_prepaid_payment(updated)
    return updated


@app.patch("/admin/orders/{order_id}", response_model=OrderResponse)
def admin_update_order(
    order_id: str,
    request: AdminUpdateOrderRequest,
    key: str = Query(default=""),
) -> OrderResponse:
    require_admin_key(key)
    release_expired_rider_deposit_orders()
    record = load_order_record(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="订单不存在")

    order, user_phone, rider_phone = record
    if (
        request.rider_deposit_status == "confirmed"
        and (order.status == "matching" or not rider_phone or order.rider_deposit_status == "unpaid")
    ):
        raise HTTPException(status_code=409, detail="骑手押金确认已超时，订单已重新开放给其他骑手")

    updates = {
        name: value
        for name, value in {
            "status": request.status,
            "user_payment_status": request.user_payment_status,
            "rider_deposit_status": request.rider_deposit_status,
            "settlement_status": request.settlement_status,
        }.items()
        if value is not None
    }
    now = datetime.now(timezone.utc)
    if request.rider_deposit_status == "confirmed":
        updates["rider_deposit_due_at"] = None
    if request.settlement_status in ("paid_to_rider", "completed") and not order.rider_settlement_paid_at:
        updates["rider_settlement_paid_at"] = now
    if request.settlement_status in ("paid_to_rider", "completed") and not order.rider_settlement_bill_created_at:
        delivery_fee = order.delivery_fee or order.price
        original_delivery_fee = order.original_delivery_fee or delivery_fee
        platform_fee = order.platform_delivery_fee if order.promotion_applied else order.platform_delivery_fee or delivery_platform_fee(delivery_fee)
        rider_amount = order.rider_delivery_fee or delivery_payout_fee(original_delivery_fee)
        updates["rider_settlement_bill_title"] = "送货费已结算"
        updates["rider_settlement_bill_message"] = (
            f"订单 #{order.id[:6].upper()} 原送货费 {original_delivery_fee:,.0f} MMK，"
            f"平台扣费 {platform_fee:,.0f} MMK，最终送货费 {rider_amount:,.0f} MMK 已结算给骑手，请查收。"
        )
        updates["rider_settlement_bill_amount"] = rider_amount
        updates["rider_settlement_bill_created_at"] = now
    if (
        request.settlement_status in ("paid_to_user", "completed")
        and order.payment_mode == "cod"
        and not order.user_settlement_paid_at
    ):
        updates["user_settlement_paid_at"] = now
    if (
        request.settlement_status in ("paid_to_user", "completed")
        and order.payment_mode == "cod"
        and not order.user_settlement_bill_created_at
    ):
        updates["user_settlement_bill_title"] = "货费已转请查收"
        updates["user_settlement_bill_message"] = (
            f"货费 {order.goods_amount:,.0f} MMK 已转给用户，请查收。"
        )
        updates["user_settlement_bill_amount"] = order.goods_amount
        updates["user_settlement_bill_created_at"] = now
    updated = order.model_copy(update=updates)
    save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
    return order_for_response(updated)


@app.post("/auth/login", response_model=LoginResponse)
def login(
    request: LoginRequest,
    x_blink_app_role: str | None = Header(default=None, alias="X-Blink-App-Role"),
) -> LoginResponse:
    phone = normalize_myanmar_phone(request.phone)
    if not is_test_login(phone, request.code):
        stored = sms_codes.get(phone)
        now = datetime.now(timezone.utc)

        if not stored or stored[1] < now:
            raise HTTPException(status_code=401, detail="验证码已过期，请重新获取")

        if request.code != stored[0]:
            raise HTTPException(status_code=401, detail="验证码错误")

    sms_codes.pop(phone, None)
    profile = save_account(phone, nickname=None if load_account_profile(phone) else "快送用户")
    mark_account_app_role(phone, x_blink_app_role)

    return LoginResponse(
        token=f"dev-token-{phone}",
        user=profile,
    )


@app.post("/auth/oauth-login", response_model=LoginResponse)
def oauth_login(
    request: OAuthLoginRequest,
    x_blink_app_role: str | None = Header(default=None, alias="X-Blink-App-Role"),
) -> LoginResponse:
    subject, email, name = require_oauth_identity(request)
    account_id = oauth_account_id(request.provider, subject)
    fallback_name = "Apple 用户" if request.provider == "apple" else "Gmail 用户"
    existing = load_account_profile(account_id)
    nickname = existing.nickname if existing else clean_optional_text(name) or clean_optional_text(email) or fallback_name
    profile = save_account(account_id, email=email, nickname=nickname)
    mark_account_app_role(account_id, x_blink_app_role)

    return LoginResponse(
        token=f"dev-token-{account_id}",
        user=profile,
    )


@app.get("/account/profile", response_model=UserProfile)
def get_account_profile(
    authorization: str | None = Header(default=None),
    x_blink_app_role: str | None = Header(default=None, alias="X-Blink-App-Role"),
) -> UserProfile:
    phone = require_account_phone(authorization)
    mark_account_app_role(phone, x_blink_app_role)
    profile = load_account_profile(phone)
    if profile:
        return profile
    return save_account(phone, nickname="快送用户")


@app.post("/account/profile", response_model=UserProfile)
def update_account_profile(
    request: UpdateProfileRequest,
    authorization: str | None = Header(default=None),
    x_blink_app_role: str | None = Header(default=None, alias="X-Blink-App-Role"),
) -> UserProfile:
    phone = require_account_phone(authorization)
    mark_account_app_role(phone, x_blink_app_role)
    nickname = clean_optional_text(request.nickname)
    avatar_url = clean_optional_text(request.avatar_url)
    payment_qr_url = clean_optional_text(request.payment_qr_url)
    if nickname is not None and len(nickname) > 40:
        raise HTTPException(status_code=400, detail="用户名最多 40 个字符")
    return save_account(
        phone,
        nickname=nickname,
        avatar_url=avatar_url,
        payment_qr_url=payment_qr_url,
        clear_app_deleted_at=True,
    )


@app.post("/account/terms", response_model=UserProfile)
def accept_account_terms(
    authorization: str | None = Header(default=None),
    x_blink_app_role: str | None = Header(default=None, alias="X-Blink-App-Role"),
) -> UserProfile:
    phone = require_account_phone(authorization)
    mark_account_app_role(phone, x_blink_app_role)
    return save_account(
        phone,
        terms_accepted_at=datetime.now(timezone.utc).isoformat(),
        terms_version=CURRENT_TERMS_VERSION,
    )


@app.delete("/account", response_model=EmptyResponse)
def delete_current_account(authorization: str | None = Header(default=None)) -> EmptyResponse:
    phone = require_account_phone(authorization)
    mark_account_deleted_for_app(phone)
    return EmptyResponse()


@app.post("/auth/sms-code", response_model=SendSMSCodeResponse)
async def send_login_sms_code(request: SendSMSCodeRequest) -> SendSMSCodeResponse:
    phone = normalize_myanmar_phone(request.phone)
    if is_test_login(phone):
        expires_at = datetime.now(timezone.utc) + timedelta(days=365)
        sms_codes[phone] = (test_login_code(), expires_at)
        return SendSMSCodeResponse(
            phone=phone,
            expires_at=expires_at,
        )

    code = create_sms_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    await send_sms_code(phone, code)
    sms_codes[phone] = (code, expires_at)

    return SendSMSCodeResponse(
        phone=phone,
        expires_at=expires_at,
    )


@app.get("/chat/messages", response_model=list[ChatMessageResponse])
def list_chat_messages(
    conversation_id: str = "main",
    limit: int = Query(default=100, ge=1, le=300),
    authorization: str | None = Header(default=None),
) -> list[ChatMessageResponse]:
    if conversation_id == "all":
        phone = require_account_phone(authorization)
        hidden_before = account_data_hidden_before(phone)
        main_conversation_id = account_conversation_id("main", authorization, phone)
        with connect_db() as connection:
            if hidden_before:
                order_rows = connection.execute(
                    """
                    SELECT id
                    FROM orders
                    WHERE (user_phone = ? OR rider_phone = ?)
                      AND created_at > ?
                    """,
                    (phone, phone, hidden_before),
                ).fetchall()
            else:
                order_rows = connection.execute(
                    """
                    SELECT id
                    FROM orders
                    WHERE user_phone = ? OR rider_phone = ?
                    """,
                    (phone, phone),
                ).fetchall()
            conversation_ids = [main_conversation_id]
            if main_conversation_id != "main":
                conversation_ids.append("main")
            for row in order_rows:
                order_conversation_id = f"order:{row['id']}"
                conversation_ids.append(order_conversation_id.lower())
                conversation_ids.append(order_conversation_id.upper())

            if hidden_before:
                message_rows = connection.execute(
                    """
                    SELECT DISTINCT conversation_id
                    FROM chat_messages
                    WHERE sender_phone = ?
                      AND created_at > ?
                    """,
                    (phone, hidden_before),
                ).fetchall()
            else:
                message_rows = connection.execute(
                    """
                    SELECT DISTINCT conversation_id
                    FROM chat_messages
                    WHERE sender_phone = ?
                    """,
                    (phone,),
                ).fetchall()
            for row in message_rows:
                conversation_ids.append(row["conversation_id"])

            conversation_ids = list(dict.fromkeys(conversation_ids))
            placeholders = ",".join("?" for _ in conversation_ids)
            hidden_filter = "AND created_at > ?" if hidden_before else ""
            hidden_params = (hidden_before,) if hidden_before else ()
            rows = connection.execute(
                f"""
                SELECT id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
                FROM chat_messages
                WHERE conversation_id IN ({placeholders})
                {hidden_filter}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*conversation_ids, *hidden_params, limit),
            ).fetchall()

        return [chat_message_from_row(row) for row in reversed(rows)]

    conversation_id = account_conversation_id(conversation_id, authorization)
    phone = require_account_phone(authorization)
    hidden_before = account_data_hidden_before(phone)
    with connect_db() as connection:
        if hidden_before:
            rows = connection.execute(
                """
                SELECT id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
                FROM chat_messages
                WHERE conversation_id = ?
                  AND created_at > ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, hidden_before, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
                FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()

    return [chat_message_from_row(row) for row in reversed(rows)]


@app.post("/chat/messages", response_model=ChatMessageResponse)
def create_chat_message(
    request: CreateChatMessageRequest,
    authorization: str | None = Header(default=None),
) -> ChatMessageResponse:
    message_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    sender_phone = phone_from_authorization(authorization)
    if not sender_phone and request.sender_phone:
        sender_phone = normalize_chat_sender_account(request.sender_phone)
    mark_account_app_role(sender_phone, request.sender_type)
    text = request.text.strip()
    image_url = request.image_url.strip() if request.image_url else None
    sender_name = account_nickname(sender_phone) or request.sender_name.strip() or ("骑手" if request.sender_type == "rider" else "用户")
    conversation_id = account_conversation_id(request.conversation_id, authorization, sender_phone)

    if not text and not image_url:
        raise HTTPException(status_code=400, detail="消息不能为空")

    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO chat_messages (
                id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                text,
                request.sender_type,
                sender_name,
                sender_phone,
                image_url,
                created_at.isoformat(),
            ),
        )

    return ChatMessageResponse(
        id=message_id,
        conversation_id=conversation_id,
        text=text,
        sender_type=request.sender_type,
        sender_name=sender_name,
        sender_phone=sender_phone,
        image_url=signed_gcs_read_url(image_url),
        created_at=created_at,
    )


@app.get("/orders", response_model=list[OrderResponse])
def list_orders(authorization: str | None = Header(default=None)) -> list[OrderResponse]:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    return [order_for_response(order) for order in load_user_orders(user_phone)]


@app.get("/promotion/delivery", response_model=DeliveryPromotionResponse)
def get_delivery_promotion(
    distance_km: float = Query(gt=0),
    invite_email: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> DeliveryPromotionResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    return delivery_promotion_quote(user_phone, distance_km, invite_email)


@app.post("/payments/prepaid", response_model=PrepaidPaymentResponse)
def create_prepaid_payment(
    request: CreatePrepaidPaymentRequest,
    authorization: str | None = Header(default=None),
) -> PrepaidPaymentResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    payment_proof_url = clean_optional_text(request.payment_proof_url)
    if not payment_proof_url:
        raise HTTPException(status_code=400, detail="请上传 KPay 转账截图")
    if request.goods_amount > MAX_GOODS_AMOUNT_MMK:
        raise HTTPException(status_code=400, detail=f"货物价格不能超过 {MAX_GOODS_AMOUNT_MMK:,.0f} MMK")
    original_delivery_fee = estimate_price(request.distance_km, 1)
    promotion = delivery_promotion_quote(user_phone, request.distance_km, request.promo_invite_email)
    promotion_applied = promotion.active and promotion.eligible
    if promotion.active and promotion.requires_invite_email and not promotion.eligible:
        discount_fee = promotion.discount_fee or DELIVERY_PROMOTION_FEE_MMK
        if abs(request.amount - discount_fee) <= 1:
            raise HTTPException(status_code=400, detail=promotion.message or "请填写有效好友邮箱")
    payment_amount = promotion.payable_fee if promotion_applied and promotion.payable_fee is not None else round(request.amount, 2)

    payment = PrepaidPaymentResponse(
        id=str(uuid4()),
        user_phone=user_phone,
        amount=round(payment_amount, 2),
        distance_km=request.distance_km,
        goods_amount=round(request.goods_amount, 2),
        payment_mode=request.payment_mode,
        status="pending",
        created_at=datetime.now(timezone.utc),
        payment_proof_url=payment_proof_url,
        original_delivery_fee=original_delivery_fee,
        promotion_applied=promotion_applied,
        promo_invite_email=promotion.invite_email,
    )
    save_prepaid_payment(payment)
    return payment


@app.post("/payments/dinger", response_model=PrepaidPaymentResponse)
async def create_dinger_payment(
    request: CreateDingerPaymentRequest,
    authorization: str | None = Header(default=None),
) -> PrepaidPaymentResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    payment_id = str(uuid4())
    try:
        dinger_response = await create_dinger_charge(payment_id, request, user_phone)
    except httpx.HTTPError as exc:
        logger.exception("Dinger payment request failed")
        raise HTTPException(status_code=502, detail="Dinger 付款请求失败") from exc

    payment = PrepaidPaymentResponse(
        id=payment_id,
        user_phone=user_phone,
        amount=round(request.amount, 2),
        distance_km=request.distance_km,
        payment_mode=request.payment_mode,
        status="pending",
        created_at=datetime.now(timezone.utc),
        dinger_transaction_num=dinger_response.get("transactionNum"),
        dinger_form_token=dinger_response.get("formToken"),
        dinger_qr_code=dinger_response.get("qrCode"),
        dinger_provider_name=request.provider_name,
        dinger_method_name=request.method_name,
    )
    save_prepaid_payment(payment)
    return payment


@app.post("/payments/dinger/callback")
def handle_dinger_callback(request: DingerCallbackRequest) -> dict[str, str]:
    try:
        result, result_text = decrypt_dinger_payment_result(request.payment_result)
        verify_dinger_checksum(result_text, request.checksum)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Dinger callback decrypt failed")
        raise HTTPException(status_code=400, detail="Dinger callback 解密失败") from exc

    payment_id = None
    for key in ("merchantOrderId", "merchant_order_id", "orderId"):
        value = result.get(key)
        if value is not None:
            payment_id = clean_optional_text(str(value))
            if payment_id:
                break
    if not payment_id:
        raise HTTPException(status_code=400, detail="Dinger callback 缺少订单号")

    payment = load_prepaid_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="付款记录不存在")

    transaction_status = str(result.get("transactionStatus") or "").upper()
    next_status: PaymentStatus = "confirmed" if transaction_status == "SUCCESS" else "rejected"
    updated = payment.model_copy(
        update={
            "status": next_status,
            "confirmed_at": datetime.now(timezone.utc) if next_status == "confirmed" else None,
            "dinger_transaction_num": result.get("transactionId") or payment.dinger_transaction_num,
            "dinger_provider_name": result.get("providerName") or payment.dinger_provider_name,
            "dinger_method_name": result.get("methodName") or payment.dinger_method_name,
        }
    )
    save_prepaid_payment(updated)
    sync_orders_for_prepaid_payment(updated)
    return {"status": "ok"}


@app.get("/payments/prepaid/{payment_id}", response_model=PrepaidPaymentResponse)
def get_prepaid_payment(
    payment_id: str,
    authorization: str | None = Header(default=None),
) -> PrepaidPaymentResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    payment = load_prepaid_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="付款记录不存在")
    if payment.user_phone != user_phone:
        raise HTTPException(status_code=403, detail="不能查看其他账号的付款记录")
    return payment


@app.post("/orders", response_model=OrderResponse)
def create_order(
    request: CreateOrderRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    original_delivery_fee = estimate_price(request.distance_km, request.weight_kg)
    kpay_transaction_id = clean_optional_text(request.kpay_transaction_id)
    goods_image_url = clean_optional_text(request.goods_image_url)
    payment_proof_url = clean_optional_text(request.payment_proof_url)

    if request.goods_amount <= 0:
        raise HTTPException(status_code=400, detail="请填写货物价格")
    if request.goods_amount > MAX_GOODS_AMOUNT_MMK:
        raise HTTPException(status_code=400, detail=f"货物价格不能超过 {MAX_GOODS_AMOUNT_MMK:,.0f} MMK")

    if not goods_image_url:
        raise HTTPException(status_code=400, detail="请上传商品图片")

    if not kpay_transaction_id:
        raise HTTPException(status_code=400, detail="请先支付送货费，并等待后台确认收到付款")
    prepaid_payment = load_prepaid_payment(kpay_transaction_id)
    if not prepaid_payment:
        raise HTTPException(status_code=400, detail="付款记录不存在，请重新付款")
    if prepaid_payment.user_phone != user_phone:
        raise HTTPException(status_code=403, detail="不能使用其他账号的付款记录")
    if prepaid_payment.status == "rejected":
        raise HTTPException(status_code=400, detail="送货费付款未通过，请重新付款")
    if prepaid_payment.status != "confirmed":
        raise HTTPException(status_code=400, detail="请等待后台确认收到送货费后再下单")
    promotion_applied = prepaid_payment.promotion_applied
    promo_invite_email = normalize_email(prepaid_payment.promo_invite_email)
    delivery_fee = round(prepaid_payment.amount, 2) if promotion_applied else original_delivery_fee
    platform_delivery_fee = 0 if promotion_applied else delivery_platform_fee(delivery_fee)
    rider_delivery_fee = delivery_payout_fee(original_delivery_fee)
    accepted_payment_amounts = (delivery_fee, rider_delivery_fee)
    if all(abs(prepaid_payment.amount - amount) > 1 for amount in accepted_payment_amounts):
        raise HTTPException(status_code=400, detail="付款金额和当前订单金额不一致，请重新付款")
    if prepaid_payment.payment_mode != request.payment_mode:
        raise HTTPException(status_code=400, detail="付款方式和当前订单不一致，请重新付款")
    if load_order_record(kpay_transaction_id):
        raise HTTPException(status_code=400, detail="这个付款订单已经创建过，请刷新订单列表")
    if promotion_applied:
        save_delivery_promotion_redemption(
            user_phone=user_phone,
            payment_id=kpay_transaction_id,
            order_id=kpay_transaction_id,
            original_delivery_fee=original_delivery_fee,
            discounted_delivery_fee=delivery_fee,
            invite_email=promo_invite_email,
        )
    payment_proof_url = payment_proof_url or prepaid_payment.payment_proof_url
    user_payment_status: PaymentStatus = prepaid_payment.status
    rider_deposit_status: PaymentStatus = "unpaid" if request.goods_amount > 0 else "not_required"

    order = OrderResponse(
        id=kpay_transaction_id,
        pickup_address=request.pickup_address,
        dropoff_address=request.dropoff_address,
        parcel_type=request.parcel_type,
        weight_kg=request.weight_kg,
        note=request.note,
        distance_km=request.distance_km,
        price=delivery_fee,
        delivery_fee=delivery_fee,
        platform_delivery_fee=platform_delivery_fee,
        rider_delivery_fee=rider_delivery_fee,
        payment_mode=request.payment_mode,
        goods_amount=request.goods_amount,
        goods_image_url=goods_image_url,
        user_payment_status=user_payment_status,
        rider_deposit_status=rider_deposit_status,
        settlement_status="pending",
        kpay_transaction_id=kpay_transaction_id,
        payment_proof_url=payment_proof_url,
        status="matching",
        created_at=datetime.now(timezone.utc),
        pickup_lat=request.pickup_lat,
        pickup_lng=request.pickup_lng,
        dropoff_lat=request.dropoff_lat,
        dropoff_lng=request.dropoff_lng,
        original_delivery_fee=original_delivery_fee if promotion_applied else None,
        promotion_applied=promotion_applied,
        promo_invite_email=promo_invite_email,
    )
    save_order(order, user_phone=user_phone)
    return order_for_response(order)


@app.get("/orders/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    release_expired_rider_deposit_orders()
    record = load_order_record(order_id)
    if record:
        order, stored_user_phone, _ = record
        if stored_user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能查看其他账号的订单")
        if not app_data_visible_to_account(user_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        return order_for_response(order)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/orders/{order_id}/settlement", response_model=OrderResponse)
def request_user_settlement(
    order_id: str,
    request: UserSettlementRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    record = load_order_record(order_id)
    if record:
        order, stored_user_phone, rider_phone = record
        if stored_user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能提交其他账号的收款信息")
        if not app_data_visible_to_account(user_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.payment_mode != "cod":
            raise HTTPException(status_code=400, detail="只有货到付款订单需要提醒平台转货费")
        if order.status != "completed":
            raise HTTPException(status_code=400, detail="骑手完成送货后才能提醒平台转货费")
        if order.settlement_status in ("paid_to_user", "completed"):
            return order_for_response(order)

        name = request.name.strip()
        qr_url = clean_optional_text(request.qr_url)
        if not name:
            raise HTTPException(status_code=400, detail="请填写收款人名字")
        if not qr_url:
            raise HTTPException(status_code=400, detail="请上传收款二维码")

        updated = order.model_copy(
            update={
                "user_settlement_name": name,
                "user_settlement_qr_url": qr_url,
                "user_settlement_requested_at": datetime.now(timezone.utc),
                "settlement_status": "paid_to_rider" if order.settlement_status == "paid_to_rider" else "pending",
            }
        )
        save_order(updated, user_phone=stored_user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.get("/rider/orders", response_model=list[OrderResponse])
def list_rider_orders(authorization: str | None = Header(default=None)) -> list[OrderResponse]:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    return [order_for_response(order) for order in load_rider_orders(rider_phone)]


@app.post("/rider/orders/{order_id}/accept", response_model=OrderResponse)
def accept_order(
    order_id: str,
    request: AcceptOrderRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    release_expired_rider_deposit_orders()
    record = load_order_record(order_id)
    if record:
        order, user_phone, _ = record
        if not app_data_visible_to_account(user_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.status != "matching":
            raise HTTPException(status_code=409, detail="订单已被接单或不可接单")
        if order.user_payment_status != "confirmed":
            raise HTTPException(status_code=403, detail="平台确认用户送货费付款后骑手才能接单")
        updates: dict[str, object] = {
            "status": "accepted",
            "rider_name": request.rider_name,
            "accepted_at": datetime.now(timezone.utc),
        }
        if order.rider_deposit_status != "not_required":
            updates["rider_deposit_due_at"] = rider_deposit_due_at()
            updates["rider_deposit_submitted_at"] = None
        updated = order.model_copy(update=updates)
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/deposit", response_model=OrderResponse)
def mark_rider_deposit_transferred(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    release_expired_rider_deposit_orders()
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            if order.status == "matching":
                raise HTTPException(status_code=409, detail="押金确认超时，订单已重新开放给其他骑手")
            raise HTTPException(status_code=403, detail="不能更新其他骑手的押金状态")
        if not app_data_visible_to_account(rider_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.rider_deposit_status == "not_required":
            raise HTTPException(status_code=400, detail="这个订单不需要骑手押金")
        if order.rider_deposit_status == "confirmed":
            return order_for_response(order)
        updated = order.model_copy(
            update={
                "rider_deposit_status": "pending",
                "rider_deposit_submitted_at": datetime.now(timezone.utc),
            }
        )
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/status", response_model=OrderResponse)
def update_rider_order_status(
    order_id: str,
    request: UpdateOrderStatusRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    release_expired_rider_deposit_orders()
    allowed = ["picking_up", "delivering", "completed"]
    if request.status not in allowed:
        raise HTTPException(status_code=400, detail="骑手不能设置这个订单状态")

    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能更新其他骑手的订单")
        if not app_data_visible_to_account(rider_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if (
            request.status in ["picking_up", "delivering"]
            and order.rider_deposit_status != "not_required"
            and order.rider_deposit_status != "confirmed"
        ):
            raise HTTPException(status_code=403, detail="平台确认骑手押金后才能开始取件配送")
        updated = order.model_copy(update={"status": request.status})
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/cancel", response_model=OrderResponse)
def cancel_rider_order(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能取消其他骑手的订单")
        if not app_data_visible_to_account(rider_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.status in ("accepted", "picking_up"):
            released = order.model_copy(
                update={
                    "status": "matching",
                    "rider_name": None,
                    "accepted_at": None,
                    "rider_deposit_status": "unpaid" if order.rider_deposit_status != "not_required" else "not_required",
                    "rider_deposit_due_at": None,
                    "rider_deposit_submitted_at": None,
                    "rider_lat": None,
                    "rider_lng": None,
                    "rider_location_updated_at": None,
                    "cancellation_actor": None,
                    "cancellation_reason": None,
                    "cancellation_compensation_amount": None,
                    "cancelled_at": None,
                }
            )
            save_order(released, user_phone=user_phone, rider_phone=None)
            return order_for_response(released)
        if order.status == "delivering":
            cancelled = order.model_copy(
                update={
                    "status": "cancelled",
                    "cancellation_actor": "rider",
                    "cancellation_reason": "骑手取消送货，需把货还给用户",
                    "cancellation_compensation_amount": 1000,
                    "cancelled_at": datetime.now(timezone.utc),
                }
            )
            save_order(cancelled, user_phone=user_phone, rider_phone=rider_phone)
            return order_for_response(cancelled)
        raise HTTPException(status_code=400, detail="这个订单当前不能取消送货")
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/settlement", response_model=OrderResponse)
def request_rider_settlement(
    order_id: str,
    request: RiderSettlementRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能提交其他骑手的收款信息")
        if not app_data_visible_to_account(rider_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.status != "completed":
            raise HTTPException(status_code=400, detail="完成送货后才能提醒平台结算")
        if order.settlement_status in ("paid_to_rider", "completed"):
            return order_for_response(order)

        name = request.name.strip()
        qr_url = clean_optional_text(request.qr_url)
        if not name:
            raise HTTPException(status_code=400, detail="请填写收款人名字")
        if not qr_url:
            raise HTTPException(status_code=400, detail="请上传收款二维码")

        updated = order.model_copy(
            update={
                "rider_settlement_name": name,
                "rider_settlement_qr_url": qr_url,
                "rider_settlement_requested_at": datetime.now(timezone.utc),
                "settlement_status": "paid_to_user" if order.settlement_status == "paid_to_user" else "pending",
            }
        )
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/location", response_model=OrderResponse)
def update_rider_location(
    order_id: str,
    request: UpdateRiderLocationRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    mark_account_app_role(rider_phone, "rider")
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能更新其他骑手的订单位置")
        if not app_data_visible_to_account(rider_phone, order.created_at):
            raise HTTPException(status_code=404, detail="订单不存在")
        if order.status not in ["accepted", "picking_up", "delivering"]:
            raise HTTPException(status_code=400, detail="订单不在配送中，不能更新骑手位置")
        updated = order.model_copy(
            update={
                "rider_lat": request.lat,
                "rider_lng": request.lng,
                "rider_location_updated_at": datetime.now(timezone.utc),
            }
        )
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/orders/{order_id}/cancel", response_model=OrderResponse)
def cancel_order(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    user_phone = require_account_phone(authorization)
    mark_account_app_role(user_phone, "user")
    record = load_order_record(order_id)
    if record:
        order, stored_user_phone, rider_phone = record
        if stored_user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能取消其他账号的订单")
        if order.status in ("delivering", "completed"):
            raise HTTPException(status_code=400, detail="订单已经开始配送，不能取消")
        now = datetime.now(timezone.utc)
        accepted_at = order.accepted_at or order.created_at
        if accepted_at.tzinfo is None:
            accepted_at = accepted_at.replace(tzinfo=timezone.utc)
        is_late_confirmed_rider_deposit = (
            order.rider_deposit_status == "confirmed"
            and order.status in ("accepted", "picking_up")
            and (now - accepted_at).total_seconds() >= 5 * 60
        )
        updated = order.model_copy(
            update={
                "status": "cancelled",
                "cancellation_actor": "user",
                "cancellation_reason": "用户取消订单",
                "cancellation_compensation_amount": 1000 if is_late_confirmed_rider_deposit else 0,
                "cancelled_at": now,
            }
        )
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/distance/estimate", response_model=DistanceEstimateResponse)
async def estimate_distance(request: DistanceEstimateRequest) -> DistanceEstimateResponse:
    pickup = await geocode_location(request.pickup_location)
    dropoff = await geocode_location(request.dropoff_location)
    route_km, route_polyline = await route_distance_estimate(pickup, dropoff)
    distance_km = round(route_km, 1)
    return DistanceEstimateResponse(
        distance_km=distance_km,
        price=estimate_price(distance_km, 1),
        pickup_lat=pickup[0],
        pickup_lng=pickup[1],
        dropoff_lat=dropoff[0],
        dropoff_lng=dropoff[1],
        route_polyline=route_polyline,
    )


@app.post("/storage/signed-upload-url", response_model=SignedUploadResponse)
def create_signed_upload_url(request: SignedUploadRequest) -> SignedUploadResponse:
    if storage is None:
        raise HTTPException(status_code=500, detail="服务器未安装 Google Cloud Storage 依赖")

    bucket_name = os.getenv("GCS_BUCKET") or os.getenv("GCS_BUCKET_NAME", "courierblink")
    folder = upload_folder(request.folder)
    safe_name = safe_upload_name(request.file_name)
    object_name = f"{folder}/{uuid4()}-{safe_name}"

    try:
        client = gcs_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        upload_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=15),
            method="PUT",
            content_type=request.content_type,
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("GCS signed upload URL creation failed")
        raise HTTPException(status_code=500, detail=f"GCS 上传链接创建失败：{error}") from error

    return SignedUploadResponse(
        upload_url=upload_url,
        public_url=f"https://storage.googleapis.com/{bucket_name}/{quote(object_name)}",
    )
