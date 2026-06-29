import math
import logging
import os
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


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
    pickup_lat: float | None = None
    pickup_lng: float | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None


class OrderResponse(BaseModel):
    id: str
    pickup_address: str
    dropoff_address: str
    parcel_type: str
    weight_kg: float
    note: str
    distance_km: float
    price: float
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


orders: list[OrderResponse] = []
sms_codes: dict[str, tuple[str, datetime]] = {}

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
            logger.info(
                "Twilio SMS accepted: sid=%s status=%s to=%s",
                payload.get("sid"),
                payload.get("status"),
                phone,
            )
        except ValueError:
            logger.info("Twilio SMS accepted: status=%s to=%s", response.status_code, phone)
       
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


@app.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="courier-api",
        timestamp=datetime.now(timezone.utc),
    )


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


@app.get("/orders", response_model=list[OrderResponse])
def list_orders() -> list[OrderResponse]:
    return orders


@app.post("/orders", response_model=OrderResponse)
def create_order(request: CreateOrderRequest) -> OrderResponse:
    order = OrderResponse(
        id=str(uuid4()),
        pickup_address=request.pickup_address,
        dropoff_address=request.dropoff_address,
        parcel_type=request.parcel_type,
        weight_kg=request.weight_kg,
        note=request.note,
        distance_km=request.distance_km,
        price=estimate_price(request.distance_km, request.weight_kg),
        status="matching",
        created_at=datetime.now(timezone.utc),
        pickup_lat=request.pickup_lat,
        pickup_lng=request.pickup_lng,
        dropoff_lat=request.dropoff_lat,
        dropoff_lng=request.dropoff_lng,
    )
    orders.insert(0, order)
    return order


@app.get("/orders/{order_id}", response_model=OrderResponse)
def get_order(order_id: str) -> OrderResponse:
    for order in orders:
        if order.id == order_id:
            return order
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/orders/{order_id}/cancel", response_model=OrderResponse)
def cancel_order(order_id: str) -> OrderResponse:
    for index, order in enumerate(orders):
        if order.id == order_id:
            updated = order.model_copy(update={"status": "cancelled"})
            orders[index] = updated
            return updated
    raise HTTPException(status_code=404, detail="订单不存在")

@app.get("/rider/orders", response_model=list[OrderResponse])
def list_rider_orders() -> list[OrderResponse]:
    return [order for order in orders if order.status in ["matching", "accepted", "picking_up", "delivering"]]


@app.post("/rider/orders/{order_id}/accept", response_model=OrderResponse)
def accept_order(order_id: str, request: AcceptOrderRequest) -> OrderResponse:
    for index, order in enumerate(orders):
        if order.id == order_id:
            if order.status != "matching":
                raise HTTPException(status_code=409, detail="订单已被接单或不可接单")
            updated = order.model_copy(update={"status": "accepted", "rider_name": request.rider_name})
            orders[index] = updated
            return updated
    raise HTTPException(status_code=404, detail="订单不存在")


@app.post("/rider/orders/{order_id}/status", response_model=OrderResponse)
def update_rider_order_status(order_id: str, request: UpdateOrderStatusRequest) -> OrderResponse:
    allowed = ["picking_up", "delivering", "completed"]
    if request.status not in allowed:
        raise HTTPException(status_code=400, detail="骑手不能设置这个订单状态")

    for index, order in enumerate(orders):
        if order.id == order_id:
            updated = order.model_copy(update={"status": request.status})
            orders[index] = updated
            return updated
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
    # Placeholder for GCS signed URL integration.
    safe_name = request.file_name.replace("/", "-")
    return SignedUploadResponse(
        upload_url=f"https://storage.googleapis.com/your-bucket/uploads/{safe_name}",
        public_url=f"https://storage.googleapis.com/your-bucket/uploads/{safe_name}",
    )
