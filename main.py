import math
import os
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


app = FastAPI(title="Courier API", version="1.0.0")

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


class CreateOrderRequest(BaseModel):
    pickup_address: str
    dropoff_address: str
    parcel_type: str
    weight_kg: float = Field(gt=0)
    note: str = ""
    distance_km: float = Field(gt=0)


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

class AcceptOrderRequest(BaseModel):
    rider_name: str


class UpdateOrderStatusRequest(BaseModel):
    status: OrderStatus


orders: list[OrderResponse] = []


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
    # First production pass: replace this with SMS verification and a real JWT.
    if request.code != "1234":
        raise HTTPException(status_code=401, detail="验证码错误")

    return LoginResponse(
        token=f"dev-token-{request.phone}",
        user=UserProfile(
            id="user_001",
            phone=request.phone,
            nickname="快送用户",
        ),
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
    )


@app.post("/storage/signed-upload-url", response_model=SignedUploadResponse)
def create_signed_upload_url(request: SignedUploadRequest) -> SignedUploadResponse:
    # Placeholder for GCS signed URL integration.
    safe_name = request.file_name.replace("/", "-")
    return SignedUploadResponse(
        upload_url=f"https://storage.googleapis.com/your-bucket/uploads/{safe_name}",
        public_url=f"https://storage.googleapis.com/your-bucket/uploads/{safe_name}",
    )
