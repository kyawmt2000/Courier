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
    from google.oauth2 import service_account
except ImportError:
    storage = None
    service_account = None


app = FastAPI(title="Courier API", version="1.0.0")
logger = logging.getLogger("courier-api")

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


class UserProfile(BaseModel):
    id: str
    phone: str
    nickname: str | None = None
    avatar_url: str | None = None
    payment_qr_url: str | None = None


class LoginRequest(BaseModel):
    phone: str = Field(min_length=6)
    code: str = Field(min_length=4)


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


class CreatePrepaidPaymentRequest(BaseModel):
    amount: float = Field(gt=0)
    distance_km: float = Field(gt=0)
    goods_amount: float = Field(default=0, ge=0)
    payment_proof_url: str
    payment_mode: PaymentMode = "cod"


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
    settlement_status: SettlementStatus = "pending"
    kpay_transaction_id: str | None = None
    payment_proof_url: str | None = None
    status: OrderStatus
    rider_name: str | None = None
    created_at: datetime
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
                nickname TEXT,
                avatar_url TEXT,
                payment_qr_url TEXT,
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
        add_column_if_missing(connection, "chat_messages", "conversation_id", "TEXT NOT NULL DEFAULT 'main'")
        add_column_if_missing(connection, "chat_messages", "sender_phone", "TEXT")
        add_column_if_missing(connection, "chat_messages", "image_url", "TEXT")
        add_column_if_missing(connection, "orders", "user_phone", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "orders", "rider_phone", "TEXT")
        add_column_if_missing(connection, "orders", "status", "TEXT NOT NULL DEFAULT 'matching'")
        add_column_if_missing(connection, "orders", "created_at", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "orders", "payload", "TEXT NOT NULL DEFAULT '{}'")
        add_column_if_missing(connection, "accounts", "nickname", "TEXT")
        add_column_if_missing(connection, "accounts", "avatar_url", "TEXT")
        add_column_if_missing(connection, "accounts", "payment_qr_url", "TEXT")
        add_column_if_missing(connection, "accounts", "last_login_at", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "prepaid_payments", "user_phone", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "prepaid_payments", "status", "TEXT NOT NULL DEFAULT 'pending'")
        add_column_if_missing(connection, "prepaid_payments", "created_at", "TEXT NOT NULL DEFAULT ''")
        add_column_if_missing(connection, "prepaid_payments", "payload", "TEXT NOT NULL DEFAULT '{}'")


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

    try:
        return normalize_myanmar_phone(token.removeprefix("dev-token-"))
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
    platform_fee = order.platform_delivery_fee or delivery_platform_fee(delivery_fee)
    rider_fee = order.rider_delivery_fee or delivery_payout_fee(delivery_fee)
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
    with connect_db() as connection:
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
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT payload FROM orders
            WHERE status = 'matching'
               OR rider_phone = ?
            ORDER BY created_at DESC
            """,
            (rider_phone,),
        ).fetchall()
    orders = [order_from_row(row) for row in rows]
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
    nickname: str | None,
    avatar_url: str | None,
    payment_qr_url: str | None,
) -> UserProfile:
    user_id_digits = re.sub(r"\D", "", phone)
    return UserProfile(
        id=f"user_{user_id_digits}",
        phone=phone,
        nickname=nickname,
        avatar_url=signed_gcs_read_url(avatar_url),
        payment_qr_url=signed_gcs_read_url(payment_qr_url),
    )


def load_account_profile(phone: str) -> UserProfile | None:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT phone, nickname, avatar_url, payment_qr_url
            FROM accounts
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()

    if not row:
        return None
    return user_profile_from_account(
        row["phone"],
        row["nickname"],
        row["avatar_url"],
        row["payment_qr_url"],
    )


def save_account(
    phone: str,
    nickname: str | None = None,
    avatar_url: str | None = None,
    payment_qr_url: str | None = None,
) -> UserProfile:
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO accounts (phone, nickname, avatar_url, payment_qr_url, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                nickname = COALESCE(excluded.nickname, accounts.nickname),
                avatar_url = COALESCE(excluded.avatar_url, accounts.avatar_url),
                payment_qr_url = COALESCE(excluded.payment_qr_url, accounts.payment_qr_url),
                last_login_at = excluded.last_login_at
            """,
            (phone, nickname, avatar_url, payment_qr_url, datetime.now(timezone.utc).isoformat()),
        )
    return load_account_profile(phone) or user_profile_from_account(phone, nickname, avatar_url, payment_qr_url)


