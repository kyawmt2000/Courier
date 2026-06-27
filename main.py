from datetime import datetime
from typing import Literal
from uuid import uuid4

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


orders: list[OrderResponse] = []


def estimate_price(distance_km: float, weight_kg: float) -> float:
    base = 12
    distance_fee = max(distance_km - 2, 0) * 3.2
    weight_fee = max(weight_kg - 1, 0) * 4
    return round(base + distance_fee + weight_fee, 2)


@app.get("/", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="courier-api",
        timestamp=datetime.utcnow(),
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
        created_at=datetime.utcnow(),
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


@app.post("/storage/signed-upload-url", response_model=SignedUploadResponse)
def create_signed_upload_url(request: SignedUploadRequest) -> SignedUploadResponse:
    # Placeholder for GCS signed URL integration.
    safe_name = request.file_name.replace("/", "-")
    return SignedUploadResponse(
        upload_url=f"https://storage.googleapis.com/your-bucket/uploads/{safe_name}",
        public_url=f"https://storage.googleapis.com/your-bucket/uploads/{safe_name}",
    )
