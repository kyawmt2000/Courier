import base64
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
from pydantic import BaseModel, Field

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


class LoginRequest(BaseModel):
    phone: str = Field(min_length=6)
    code: str = Field(min_length=4)


class LoginResponse(BaseModel):
    token: str
    user: UserProfile


class UpdateProfileRequest(BaseModel):
    nickname: str | None = None
    avatar_url: str | None = None


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
    payment_proof_url: str
    payment_mode: PaymentMode = "cod"


class PrepaidPaymentResponse(BaseModel):
    id: str
    user_phone: str
    amount: float
    distance_km: float
    payment_mode: PaymentMode = "cod"
    status: PaymentStatus = "pending"
    created_at: datetime
    confirmed_at: datetime | None = None
    payment_proof_url: str | None = None


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
    user_settlement_name: str | None = None
    user_settlement_qr_url: str | None = None
    user_settlement_requested_at: datetime | None = None
    user_settlement_paid_at: datetime | None = None


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
        "chat": "Chats",
        "chats": "Chats",
        "nrc": "NRC",
        "profile picture": "profile picture",
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


def order_for_response(order: OrderResponse) -> OrderResponse:
    return order.model_copy(
        update={
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


def user_profile_from_account(phone: str, nickname: str | None, avatar_url: str | None) -> UserProfile:
    user_id_digits = re.sub(r"\D", "", phone)
    return UserProfile(
        id=f"user_{user_id_digits}",
        phone=phone,
        nickname=nickname,
        avatar_url=signed_gcs_read_url(avatar_url),
    )


def load_account_profile(phone: str) -> UserProfile | None:
    with connect_db() as connection:
        row = connection.execute(
            """
            SELECT phone, nickname, avatar_url
            FROM accounts
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()

    if not row:
        return None
    return user_profile_from_account(row["phone"], row["nickname"], row["avatar_url"])


def save_account(phone: str, nickname: str | None = None, avatar_url: str | None = None) -> UserProfile:
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO accounts (phone, nickname, avatar_url, last_login_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                nickname = COALESCE(excluded.nickname, accounts.nickname),
                avatar_url = COALESCE(excluded.avatar_url, accounts.avatar_url),
                last_login_at = excluded.last_login_at
            """,
            (phone, nickname, avatar_url, datetime.now(timezone.utc).isoformat()),
        )
    return load_account_profile(phone) or user_profile_from_account(phone, nickname, avatar_url)


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
            SELECT phone, nickname, avatar_url, last_login_at
            FROM accounts
            ORDER BY last_login_at DESC
            """
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        account = dict(row)
        account["avatar_url"] = signed_gcs_read_url(account.get("avatar_url"))
        result.append(account)
    return result


def load_admin_chat_messages(limit: int = 200) -> list[dict]:
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
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #eef2f7; vertical-align: top; }
    th { color: #6b7280; font-weight: 700; }
    tr:hover { background: #f9fafb; }
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
    .service-reply { display: flex; gap: 8px; margin-top: 12px; }
    .service-reply textarea { flex: 1; min-height: 44px; padding: 10px; border: 1px solid #d1d5db; border-radius: 8px; font: inherit; resize: vertical; }
    .service-reply input[type="file"] { max-width: 190px; align-self: center; }
    .empty { padding: 28px; text-align: center; color: #6b7280; background: #f9fafb; border-radius: 8px; }
    .hidden { display: none !important; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } header { flex-wrap: wrap; } .toolbar { margin-left: 0; width: 100%; flex-wrap: wrap; } }
  </style>
</head>
<body>
  <header>
    <h1>快送后台</h1>
    <span class="version">orders-ui-v8</span>
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
      <table>
        <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>送货费付款</th><th>骑手押金</th><th>地址</th><th>操作</th></tr></thead>
        <tbody id="codOrders"></tbody>
      </table>
    </section>
    <section id="page-orders" class="page">
      <h2>货费已付款订单</h2>
      <table>
        <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>送货费付款</th><th>地址</th><th>操作</th></tr></thead>
        <tbody id="orders"></tbody>
      </table>
    </section>
    <section id="detailSection" class="hidden">
      <h2>订单详情</h2>
      <div id="detail" class="detail muted"></div>
    </section>
    <section id="page-accounts" class="page">
      <h2>账号资料</h2>
      <table><thead><tr><th>头像</th><th>手机号</th><th>昵称</th><th>最近登录</th></tr></thead><tbody id="accounts"></tbody></table>
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
    function optionHtml(options, current) {
      return options.map(v => `<option value="${v}" ${v === current ? "selected" : ""}>${label(v)}</option>`).join("");
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
          <table>
            <thead><tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>送货费付款</th><th>地址</th><th>操作</th></tr></thead>
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

    function render() {
      const q = document.getElementById("q").value.toLowerCase();
      const orders = state.orders.filter(order => JSON.stringify(order).toLowerCase().includes(q));
      const codOrderRows = orders.filter(order => order.payment_mode === "cod");
      const prepaidOrderRows = orders.filter(order => order.payment_mode === "prepaid");
      const usedPaymentIds = new Set(orders.map(order => order.kpay_transaction_id).filter(Boolean));
      const pendingCodPayments = (state.payments || []).filter(payment =>
        payment.payment_mode === "cod" && !usedPaymentIds.has(payment.id) && JSON.stringify(payment).toLowerCase().includes(q)
      );
      const pendingPrepaidPayments = (state.payments || []).filter(payment =>
        payment.payment_mode === "prepaid" && !usedPaymentIds.has(payment.id) && JSON.stringify(payment).toLowerCase().includes(q)
      );
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

      codOrdersTable.innerHTML = pendingCodPayments.map(payment => `
        <tr>
          <td><strong>付款申请 #${escapeHtml(payment.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(payment.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(payment.user_phone)}<br><span class="muted">用户已上传送货费截图，等待后台确认后才能下单</span></td>
          <td><span class="pill">${label(payment.status)}</span><br><span class="muted">${label(payment.payment_mode)}</span></td>
          <td>配送费 ${money(payment.amount)}<br><span class="muted">${Number(payment.distance_km || 0).toFixed(1)} km</span></td>
          <td>${payment.payment_proof_url ? `<img src="${escapeHtml(payment.payment_proof_url)}" alt="KPay 转账截图" style="width:84px;height:84px;object-fit:cover;border-radius:8px;background:#f3f4f6;">` : `<span class="muted">无截图</span>`}</td>
          <td><span class="muted">订单创建后显示</span></td>
          <td><span class="muted">后台确认后，用户端才可以点立即下单</span></td>
          <td>${payment.status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmPrepaidPayment('${payment.id}')">确认送货费</button>` : `<span class="pill">已确认</span>`}</td>
        </tr>`).join("") + codOrderRows.map(order => `
        <tr onclick="showDetail('${order.id}')">
          <td><strong>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(order.user_phone)}<br><span class="muted">${escapeHtml(order.rider_phone || "未接单")} ${escapeHtml(order.rider_name || "")}</span></td>
          <td><span class="pill">${label(order.status)}</span><br><span class="muted">${label(order.payment_mode)} / 用户付款：${label(order.user_payment_status)}</span></td>
          <td>配送费 ${money(order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td>${paymentProofCell(order)}</td>
          <td>${riderDepositLabel(order.rider_deposit_status)}</td>
          <td>${escapeHtml(order.pickup_address)}<br><span class="muted">${escapeHtml(order.dropoff_address)}</span></td>
          <td>
            ${order.user_payment_status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmUserPayment('${order.id}')">确认送货费</button>` : ""}
            ${order.rider_deposit_status === "pending" ? `<button onclick="event.stopPropagation(); confirmDeposit('${order.id}')">确认骑手押金</button>` : ""}
          </td>
        </tr>`).join("");
      if (!pendingCodPayments.length && !codOrderRows.length) {
        codOrdersTable.innerHTML = `<tr><td colspan="8" class="muted">暂无货到付款订单</td></tr>`;
      }
      prepaidOrdersTable.innerHTML = pendingPrepaidPayments.map(payment => `
        <tr>
          <td><strong>付款申请 #${escapeHtml(payment.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(payment.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(payment.user_phone)}<br><span class="muted">用户已上传送货费截图，等待后台确认后才能下单</span></td>
          <td><span class="pill">${label(payment.status)}</span><br><span class="muted">${label(payment.payment_mode)}</span></td>
          <td>${money(payment.amount)}<br><span class="muted">${Number(payment.distance_km || 0).toFixed(1)} km</span></td>
          <td>${payment.payment_proof_url ? `<img src="${escapeHtml(payment.payment_proof_url)}" alt="KPay 转账截图" style="width:84px;height:84px;object-fit:cover;border-radius:8px;background:#f3f4f6;">` : `<span class="muted">无截图</span>`}</td>
          <td><span class="muted">后台确认后，用户端才可以点立即下单</span></td>
          <td>${payment.status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmPrepaidPayment('${payment.id}')">确认送货费</button>` : `<span class="pill">已确认</span>`}</td>
        </tr>`).join("") + prepaidOrderRows.map(order => `
        <tr onclick="showDetail('${order.id}')">
          <td><strong>#${escapeHtml(order.id.slice(0, 6).toUpperCase())}</strong><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(order.user_phone)}<br><span class="muted">${escapeHtml(order.rider_phone || "未接单")}</span></td>
          <td><span class="pill">${label(order.status)}</span><br><span class="muted">${label(order.payment_mode)} / 用户付款：${label(order.user_payment_status)}</span></td>
          <td>${money(order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td>${paymentProofCell(order)}</td>
          <td>${escapeHtml(order.pickup_address)}<br><span class="muted">${escapeHtml(order.dropoff_address)}</span></td>
          <td>${order.user_payment_status !== "confirmed" ? `<button onclick="event.stopPropagation(); confirmUserPayment('${order.id}')">确认送货费</button>` : ""}</td>
        </tr>`).join("");
      if (!pendingPrepaidPayments.length && !prepaidOrderRows.length) {
        prepaidOrdersTable.innerHTML = `<tr><td colspan="7" class="muted">暂无货费已付款订单</td></tr>`;
      }
      document.getElementById("accounts").innerHTML = state.accounts.map(account => `
        <tr>
          <td>${account.avatar_url ? `<img src="${escapeHtml(account.avatar_url)}" alt="头像" style="width:44px;height:44px;object-fit:cover;border-radius:50%;background:#f3f4f6;">` : ""}</td>
          <td>${escapeHtml(account.phone)}</td>
          <td>${escapeHtml(account.nickname || "")}</td>
          <td>${escapeHtml(new Date(account.last_login_at).toLocaleString())}</td>
        </tr>`).join("");
      document.getElementById("settlements").innerHTML = orders.map(order => `
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
      renderServiceChat();
    }

    function serviceConversations() {
      const grouped = new Map();
      state.messages.forEach(message => {
        const existing = grouped.get(message.conversation_id);
        if (!existing || new Date(message.created_at) > new Date(existing.created_at)) {
          grouped.set(message.conversation_id, message);
        }
      });
      return Array.from(grouped.values()).sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
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
        .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
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
    text = unquote(text)
    patterns = [
        r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"q=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"ll=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"query=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"destination=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        r"(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None


async def expand_location_text(text: str) -> str:
    if "maps.app.goo.gl" not in text and "goo.gl/maps" not in text:
        return text

    async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
        response = await client.get(text)
        return str(response.url)


def google_maps_query_text(text: str) -> str:
    parsed = urlparse(text)
    if "google.com" not in parsed.netloc and "maps.google" not in parsed.netloc:
        return text

    params = parse_qs(parsed.query)
    for key in ("q", "query", "destination", "daddr"):
        value = params.get(key, [""])[0].strip()
        if value:
            return value

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
        raise HTTPException(
            status_code=400,
            detail=f"Google Map Location 不正确或无法解析：{payload.get('status', 'UNKNOWN')}",
        )

    location = payload["results"][0]["geometry"]["location"]
    return float(location["lat"]), float(location["lng"])


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
        return await osrm_route_distance_km(origin, destination)

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
        return await osrm_route_distance_km(origin, destination)

    try:
        element = payload["rows"][0]["elements"][0]
    except (KeyError, IndexError, TypeError):
        logger.warning("Google Distance Matrix malformed response: %s", payload)
        return await osrm_route_distance_km(origin, destination)

    if element.get("status") != "OK":
        logger.warning("Google Distance Matrix failed: %s", payload)
        return await osrm_route_distance_km(origin, destination)

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

    return estimated_road_km(origin, destination)


def estimated_road_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    return haversine_km(origin, destination) * 1.35


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


@app.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="courier-api",
        timestamp=datetime.now(timezone.utc),
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
    if request.settlement_status in ("paid_to_rider", "completed") and not order.rider_settlement_paid_at:
        updates["rider_settlement_paid_at"] = datetime.now(timezone.utc)
    if request.settlement_status in ("paid_to_user", "completed") and not order.user_settlement_paid_at:
        updates["user_settlement_paid_at"] = datetime.now(timezone.utc)
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
    if nickname is not None and len(nickname) > 40:
        raise HTTPException(status_code=400, detail="用户名最多 40 个字符")
    return save_account(phone, nickname=nickname, avatar_url=avatar_url)


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
        payment_mode=request.payment_mode,
        status="pending",
        created_at=datetime.now(timezone.utc),
        payment_proof_url=payment_proof_url,
    )
    save_prepaid_payment(payment)
    return payment


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
    kpay_transaction_id = clean_optional_text(request.kpay_transaction_id)
    goods_image_url = clean_optional_text(request.goods_image_url)
    payment_proof_url = clean_optional_text(request.payment_proof_url)

    if request.payment_mode == "cod" and request.goods_amount <= 0:
        raise HTTPException(status_code=400, detail="货到付款订单需要填写货物价格")

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
    if abs(prepaid_payment.amount - delivery_fee) > 1:
        raise HTTPException(status_code=400, detail="付款金额和当前配送费不一致，请重新计算后付款")
    payment_proof_url = payment_proof_url or prepaid_payment.payment_proof_url
    user_payment_status: PaymentStatus = prepaid_payment.status
    rider_deposit_status: PaymentStatus = "unpaid" if request.payment_mode == "cod" else "not_required"

    order = OrderResponse(
        id=str(uuid4()),
        pickup_address=request.pickup_address,
        dropoff_address=request.dropoff_address,
        parcel_type=request.parcel_type,
        weight_kg=request.weight_kg,
        note=request.note,
        distance_km=request.distance_km,
        price=delivery_fee,
        delivery_fee=delivery_fee,
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
        if order.payment_mode != "cod":
            raise HTTPException(status_code=400, detail="只有货到付款订单需要骑手押金")
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
            order.payment_mode == "cod"
            and request.status in ["picking_up", "delivering"]
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
    distance_km = max(round(await route_distance_km(pickup, dropoff), 1), 0.1)
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