def require_admin_key(key: str | None) -> None:
    expected = clean_optional_text(os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_KEY"))
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD 未配置")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="后台密码不正确")


def load_admin_orders() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT user_phone, rider_phone, payload
            FROM orders
            ORDER BY created_at DESC
            """
        ).fetchall()

    result: list[dict] = []
    for row in rows:
        order = order_for_response(order_from_row(row)).model_dump(mode="json")
        order["user_phone"] = row["user_phone"]
        order["rider_phone"] = row["rider_phone"]
        result.append(order)
    return result


def load_admin_accounts() -> list[dict]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT phone, nickname, payment_qr_url, avatar_url, last_login_at
            FROM accounts
            ORDER BY last_login_at DESC
            """
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        account = dict(row)
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
    header { position: sticky; top: 0; z-index: 2; display: flex; gap: 12px; align-items: center; padding: 14px 18px; background: #fff; border-bottom: 1px solid #e5e7eb; }
    h1 { margin: 0; font-size: 20px; }
    .version { color: #6b7280; font-size: 12px; white-space: nowrap; }
    main { padding: 18px; display: grid; gap: 16px; }
    .toolbar { margin-left: auto; display: flex; gap: 8px; align-items: center; }
    input, select, button { font: inherit; border: 1px solid #d1d5db; border-radius: 8px; padding: 9px 10px; background: #fff; }
    button { cursor: pointer; background: #16a34a; color: white; border-color: #16a34a; font-weight: 700; }
    button.secondary { background: #fff; color: #111827; border-color: #d1d5db; }
    button.tab { background: #fff; color: #374151; border-color: #d1d5db; }
    button.tab.active { background: #111827; color: #fff; border-color: #111827; }
    section { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px; }
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
    tr:hover { background: #f9fafb; }
    .address-cell { width: 100%; max-width: 100%; overflow-wrap: anywhere; word-break: break-word; line-height: 1.35; }
    .address-cell .muted { display: block; margin-top: 4px; }
    .actions-cell { width: 150px; }
    .actions-cell button { width: 100%; margin-bottom: 6px; padding: 8px 6px; white-space: normal; }
    .grid { display: grid; grid-template-columns: 1.4fr .9fr; gap: 16px; align-items: start; }
    .pill { display: inline-flex; border-radius: 999px; padding: 3px 8px; background: #eef2ff; color: #3730a3; font-size: 12px; font-weight: 700; }
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
    .accounts-layout { display: grid; grid-template-columns: minmax(460px, .9fr) minmax(560px, 1.1fr); gap: 16px; align-items: start; }
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
    @media (max-width: 1100px) { .accounts-layout { grid-template-columns: 1fr; } .account-detail { max-height: none; } }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } header { flex-wrap: wrap; } .toolbar { margin-left: 0; width: 100%; flex-wrap: wrap; } }
  </style>
</head>
<body>
  <header>
    <h1>快送后台</h1>
    <span class="version">orders-ui-v15</span>
    <div class="toolbar">
      <input id="key" type="password" placeholder="后台密码" />
      <input id="q" placeholder="搜索订单/手机号/地址" />
      <button onclick="loadData()">刷新</button>
      <button id="tab-payments" class="tab active" onclick="showPage('payments')">货到付款订单</button>
      <button id="tab-orders" class="tab" onclick="showPage('orders')">货费已付款订单</button>
      <button id="tab-accounts" class="tab" onclick="showPage('accounts')">账号资料</button>
      <button id="tab-settlements" class="tab" onclick="showPage('settlements')">结算</button>
      <button id="tab-service" class="tab" onclick="showPage('service')">客服</button>
    </div>
  </header>
  <main>
    <section id="page-payments" class="page active">
      <h2>货到付款订单</h2>
      <table class="orders-table">
        <colgroup>
          <col class="col-order"><col class="col-party"><col class="col-status"><col class="col-amount">
          <col class="col-proof"><col class="col-deposit"><col><col class="col-actions">
        </colgroup>
        <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>送货费付款</th><th>骑手押金</th><th>地址</th><th>操作</th></tr></thead>
        <tbody id="codOrders"></tbody>
      </table>
    </section>
    <section id="page-orders" class="page">
      <h2>货费已付款订单</h2>
      <table class="orders-table">
        <colgroup>
          <col class="col-order"><col class="col-party"><col class="col-status"><col class="col-amount">
          <col class="col-proof"><col class="col-deposit"><col><col class="col-actions">
        </colgroup>
        <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>用户付款</th><th>骑手押金</th><th>地址</th><th>操作</th></tr></thead>
        <tbody id="orders"></tbody>
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
          <table><thead><tr><th>头像</th><th>昵称</th><th>收款码</th><th>手机号</th><th>最近登录</th></tr></thead><tbody id="accounts"></tbody></table>
        </div>
        <div id="accountDetail" class="account-detail"></div>
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
      <h2>客服</h2>
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
            <button onclick="sendServiceReply()">发送</button>
          </div>
        </section>
      </div>
    </section>
  </main>
  <script>
    let state = { orders: [], accounts: [], messages: [], payments: [] };
    let currentPage = "payments";
    let selectedServiceConversationId = null;
    let selectedAccountPhone = null;
    let selectedAccountPanel = "placed";
    const pages = ["payments","orders","accounts","settlements","service"];
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
    function newestDateMs(item, fields) {
      return Math.max(...fields.map(field => dateMs(item[field])));
    }
    function sortByDateDesc(items, fields = ["created_at"]) {
      return [...items].sort((a, b) => newestDateMs(b, fields) - newestDateMs(a, fields));
    }
    function sortByDateAsc(items, fields = ["created_at"]) {
      return [...items].sort((a, b) => newestDateMs(a, fields) - newestDateMs(b, fields));
    }
    function showPage(page) {
      currentPage = page;
      hideDetail();
      pages.forEach(name => {
        document.getElementById(`page-${name}`)?.classList.toggle("active", name === page);
        document.getElementById(`tab-${name}`)?.classList.toggle("active", name === page);
      });
    }
    function hideDetail() {
      document.getElementById("detailSection")?.classList.add("hidden");
      const detail = document.getElementById("detail");
      if (detail) detail.innerHTML = "";
    }

    function ensureOrderTables() {
      const codSection = document.getElementById("page-payments");
      let prepaidSection = document.getElementById("page-orders");
      if (!prepaidSection && codSection) {
        prepaidSection = document.createElement("section");
        prepaidSection.id = "page-orders";
        prepaidSection.className = "page";
        prepaidSection.innerHTML = `
          <h2>货费已付款订单</h2>
          <table class="orders-table">
            <colgroup>
              <col class="col-order"><col class="col-party"><col class="col-status"><col class="col-amount">
              <col class="col-proof"><col class="col-deposit"><col><col class="col-actions">
            </colgroup>
            <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>用户付款</th><th>骑手押金</th><th>地址</th><th>操作</th></tr></thead>
            <tbody id="orders"></tbody>
          </table>
        `;
        codSection.insertAdjacentElement("afterend", prepaidSection);
      }
      return {
        cod: document.getElementById("codOrders"),
        prepaid: document.getElementById("orders")
      };
    }

    async function loadData() {
      const response = await fetch(`/admin/data?key=${keyParam()}`);
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      state = await response.json();
      render();
      showPage(currentPage);
    }

    function pendingPaymentRow(payment, prepaid) {
      return `
        <tr>
          <td><strong>订单 #${escapeHtml(payment.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(payment.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(payment.user_phone)}<br><span class="muted">用户已上传付款截图，等待后台确认后才能下单</span></td>
          <td><span class="pill">${label(payment.status)}</span><br><span class="muted">${label(payment.payment_mode)}</span></td>
          <td>${prepaid ? "送货费" : "配送费"} ${money(payment.amount)}<br><span class="muted">${Number(payment.distance_km || 0).toFixed(1)} km</span></td>
          <td>${payment.payment_proof_url ? `<img src="${escapeHtml(payment.payment_proof_url)}" alt="KPay 转账截图" style="width:84px;height:84px;object-fit:cover;border-radius:8px;background:#f3f4f6;">` : `<span class="muted">无截图</span>`}</td>
          <td>${prepaid && payment.goods_amount ? `骑手押金 ${money(payment.goods_amount)}` : `<span class="muted">订单创建后显示</span>`}</td>
          <td class="address-cell"><span class="muted">后台确认后，用户端才可以点立即下单</span></td>
          <td class="actions-cell">${payment.status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmPrepaidPayment('${payment.id}')">确认用户付款</button>` : `<span class="pill">已确认</span>`}</td>
        </tr>`;
    }

    function orderTableRow(order, prepaid) {
      return `
        <tr onclick="showDetail('${order.id}')">
          <td><strong>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(order.user_phone)}<br><span class="muted">${escapeHtml(order.rider_phone || "未接单")} ${escapeHtml(order.rider_name || "")}</span></td>
          <td><span class="pill">${label(order.status)}</span><br><span class="muted">${label(order.payment_mode)} / 用户付款：${label(order.user_payment_status)}</span></td>
          <td>配送费 ${money(order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td>${paymentProofCell(order)}</td>
          <td>${riderDepositLabel(order.rider_deposit_status)}</td>
          <td class="address-cell">${escapeHtml(order.pickup_address)}<br><span class="muted">${escapeHtml(order.dropoff_address)}</span></td>
          <td class="actions-cell">
            ${order.user_payment_status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmUserPayment('${order.id}')">${prepaid ? "确认用户付款" : "确认送货费"}</button>` : ""}
            ${order.rider_deposit_status === "pending" ? `<button onclick="event.stopPropagation(); confirmDeposit('${order.id}')">确认骑手押金</button>` : ""}
          </td>
        </tr>`;
    }

    function render() {
      const q = document.getElementById("q").value.toLowerCase();
      const orders = sortByDateDesc(state.orders.filter(order => JSON.stringify(order).toLowerCase().includes(q)));
      const accounts = sortByDateDesc(
        (state.accounts || []).filter(account => JSON.stringify(account).toLowerCase().includes(q)),
        ["last_login_at"]
      );
      const codOrderRows = sortByDateDesc(orders.filter(order => order.payment_mode === "cod"));
      const prepaidOrderRows = sortByDateDesc(orders.filter(order => order.payment_mode === "prepaid"));
      const usedPaymentIds = new Set(orders.map(order => order.kpay_transaction_id).filter(Boolean));
      const isPrepaidPayment = payment => payment.payment_mode === "prepaid";
      const pendingCodPayments = sortByDateDesc((state.payments || []).filter(payment =>
        !isPrepaidPayment(payment) && !usedPaymentIds.has(payment.id) && JSON.stringify(payment).toLowerCase().includes(q)
      ));
      const pendingPrepaidPayments = sortByDateDesc((state.payments || []).filter(payment =>
        isPrepaidPayment(payment) && !usedPaymentIds.has(payment.id) && JSON.stringify(payment).toLowerCase().includes(q)
      ));
      const codRows = sortByDateDesc([
        ...pendingCodPayments.map(payment => ({ kind: "payment", payment, created_at: payment.created_at })),
        ...codOrderRows.map(order => ({ kind: "order", order, created_at: order.created_at }))
      ]);
      const prepaidRows = sortByDateDesc([
        ...pendingPrepaidPayments.map(payment => ({ kind: "payment", payment, created_at: payment.created_at })),
        ...prepaidOrderRows.map(order => ({ kind: "order", order, created_at: order.created_at }))
      ]);
      const tables = ensureOrderTables();
      const codOrdersTable = tables.cod;
      const prepaidOrdersTable = tables.prepaid;
      if (!codOrdersTable || !prepaidOrdersTable) {
        console.error("后台订单表格节点缺失，请刷新页面。");
        const activePage = document.querySelector(".page.active");
        if (activePage) {
          activePage.insertAdjacentHTML("beforeend", `<div class="empty">后台页面版本不完整，请重新部署最新 main.py 后刷新。</div>`);
        }
        return;
      }

      codOrdersTable.innerHTML = codRows.map(row =>
        row.kind === "payment" ? pendingPaymentRow(row.payment, false) : orderTableRow(row.order, false)
      ).join("");
      if (!codRows.length) {
        codOrdersTable.innerHTML = `<tr><td colspan="8" class="muted">暂无货到付款订单</td></tr>`;
      }
      prepaidOrdersTable.innerHTML = prepaidRows.map(row =>
        row.kind === "payment" ? pendingPaymentRow(row.payment, true) : orderTableRow(row.order, true)
      ).join("");
      if (!prepaidRows.length) {
        prepaidOrdersTable.innerHTML = `<tr><td colspan="8" class="muted">暂无货费已付款订单</td></tr>`;
      }
      if (selectedAccountPhone && !accounts.some(account => account.phone === selectedAccountPhone)) {
        selectedAccountPhone = null;
      }
      document.getElementById("accounts").innerHTML = accounts.map(account => `
        <tr class="account-row ${account.phone === selectedAccountPhone ? "active" : ""}" onclick="selectAccount(${jsValue(account.phone)})">
          <td>${account.avatar_url ? `<img src="${escapeHtml(account.avatar_url)}" alt="头像" style="width:44px;height:44px;object-fit:cover;border-radius:50%;background:#f3f4f6;">` : ""}</td>
          <td>${escapeHtml(account.nickname || "")}</td>
          <td>${account.payment_qr_url ? `<a href="${escapeHtml(account.payment_qr_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()"><img src="${escapeHtml(account.payment_qr_url)}" alt="收款码" title="点击查看收款码" style="width:54px;height:54px;object-fit:cover;border-radius:8px;background:#f3f4f6;border:1px solid #e5e7eb;"></a>` : `<span class="muted">未上传</span>`}</td>
          <td>${escapeHtml(account.phone)}</td>
          <td>${escapeHtml(new Date(account.last_login_at).toLocaleString())}</td>
        </tr>`).join("");
      if (!accounts.length) {
        document.getElementById("accounts").innerHTML = `<tr><td colspan="5" class="muted">暂无账号资料</td></tr>`;
      }
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
          <td>${escapeHtml(order.user_phone)}<br><span class="muted">${escapeHtml(order.rider_phone || "未接单")} ${escapeHtml(order.rider_name || "")}</span></td>
          <td>配送费 ${money(order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td>${settlementInfo(order)}</td>
          <td>${settlementQRCodes(order)}</td>
          <td><span class="pill">${label(order.settlement_status)}</span>${settlementPaidTimes(order)}</td>
          <td>
            ${order.payment_mode === "cod" && order.user_settlement_qr_url && !["paid_to_user","completed"].includes(order.settlement_status) ? `<button onclick="confirmUserSettlement('${order.id}')">确认已转账货费</button>` : ""}
            ${order.rider_settlement_qr_url && !["paid_to_rider","completed"].includes(order.settlement_status) ? `<button onclick="confirmRiderSettlement('${order.id}')">确认已转账送货费</button>` : ""}
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
      render();
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
          <td>配送费 ${money(order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
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
        return "客服主会话";
      }
      if (conversationId.startsWith("order:")) {
        const orderId = conversationId.slice("order:".length);
        const order = state.orders.find(item => item.id.toLowerCase() === orderId.toLowerCase());
        if (order) {
          const otherSide = order.user_phone === phone
            ? (order.rider_phone || "未接单骑手")
            : order.user_phone;
          return `订单 ${order.id.slice(0, 6).toUpperCase()} / 对方：${otherSide}`;
        }
      }
      return conversationId;
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
              <strong>${escapeHtml(message.sender_name)}</strong>
              <span class="muted">${escapeHtml(message.sender_phone || "")} ${escapeHtml(new Date(message.created_at).toLocaleString())}</span><br>
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
      if (!container) return;
      if (!selectedAccountPhone) {
        container.innerHTML = `<div class="empty">点击上方账号，可查看他下的单、接的单和相关聊天记录</div>`;
        return;
      }
      const account = (state.accounts || []).find(item => item.phone === selectedAccountPhone);
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
          <h3>账号详情</h3>
          <div class="row"><b>手机号</b><span>${escapeHtml(selectedAccountPhone)}</span></div>
          <div class="row"><b>昵称</b><span>${escapeHtml(account?.nickname || "")}</span></div>
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
          <strong>${escapeHtml(message.conversation_id)}</strong><br>
          <span class="muted">${escapeHtml(message.sender_name)}：${escapeHtml(message.text || "[图片]")}</span><br>
          <span class="muted">${escapeHtml(new Date(message.created_at).toLocaleString())}</span>
        </button>
      `).join("");

      if (!conversations.length) {
        list.innerHTML = `<div class="empty">暂无客服会话</div>`;
        chat.innerHTML = `<div class="empty">暂无客服消息</div>`;
        title.textContent = "聊天记录";
        return;
      }

      const messages = state.messages
        .filter(message => message.conversation_id === selectedServiceConversationId)
        .sort((a, b) => dateMs(a.created_at) - dateMs(b.created_at));
      title.textContent = selectedServiceConversationId || "聊天记录";
      chat.innerHTML = messages.map(message => `
        <p>
          <strong>${escapeHtml(message.sender_name)}</strong>
          <span class="muted">${escapeHtml(message.sender_phone || "")} ${escapeHtml(new Date(message.created_at).toLocaleString())}</span><br>
          ${escapeHtml(message.text)}
          ${message.image_url ? `<br><img src="${escapeHtml(message.image_url)}" alt="聊天图片">` : ""}
        </p>
      `).join("");
      chat.scrollTop = chat.scrollHeight;
    }

    async function sendServiceReply() {
      const input = document.getElementById("serviceReply");
      const imageInput = document.getElementById("serviceImage");
      const text = input.value.trim();
      const imageFile = imageInput?.files?.[0] || null;
      if (!selectedServiceConversationId) {
        alert("请先选择一个客服会话");
        return;
      }
      if (!text && !imageFile) {
        alert("请输入回复内容或选择图片");
        return;
      }

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
        alert(await response.text());
        return;
      }
      input.value = "";
      if (imageInput) imageInput.value = "";
      await loadData();
      selectServiceConversation(selectedServiceConversationId);
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
      document.getElementById("detailSection").classList.remove("hidden");
      document.getElementById("detail").innerHTML = `
        ${order.goods_image_url ? `<img src="${order.goods_image_url}" alt="商品图">` : `<div class="muted">暂无商品图</div>`}
        <div class="row"><b>订单号</b><span>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</span></div>
        <div class="row"><b>用户</b><span>${escapeHtml(order.user_phone)}</span></div>
        <div class="row"><b>骑手</b><span>${escapeHtml(order.rider_phone || "未接单")} ${escapeHtml(order.rider_name || "")}</span></div>
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
        ${order.user_payment_status !== "confirmed" ? `<button onclick="confirmUserPayment('${order.id}')">确认收到送货费</button>` : ""}
        <button onclick="saveOrder('${order.id}')">保存订单状态</button>
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

    async function confirmUserPayment(id) {
      const order = state.orders.find(item => item.id === id);
      if (order?.kpay_transaction_id) {
        await confirmPrepaidPayment(order.kpay_transaction_id, id);
        return;
      }

      const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_payment_status: "confirmed" })
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
      showDetail(id);
    }

    async function confirmPrepaidPayment(id, orderId = null) {
      const response = await fetch(`/admin/payments/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "confirmed" })
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
      if (orderId) {
        showDetail(orderId);
      }
    }

    async function confirmDeposit(id) {
      const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rider_deposit_status: "confirmed" })
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
      showDetail(id);
    }

    async function saveOrder(id) {
      const body = {
        status: document.getElementById("status").value,
        user_payment_status: document.getElementById("userPayment").value,
        rider_deposit_status: document.getElementById("riderDeposit").value,
        settlement_status: document.getElementById("settlement").value
      };
      const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
      showDetail(id);
    }

    async function saveSettlement(id) {
      const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settlement_status: document.getElementById(`settlement-${id}`).value })
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
    }

    async function confirmRiderSettlement(id) {
      const order = state.orders.find(item => item.id === id);
      const status = order?.payment_mode === "cod" && order?.settlement_status === "paid_to_user" ? "completed" : "paid_to_rider";
      const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settlement_status: status })
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
    }

    async function confirmUserSettlement(id) {
      const order = state.orders.find(item => item.id === id);
      const status = order?.settlement_status === "paid_to_rider" ? "completed" : "paid_to_user";
      const response = await fetch(`/admin/orders/${id}?key=${keyParam()}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settlement_status: status })
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      await loadData();
    }

    document.getElementById("q").addEventListener("input", render);
    showPage(currentPage);
  </script>
</body>
</html>
'''


def parse_coordinate(text: str) -> tuple[float, float] | None:
    text = decoded_google_maps_text(text)
    reliable_coordinate = parse_reliable_google_maps_coordinate(text)
    if reliable_coordinate:
        return reliable_coordinate

    bare_match = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*", text)
    if bare_match:
        lat = float(bare_match.group(1))
        lng = float(bare_match.group(2))
        if is_valid_coordinate(lat, lng):
            return lat, lng

    return None


def parse_reliable_google_maps_coordinate(text: str) -> tuple[float, float] | None:
    text = decoded_google_maps_text(text)
    patterns = [
        (r"@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"q=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"ll=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"query=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"destination=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)", 1, 2),
        (r"!2d(-?\d{1,3}(?:\.\d+)?)!3d(-?\d{1,2}(?:\.\d+)?)", 2, 1),
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
    return "maps.app.goo.gl" in lowered or "goo.gl/maps" in lowered


def google_maps_url_text(text: str) -> str | None:
    text = decoded_google_maps_text(text)
    patterns = [
        r"https?://(?:www\.)?google\.[^\"'\s<>]+/maps[^\"'\s<>]*",
        r"https?://(?:www\.)?google\.[^\"'\s<>]+/search[^\"'\s<>]*",
        r"/maps\?[^\"'\s<>]+",
        r"https?://maps\.app\.goo\.gl/[^\"'\s<>]+",
        r"maps\.app\.goo\.gl/[^\"'\s<>]+",
        r"goo\.gl/maps/[^\"'\s<>]+",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(0).replace("&amp;", "&").strip()
            if value.startswith(("/maps", "/search")):
                value = f"https://www.google.com{value}"
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

    async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
        response = await client.get(
            url_text,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        expanded_url = str(response.url)
        decoded_html = decoded_google_maps_text(response.text)
        if parse_reliable_google_maps_coordinate(decoded_html):
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

    place_match = re.search(r"/(?:maps/)?place/([^?#]+?)(?:/data=|/[@?]|$)", parsed.path, re.IGNORECASE)
    if place_match:
        place_text = unquote(place_match.group(1)).replace("+", " ").strip()
        if place_text:
            return place_text

    return text


async def geocode_location(text: str) -> tuple[float, float]:
    expanded = await expand_location_text(text.strip())

    if is_google_maps_short_link(expanded):
        raise HTTPException(
            status_code=400,
            detail="Google Map 短链接无法解析，请检查链接或重新复制分享链接",
        )

    coordinate = parse_coordinate(expanded)
    if coordinate:
        return coordinate

    query_text = google_maps_query_text(expanded)
    coordinate = parse_coordinate(query_text)
    if coordinate:
        return coordinate

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
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return await route_distance_fallback_km(origin, destination)

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
        return await route_distance_fallback_km(origin, destination)

    try:
        element = payload["rows"][0]["elements"][0]
    except (KeyError, IndexError, TypeError):
        logger.warning("Google Distance Matrix malformed response: %s", payload)
        return await route_distance_fallback_km(origin, destination)

    if element.get("status") != "OK":
        logger.warning("Google Distance Matrix failed: %s", payload)
        return await route_distance_fallback_km(origin, destination)

    meters = element["distance"]["value"]
    return float(meters) / 1000


async def osrm_route_distance_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    origin_value = f"{origin[1]},{origin[0]}"
    destination_value = f"{destination[1]},{destination[0]}"
    url = f"https://router.project-osrm.org/route/v1/driving/{origin_value};{destination_value}"

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(
                url,
                params={
                    "overview": "false",
                    "alternatives": "false",
                    "steps": "false",
                },
            )
            payload = response.json()

        if payload.get("code") == "Ok" and payload.get("routes"):
            return float(payload["routes"][0]["distance"]) / 1000
        logger.warning("OSRM route failed: %s", payload)
    except Exception as error:
        logger.warning("OSRM route request failed: %s", error)

    raise HTTPException(status_code=502, detail="路线距离计算失败，请检查 Google Map Location 后再试")


async def route_distance_fallback_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    try:
        return await osrm_route_distance_km(origin, destination)
    except HTTPException:
        direct_km = haversine_km(origin, destination)
        if direct_km <= 0:
            raise
        estimated_road_km = direct_km * 1.3
        logger.warning(
            "Route APIs failed; using haversine fallback. origin=%s destination=%s direct_km=%.3f estimated_km=%.3f",
            origin,
            destination,
            direct_km,
            estimated_road_km,
        )
        return estimated_road_km


def chat_message_from_row(row: sqlite3.Row) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        text=row["text"],
        sender_type=row["sender_type"],
        sender_name=row["sender_name"],
        sender_phone=row["sender_phone"],
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


@app.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="courier-api",
        timestamp=datetime.now(timezone.utc),
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy_page() -> HTMLResponse:
    return HTMLResponse(
        PRIVACY_POLICY_HTML,
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
                "客服",
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
        sender_name="客服",
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
    record = load_order_record(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="订单不存在")

    order, user_phone, rider_phone = record
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
    if request.settlement_status in ("paid_to_rider", "completed") and not order.rider_settlement_paid_at:
        updates["rider_settlement_paid_at"] = now
    if request.settlement_status in ("paid_to_rider", "completed") and not order.rider_settlement_bill_created_at:
        delivery_fee = order.delivery_fee or order.price
        platform_fee = order.platform_delivery_fee or delivery_platform_fee(delivery_fee)
        rider_amount = order.rider_delivery_fee or delivery_payout_fee(delivery_fee)
        updates["rider_settlement_bill_title"] = "送货费已结算"
        updates["rider_settlement_bill_message"] = (
            f"订单 #{order.id[:6].upper()} 原送货费 {delivery_fee:,.0f} MMK，"
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
def login(request: LoginRequest) -> LoginResponse:
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

    return LoginResponse(
        token=f"dev-token-{phone}",
        user=profile,
    )


@app.get("/account/profile", response_model=UserProfile)
def get_account_profile(authorization: str | None = Header(default=None)) -> UserProfile:
    phone = require_account_phone(authorization)
    profile = load_account_profile(phone)
    if profile:
        return profile
    return save_account(phone, nickname="快送用户")


@app.post("/account/profile", response_model=UserProfile)
def update_account_profile(
    request: UpdateProfileRequest,
    authorization: str | None = Header(default=None),
) -> UserProfile:
    phone = require_account_phone(authorization)
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
    )


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
        main_conversation_id = account_conversation_id("main", authorization, phone)
        with connect_db() as connection:
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
            rows = connection.execute(
                f"""
                SELECT id, conversation_id, text, sender_type, sender_name, sender_phone, image_url, created_at
                FROM chat_messages
                WHERE conversation_id IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*conversation_ids, limit),
            ).fetchall()

        return [chat_message_from_row(row) for row in reversed(rows)]

    conversation_id = account_conversation_id(conversation_id, authorization)
    with connect_db() as connection:
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
    sender_phone = normalize_myanmar_phone(request.sender_phone) if request.sender_phone else None
    text = request.text.strip()
    image_url = request.image_url.strip() if request.image_url else None
    sender_name = request.sender_name.strip() or ("骑手" if request.sender_type == "rider" else "用户")
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
    return [order_for_response(order) for order in load_user_orders(user_phone)]


@app.post("/payments/prepaid", response_model=PrepaidPaymentResponse)
def create_prepaid_payment(
    request: CreatePrepaidPaymentRequest,
    authorization: str | None = Header(default=None),
) -> PrepaidPaymentResponse:
    user_phone = require_account_phone(authorization)
    payment_proof_url = clean_optional_text(request.payment_proof_url)
    if not payment_proof_url:
        raise HTTPException(status_code=400, detail="请上传 KPay 转账截图")

    payment = PrepaidPaymentResponse(
        id=str(uuid4()),
        user_phone=user_phone,
        amount=round(request.amount, 2),
        distance_km=request.distance_km,
        goods_amount=round(request.goods_amount, 2),
        payment_mode=request.payment_mode,
        status="pending",
        created_at=datetime.now(timezone.utc),
        payment_proof_url=payment_proof_url,
    )
    save_prepaid_payment(payment)
    return payment


@app.post("/payments/dinger", response_model=PrepaidPaymentResponse)
async def create_dinger_payment(
    request: CreateDingerPaymentRequest,
    authorization: str | None = Header(default=None),
) -> PrepaidPaymentResponse:
    user_phone = require_account_phone(authorization)
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
    delivery_fee = estimate_price(request.distance_km, request.weight_kg)
    platform_delivery_fee = delivery_platform_fee(delivery_fee)
    rider_delivery_fee = delivery_payout_fee(delivery_fee)
    kpay_transaction_id = clean_optional_text(request.kpay_transaction_id)
    goods_image_url = clean_optional_text(request.goods_image_url)
    payment_proof_url = clean_optional_text(request.payment_proof_url)

    if request.goods_amount <= 0:
        raise HTTPException(status_code=400, detail="请填写货物价格")

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
    accepted_payment_amounts = (delivery_fee, rider_delivery_fee)
    if all(abs(prepaid_payment.amount - amount) > 1 for amount in accepted_payment_amounts):
        raise HTTPException(status_code=400, detail="付款金额和当前订单金额不一致，请重新付款")
    if prepaid_payment.payment_mode != request.payment_mode:
        raise HTTPException(status_code=400, detail="付款方式和当前订单不一致，请重新付款")
    if load_order_record(kpay_transaction_id):
        raise HTTPException(status_code=400, detail="这个付款订单已经创建过，请刷新订单列表")
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
    )
    save_order(order, user_phone=user_phone)
    return order_for_response(order)


@app.get("/orders/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    user_phone = require_account_phone(authorization)
    record = load_order_record(order_id)
    if record:
        order, stored_user_phone, _ = record
        if stored_user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能查看其他账号的订单")
        return order_for_response(order)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/orders/{order_id}/settlement", response_model=OrderResponse)
def request_user_settlement(
    order_id: str,
    request: UserSettlementRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    user_phone = require_account_phone(authorization)
    record = load_order_record(order_id)
    if record:
        order, stored_user_phone, rider_phone = record
        if stored_user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能提交其他账号的收款信息")
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
    return [order_for_response(order) for order in load_rider_orders(rider_phone)]


@app.post("/rider/orders/{order_id}/accept", response_model=OrderResponse)
def accept_order(
    order_id: str,
    request: AcceptOrderRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    record = load_order_record(order_id)
    if record:
        order, user_phone, _ = record
        if order.status != "matching":
            raise HTTPException(status_code=409, detail="订单已被接单或不可接单")
        if order.user_payment_status != "confirmed":
            raise HTTPException(status_code=403, detail="平台确认用户送货费付款后骑手才能接单")
        updated = order.model_copy(update={"status": "accepted", "rider_name": request.rider_name})
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/deposit", response_model=OrderResponse)
def mark_rider_deposit_transferred(
    order_id: str,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能更新其他骑手的押金状态")
        if order.rider_deposit_status == "not_required":
            raise HTTPException(status_code=400, detail="这个订单不需要骑手押金")
        if order.rider_deposit_status == "confirmed":
            return order_for_response(order)
        updated = order.model_copy(update={"rider_deposit_status": "pending"})
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
    allowed = ["picking_up", "delivering", "completed"]
    if request.status not in allowed:
        raise HTTPException(status_code=400, detail="骑手不能设置这个订单状态")

    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能更新其他骑手的订单")
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


@app.post("/rider/orders/{order_id}/settlement", response_model=OrderResponse)
def request_rider_settlement(
    order_id: str,
    request: RiderSettlementRequest,
    authorization: str | None = Header(default=None),
) -> OrderResponse:
    rider_phone = require_account_phone(authorization)
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能提交其他骑手的收款信息")
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
    record = load_order_record(order_id)
    if record:
        order, user_phone, stored_rider_phone = record
        if stored_rider_phone != rider_phone:
            raise HTTPException(status_code=403, detail="不能更新其他骑手的订单位置")
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
    record = load_order_record(order_id)
    if record:
        order, stored_user_phone, rider_phone = record
        if stored_user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能取消其他账号的订单")
        updated = order.model_copy(update={"status": "cancelled"})
        save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
        return order_for_response(updated)
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/distance/estimate", response_model=DistanceEstimateResponse)
async def estimate_distance(request: DistanceEstimateRequest) -> DistanceEstimateResponse:
    pickup = await geocode_location(request.pickup_location)
    dropoff = await geocode_location(request.dropoff_location)
    direct_km = haversine_km(pickup, dropoff)
    if direct_km < 0.05:
        raise HTTPException(status_code=400, detail="取件和收货位置太近，请检查是否选择了同一个 Google Map Location")

    distance_km = round(await route_distance_km(pickup, dropoff), 1)
    if distance_km < 0.1:
        raise HTTPException(status_code=400, detail="路线距离太近，请检查 Google Map Location 是否正确")
    return DistanceEstimateResponse(
        distance_km=distance_km,
        price=estimate_price(distance_km, 1),
        pickup_lat=pickup[0],
        pickup_lng=pickup[1],
        dropoff_lat=dropoff[0],
        dropoff_lng=dropoff[1],
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
