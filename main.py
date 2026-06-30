import base64
import json
import math
import logging
import os
import random
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlparse
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

ChatSenderType = Literal["user", "rider"]
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


class LoginRequest(BaseModel):
    phone: str = Field(min_length=6)
    code: str = Field(min_length=4)


class LoginResponse(BaseModel):
    token: str
    user: UserProfile

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


class PrepaidPaymentResponse(BaseModel):
    id: str
    user_phone: str
    amount: float
    distance_km: float
    status: PaymentStatus = "pending"
    created_at: datetime
    confirmed_at: datetime | None = None

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
    
class CreateChatMessageRequest(BaseModel):
    text: str = Field(default="", max_length=1000)
    sender_type: ChatSenderType
    sender_name: str
    sender_phone: str | None = None
    conversation_id: str = "main"
    image_url: str | None = None

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
db_path = Path(os.getenv("COURIER_DB_PATH", os.getenv("CHAT_DB_PATH", "courier_data.sqlite3")))

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
        "chats": "Chats",
        "nrc": "NRC",
        "profile picture": "profile picture",
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


def order_for_response(order: OrderResponse) -> OrderResponse:
    return order.model_copy(
        update={
            "goods_image_url": signed_gcs_read_url(order.goods_image_url),
            "payment_proof_url": signed_gcs_read_url(order.payment_proof_url),
        }
    )

def order_from_row(row: sqlite3.Row) -> OrderResponse:
    return OrderResponse.model_validate(json.loads(row["payload"]))

def prepaid_payment_from_row(row: sqlite3.Row) -> PrepaidPaymentResponse:
    return PrepaidPaymentResponse.model_validate(json.loads(row["payload"]))


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
    return [prepaid_payment_from_row(row).model_dump(mode="json") for row in rows]

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
        or order.payment_mode == "cod"
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


def save_account(phone: str, nickname: str | None = None) -> None:
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO accounts (phone, nickname, last_login_at)
            VALUES (?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                nickname = COALESCE(excluded.nickname, accounts.nickname),
                last_login_at = excluded.last_login_at
            """,
            (phone, nickname, datetime.now(timezone.utc).isoformat()),
        )

def parse_coordinate(text: str) -> tuple[float, float] | None:
    patterns = [
        r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"q=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"ll=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
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


async def geocode_location(text: str) -> tuple[float, float]:
    expanded = await expand_location_text(text.strip())

    coordinate = parse_coordinate(expanded)
    if coordinate:
        return coordinate

    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": expanded, "key": api_key},
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

def chat_message_from_row(row: sqlite3.Row) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        text=row["text"],
        sender_type=row["sender_type"],
        sender_name=row["sender_name"],
        sender_phone=row["sender_phone"],
        image_url=row["image_url"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )

@app.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="courier-api",
        timestamp=datetime.now(timezone.utc),
    )

ADMIN_HTML = r'''
<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>快送后台</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; background: #f6f7fb; color: #111827; }
    header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 18px; }
    input, button { font: inherit; padding: 9px 12px; border-radius: 8px; border: 1px solid #d1d5db; }
    button { background: #2563eb; color: white; border-color: #2563eb; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
    th { color: #6b7280; font-weight: 700; }
    .pill { display: inline-block; padding: 3px 8px; border-radius: 999px; background: #eef2ff; color: #3730a3; font-size: 12px; font-weight: 700; }
    .muted { color: #6b7280; font-size: 12px; }
    .confirm { background: #16a34a; border-color: #16a34a; }
    .danger { color: #dc2626; white-space: pre-wrap; }
  </style>
</head>
<body>
  <header>
    <h1>快送后台</h1>
    <input id="key" type="password" placeholder="后台密码">
    <button onclick="loadData()">刷新</button>
  </header>
  <p id="error" class="danger"></p>
  <table>
    <thead>
      <tr><th>订单</th><th>用户/骑手</th><th>状态</th><th>金额</th><th>骑手押金</th><th>操作</th></tr>
    </thead>
    <tbody id="orders"></tbody>
  </table>
  <script>
  const labels = {
      matching: "待接单", accepted: "已接单", picking_up: "取件中", delivering: "配送中", completed: "已完成", cancelled: "已取消",
      cod: "货到付款", prepaid: "货费已付款",
      not_required: "无需", unpaid: "未转账", pending: "骑手已转，待确认", confirmed: "已确认", rejected: "已拒绝"
    };
    let orders = [];
    function label(value) { return labels[value] || value || ""; }
    function money(value) { return `${Number(value || 0).toLocaleString()} MMK`; }
    function shortCode(id) { return String(id || "").slice(0, 6).toUpperCase(); }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[s]));
    }
    async function loadData() {
      const key = encodeURIComponent(document.getElementById("key").value);
      const res = await fetch(`/admin/data?key=${key}`);
      if (!res.ok) {
        document.getElementById("error").textContent = await res.text();
        return;
      }
      document.getElementById("error").textContent = "";
      const data = await res.json();
      orders = data.orders || [];
      renderOrders();
    }
    function renderOrders() {
      document.getElementById("orders").innerHTML = orders.map(order => `
        <tr>
          <td><strong>#${shortCode(order.id)}</strong><br><span class="muted">${escapeHtml(new Date(order.created_at).toLocaleString())}</span></td>
          <td>${escapeHtml(order.user_phone)}<br><span class="muted">${escapeHtml(order.rider_phone || "未接单")} ${escapeHtml(order.rider_name || "")}</span></td>
          <td><span class="pill">${label(order.status)}</span><br><span class="muted">${label(order.payment_mode)}</span></td>
          <td>配送费 ${money(order.price)}<br><span class="muted">货值 ${money(order.goods_amount)}</span></td>
          <td>${label(order.rider_deposit_status)}</td>
          <td>
              ${order.payment_mode === "cod" && order.rider_deposit_status === "pending" ? `<button class="confirm" onclick="confirmDeposit('${order.id}')">确认骑手押金</button>` : ""}
              ${order.payment_mode === "prepaid" && order.user_payment_status !== "confirmed" ? `<button class="confirm" onclick="confirmUserPayment('${order.id}')">确认收到付款</button>` : ""}
          </td>
        </tr>
      `).join("");
    }
    async function confirmDeposit(id) {
      const key = encodeURIComponent(document.getElementById("key").value);
      const res = await fetch(`/admin/orders/${id}?key=${key}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rider_deposit_status: "confirmed" })
      });
      if (!res.ok) {
        document.getElementById("error").textContent = await res.text();
        return;
      }
      await loadData();
    }
    async function confirmUserPayment(id) {
      const key = encodeURIComponent(document.getElementById("key").value);
      const res = await fetch(`/admin/orders/${id}?key=${key}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_payment_status: "confirmed",
          rider_deposit_status: "not_required"
        })
      });
      if (!res.ok) {
        document.getElementById("error").textContent = await res.text();
        return;
      }
      await loadData();
    }
  </script>
</body>
</html>
'''

@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)

def require_admin_key(key: str | None) -> None:
    expected = (os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_KEY") or "admin123").strip()
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

    result = []
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
            SELECT phone, nickname, last_login_at
            FROM accounts
            ORDER BY last_login_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


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
    return [dict(row) for row in rows]

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
    updated = order.model_copy(update=updates)
    save_order(updated, user_phone=user_phone, rider_phone=rider_phone)
    return order_for_response(updated)

@app.post("/auth/login", response_model=LoginResponse)
def login(request: LoginRequest) -> LoginResponse:
    phone = normalize_myanmar_phone(request.phone)
    stored = sms_codes.get(phone)
    now = datetime.now(timezone.utc)

    if not stored or stored[1] < now:
        raise HTTPException(status_code=401, detail="验证码已过期，请重新获取")

    if request.code != stored[0]:
        raise HTTPException(status_code=401, detail="验证码错误")

    sms_codes.pop(phone, None)
    save_account(phone, nickname="快送用户")

    user_id_digits = re.sub(r"\D", "", phone)

    return LoginResponse(
        token=f"dev-token-{phone}",
        user=UserProfile(
            id=f"user_{user_id_digits}",
            phone=phone,
            nickname="快送用户",
        ),
    )

@app.post("/auth/sms-code", response_model=SendSMSCodeResponse)
async def send_login_sms_code(request: SendSMSCodeRequest) -> SendSMSCodeResponse:
    phone = normalize_myanmar_phone(request.phone)
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
                order_id = str(row["id"])
                conversation_ids.append(f"order:{order_id.lower()}")
                conversation_ids.append(f"order:{order_id.upper()}")

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
        image_url=image_url,
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
    payment = PrepaidPaymentResponse(
        id=str(uuid4()),
        user_phone=user_phone,
        amount=round(request.amount, 2),
        distance_km=request.distance_km,
        status="pending",
        created_at=datetime.now(timezone.utc),
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

    user_payment_status: PaymentStatus = "not_required"
    rider_deposit_status: PaymentStatus = "not_required"

    if request.payment_mode == "cod":
        rider_deposit_status = "unpaid"
    else:
        if not kpay_transaction_id:
            raise HTTPException(status_code=400, detail="请先付款，并等待后台确认收到付款")

        prepaid_payment = load_prepaid_payment(kpay_transaction_id)
        if not prepaid_payment:
            raise HTTPException(status_code=400, detail="付款记录不存在，请重新付款")

        if prepaid_payment.user_phone != user_phone:
            raise HTTPException(status_code=403, detail="不能使用其他账号的付款记录")

        if prepaid_payment.status != "confirmed":
            raise HTTPException(status_code=400, detail="后台确认收到付款后才能下单")

        if abs(prepaid_payment.amount - delivery_fee) > 1:
            raise HTTPException(status_code=400, detail="付款金额和当前配送费不一致，请重新计算后付款")

        user_payment_status = "confirmed"

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
        if order.payment_mode == "prepaid" and order.user_payment_status != "confirmed":
            raise HTTPException(status_code=403, detail="平台确认用户付款后骑手才能接单")
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


@app.post("/distance/estimate", response_model=DistanceEstimateResponse)
async def estimate_distance(request: DistanceEstimateRequest) -> DistanceEstimateResponse:
    pickup = await geocode_location(request.pickup_location)
    dropoff = await geocode_location(request.dropoff_location)
    distance_km = max(round(haversine_km(pickup, dropoff), 1), 0.1)
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
