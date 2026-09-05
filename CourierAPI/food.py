import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

RIDER_DEPOSIT_CONFIRM_WINDOW = timedelta(minutes=5)
logger = logging.getLogger("courier-api.food")


class FoodRestaurantResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    store_city: str = ""
    image_url: str | None = None
    is_open: bool = True
    business_hours_open: str = "09:00"
    business_hours_close: str = "21:00"
    discount_percent: int = 0
    rating: float = 5.0


class FoodMenuItemResponse(BaseModel):
    id: str
    restaurant_id: str
    category: str = ""
    name: str
    description: str = ""
    price_mmk: float
    original_price_mmk: float | None = None
    click_count: int = 0
    image_url: str | None = None
    is_available: bool = True
    status: str = "confirmed"
    rejection_reason: str | None = None
    reviewed_at: str | None = None
    created_at: str | None = None


class CreateFoodMenuItemRequest(BaseModel):
    restaurant_id: str = ""
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    price_mmk: float = Field(ge=0)
    original_price_mmk: float | None = Field(default=None, ge=0)
    image_url: str = Field(min_length=1)


class UpdateFoodMenuItemRequest(BaseModel):
    restaurant_id: str = ""
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    price_mmk: float = Field(ge=0)
    original_price_mmk: float | None = Field(default=None, ge=0)
    image_url: str = Field(min_length=1)


class UpdateFoodMenuAvailabilityRequest(BaseModel):
    is_available: bool


class FoodOrderItemRequest(BaseModel):
    menu_item_id: str
    quantity: int = Field(ge=1, le=99)
    note: str = ""
    menu_item_name: str = ""
    price_mmk: float | None = None


class FoodMenuItemClickRequest(BaseModel):
    amount: int = Field(default=1, ge=1, le=99)


class CreateFoodOrderRequest(BaseModel):
    restaurant_id: str
    delivery_address: str
    delivery_city: str = ""
    delivery_township: str = ""
    delivery_lat: float | None = None
    delivery_lng: float | None = None
    fulfillment_type: str = "delivery"
    payment_method: str = ""
    payment_proof_url: str | None = None
    phone_no: str = ""
    secondary_phone_no: str = ""
    subtotal_mmk: float = 0
    delivery_fee_mmk: float = 0
    discount_mmk: float = 0
    voucher_code: str = ""
    items: list[FoodOrderItemRequest] = Field(default_factory=list)
    note: str = ""


class FoodOrderResponse(BaseModel):
    id: str
    user_phone: str
    restaurant_id: str
    delivery_address: str
    delivery_city: str = ""
    delivery_township: str = ""
    fulfillment_type: str = "delivery"
    payment_method: str = ""
    payment_proof_url: str | None = None
    payment_status: str = "not_required"
    payment_feedback: str | None = None
    phone_no: str = ""
    secondary_phone_no: str = ""
    subtotal_mmk: float = 0
    delivery_fee_mmk: float = 0
    discount_mmk: float = 0
    goods_amount: float = 0
    voucher_code: str = ""
    status: str
    items: list[FoodOrderItemRequest]
    note: str = ""
    restaurant_name: str = ""
    restaurant_location: str = ""
    restaurant_city: str = ""
    restaurant_township: str = ""
    delivery_lat: float | None = None
    delivery_lng: float | None = None
    rider_name: str | None = None
    rider_phone: str | None = None
    rider_deposit_status: str = "not_required"
    rider_deposit_due_at: str | None = None
    rider_deposit_submitted_at: str | None = None
    rider_deposit_proof_url: str | None = None
    accepted_at: str | None = None
    pickup_started_at: str | None = None
    delivery_started_at: str | None = None
    completed_at: str | None = None
    rider_lat: float | None = None
    rider_lng: float | None = None
    rider_location_updated_at: str | None = None
    created_at: str


def _restaurant_location_text(payload: dict[str, object]) -> str:
    store_address = str(payload.get("store_address") or "").strip()
    store_location = str(payload.get("store_location") or "").strip()
    if store_address and store_location:
        return f"{store_address}, Google Map Location: {store_location}"
    return store_address or store_location


class FoodAcceptOrderRequest(BaseModel):
    rider_name: str = Field(min_length=1)
    rider_phone: str | None = None


class FoodUpdateOrderStatusRequest(BaseModel):
    status: str


class FoodUpdateRiderLocationRequest(BaseModel):
    lat: float
    lng: float


class FoodRiderDepositTransferRequest(BaseModel):
    payment_proof_url: str | None = None


class AdminUpdateFoodOrderRequest(BaseModel):
    status: str | None = None
    rider_deposit_status: str | None = None
    payment_status: str | None = None
    payment_feedback: str | None = None


class FoodStoreApplicationRequest(BaseModel):
    store_name: str = Field(min_length=1)
    owner_name: str = Field(min_length=1)
    owner_nrc_front_url: str = Field(min_length=1)
    owner_nrc_back_url: str = Field(min_length=1)
    primary_phone: str = Field(min_length=6)
    secondary_phone: str = Field(min_length=6)
    payment_qr_url: str | None = None
    bank_account: str = ""
    bank_account_name: str = ""
    bank_account_number: str = ""
    store_address: str = Field(min_length=1)
    store_city: str = ""
    store_township: str = ""
    store_location: str = ""
    business_hours_open: str = "09:00"
    business_hours_close: str = "21:00"
    service_types: list[str] = Field(min_length=1)
    restaurant_types: list[str] = Field(default_factory=list, max_length=2)
    signature_dish_image_url: str = Field(min_length=1)
    license_urls: list[str] = Field(default_factory=list, max_length=3)
    menu_urls: list[str] = Field(default_factory=list, max_length=10)
    photo_urls: list[str] = Field(min_length=5, max_length=10)


class FoodStoreApplicationResponse(BaseModel):
    id: str
    user_phone: str
    store_name: str
    owner_name: str
    owner_nrc_front_url: str = ""
    owner_nrc_back_url: str = ""
    primary_phone: str
    secondary_phone: str
    payment_qr_url: str | None = None
    bank_account: str = ""
    bank_account_name: str = ""
    bank_account_number: str = ""
    store_address: str = ""
    store_city: str = ""
    store_township: str = ""
    store_location: str = ""
    business_hours_open: str = "09:00"
    business_hours_close: str = "21:00"
    service_types: list[str]
    restaurant_types: list[str] = Field(default_factory=list)
    signature_dish_image_url: str = ""
    license_urls: list[str] = Field(default_factory=list)
    menu_urls: list[str] = Field(default_factory=list)
    photo_urls: list[str]
    status: str
    rejection_reason: str | None = None
    reviewed_at: str | None = None
    created_at: str
    is_temporarily_closed: bool = False
    deleted_at: str | None = None


class UpdateFoodStoreTemporaryClosureRequest(BaseModel):
    is_closed: bool


class AdminUpdateFoodStoreApplicationRequest(BaseModel):
    status: str
    rejection_reason: str | None = None


class AdminUpdateFoodMenuItemRequest(BaseModel):
    status: str
    rejection_reason: str | None = None


SignUrl = Callable[[str | None], str | None]
DeleteUrl = Callable[[str | None], None]
PROMOTIONAL_MENU_CATEGORIES = {"热门推荐", "优惠专区"}
RESTAURANT_TYPES = {
    "Burmese",
    "Tea Shop",
    "Chinese",
    "Thai",
    "Korean",
    "Japanese",
    "Western",
    "Milk Tea",
    "Coffee",
    "Fast Food",
}

DELIVERY_TOWNSHIPS_BY_CITY = {
    "Yangon": [
        "Ahlone",
        "Bahan",
        "Dagon",
        "Kamayut",
        "Kyauktada",
        "Kyeemyindaing",
        "Lanmadaw",
        "Latha",
        "Pabedan",
        "Sanchaung",
        "Botahtaung",
        "Dagon Myothit (East)",
        "Dagon Myothit (North)",
        "Dagon Myothit (Seikkan)",
        "Dagon Myothit (South)",
        "Dawbon",
        "Mingalartaungnyunt",
        "North Okkalapa",
        "Pazundaung",
        "South Okkalapa",
        "Tamwe",
        "Thaketa",
        "Thingangyun",
        "Yankin",
        "Hlaing",
        "Hlaingthaya",
        "Hmawbi",
        "Htantabin",
        "Insein",
        "Mayangone",
        "Mingaladon",
        "Shwepyitha",
        "Cocokyun",
        "Dala",
        "Kawhmu",
        "Kayan",
        "Kungyangon",
        "Kyauktan",
        "Seikkan",
        "Seikkyi Kanaungto",
        "Thanlyin",
        "Thongwa",
        "Twantay",
    ],
    "Mandalay": ["Aungmyethazan", "Chanayethazan", "Mahaaungmye", "Chanmyathazi", "Pyigyidagun", "Patheingyi"],
    "Naypyidaw": ["Zabuthiri", "Dekkhinathiri", "Pobbathiri", "Ottarathiri", "Zeyathiri", "Pyinmana", "Lewe", "Tatkone"],
    "Bago": ["Bago"],
    "Mawlamyine": ["Mawlamyine"],
    "Taunggyi": ["Taunggyi"],
    "Taungoo": ["Taungoo"],
}

CITY_BOUNDS_WITH_MARGIN = {
    "Yangon": ((15.95, 17.35), (95.65, 96.75), 0.25),
    "Mandalay": ((21.65, 22.35), (95.75, 96.35), 0.20),
    "Naypyidaw": ((19.35, 20.10), (95.70, 96.45), 0.25),
    "Bago": ((17.05, 17.65), (96.15, 96.85), 0.20),
    "Mawlamyine": ((16.20, 16.75), (97.35, 97.90), 0.20),
    "Taunggyi": ((20.55, 21.05), (96.75, 97.30), 0.20),
    "Taungoo": ((18.70, 19.15), (96.20, 96.70), 0.20),
}


def _validate_delivery_city_township(city: str, township: str) -> tuple[str, str]:
    cleaned_city = city.strip()
    cleaned_township = township.strip()
    if not cleaned_city:
        raise HTTPException(status_code=400, detail="请选择城市")
    if cleaned_city not in DELIVERY_TOWNSHIPS_BY_CITY:
        raise HTTPException(status_code=400, detail="暂不支持该城市")
    if not cleaned_township:
        raise HTTPException(status_code=400, detail="请选择镇区")
    if cleaned_township not in DELIVERY_TOWNSHIPS_BY_CITY[cleaned_city]:
        raise HTTPException(status_code=400, detail="请选择该城市内的镇区")
    return cleaned_city, cleaned_township


def _coordinate_from_text(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _coordinate_inside_city(latitude: float, longitude: float, city: str) -> bool:
    bounds = CITY_BOUNDS_WITH_MARGIN.get(city.strip())
    if bounds is None:
        return True
    lat_bounds, lng_bounds, margin = bounds
    return (
        lat_bounds[0] - margin <= latitude <= lat_bounds[1] + margin
        and lng_bounds[0] - margin <= longitude <= lng_bounds[1] + margin
    )


def _normalize_business_hour(value: str, default: str) -> str:
    cleaned = (value or "").strip()
    try:
        parsed = datetime.strptime(cleaned, "%H:%M").time()
    except ValueError:
        return default
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _business_hour_minutes(value: str) -> int:
    parsed = datetime.strptime(_normalize_business_hour(value, "09:00"), "%H:%M").time()
    return parsed.hour * 60 + parsed.minute


def _is_restaurant_open(open_text: str, close_text: str, now: datetime | None = None) -> bool:
    local_now = now or datetime.now(ZoneInfo("Asia/Yangon"))
    current = local_now.hour * 60 + local_now.minute
    start = _business_hour_minutes(open_text)
    end = _business_hour_minutes(close_text)
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _discount_percent(original: float | None, current: float | None) -> int:
    if not original or not current or original <= 0 or current >= original:
        return 0
    return round((original - current) / original * 100)


def init_food_storage(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS food_restaurants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            image_url TEXT,
            is_open INTEGER NOT NULL DEFAULT 1,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS food_menu_items (
            id TEXT PRIMARY KEY,
            restaurant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price_mmk REAL NOT NULL,
            image_url TEXT,
            is_available INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'confirmed',
            rejection_reason TEXT,
            reviewed_at TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS food_orders (
            id TEXT PRIMARY KEY,
            user_phone TEXT NOT NULL,
            restaurant_id TEXT NOT NULL,
            rider_phone TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS food_store_applications (
            id TEXT PRIMARY KEY,
            user_phone TEXT NOT NULL,
            store_name TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            primary_phone TEXT NOT NULL,
            secondary_phone TEXT NOT NULL,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_food_menu_restaurant "
        "ON food_menu_items (restaurant_id, is_available)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_food_orders_user_created "
        "ON food_orders (user_phone, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_food_orders_status_created "
        "ON food_orders (status, created_at)"
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(food_orders)").fetchall()}
    if "rider_phone" not in columns:
        connection.execute("ALTER TABLE food_orders ADD COLUMN rider_phone TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_food_orders_rider_created "
        "ON food_orders (rider_phone, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_food_store_applications_user_created "
        "ON food_store_applications (user_phone, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_food_store_applications_status_created "
        "ON food_store_applications (status, created_at)"
    )
    food_columns = {row["name"] for row in connection.execute("PRAGMA table_info(food_store_applications)").fetchall()}
    if "rejection_reason" not in food_columns:
        connection.execute("ALTER TABLE food_store_applications ADD COLUMN rejection_reason TEXT")
    if "reviewed_at" not in food_columns:
        connection.execute("ALTER TABLE food_store_applications ADD COLUMN reviewed_at TEXT")
    menu_columns = {row["name"] for row in connection.execute("PRAGMA table_info(food_menu_items)").fetchall()}
    if "status" not in menu_columns:
        connection.execute("ALTER TABLE food_menu_items ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")
    if "rejection_reason" not in menu_columns:
        connection.execute("ALTER TABLE food_menu_items ADD COLUMN rejection_reason TEXT")
    if "reviewed_at" not in menu_columns:
        connection.execute("ALTER TABLE food_menu_items ADD COLUMN reviewed_at TEXT")


def _application_from_row(row: sqlite3.Row) -> FoodStoreApplicationResponse:
    payload = json.loads(row["payload"])
    payload.setdefault("owner_nrc_front_url", "")
    payload.setdefault("owner_nrc_back_url", "")
    payload.setdefault("payment_qr_url", None)
    payload.setdefault("bank_account", "")
    payload.setdefault("bank_account_name", "")
    payload.setdefault("bank_account_number", "")
    payload.setdefault("store_address", "")
    payload.setdefault("store_location", "")
    payload.setdefault("business_hours_open", "09:00")
    payload.setdefault("business_hours_close", "21:00")
    payload.setdefault("signature_dish_image_url", "")
    payload.setdefault("license_urls", [])
    payload.setdefault("menu_urls", [])
    payload.setdefault("store_city", "")
    payload.setdefault("store_township", "")
    payload.setdefault("is_temporarily_closed", False)
    payload.setdefault("deleted_at", None)
    payload["status"] = row["status"]
    payload["rejection_reason"] = row["rejection_reason"]
    payload["reviewed_at"] = row["reviewed_at"]
    return FoodStoreApplicationResponse(**payload)


def _signed_application(application: FoodStoreApplicationResponse, sign_url: SignUrl | None) -> FoodStoreApplicationResponse:
    if sign_url is None:
        return application
    return application.model_copy(
        update={
            "owner_nrc_front_url": sign_url(application.owner_nrc_front_url) or "",
            "owner_nrc_back_url": sign_url(application.owner_nrc_back_url) or "",
            "payment_qr_url": sign_url(application.payment_qr_url),
            "signature_dish_image_url": sign_url(application.signature_dish_image_url) or "",
            "license_urls": [sign_url(url) or url for url in application.license_urls],
            "menu_urls": [sign_url(url) or url for url in application.menu_urls],
            "photo_urls": [sign_url(url) or url for url in application.photo_urls],
        }
    )


def _stored_url(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().split("?", 1)[0]


def _build_store_application(
    request: FoodStoreApplicationRequest,
    *,
    application_id: str,
    user_phone: str,
    status: str,
    created_at: str,
    rejection_reason: str | None = None,
    reviewed_at: str | None = None,
) -> FoodStoreApplicationResponse:
    service_types = [
        value.strip()
        for value in request.service_types
        if value.strip() in {"外卖", "堂食"}
    ]
    if not service_types:
        raise HTTPException(status_code=400, detail="请选择经营方式")
    restaurant_types = []
    for value in request.restaurant_types:
        cleaned = value.strip()
        if cleaned in RESTAURANT_TYPES and cleaned not in restaurant_types:
            restaurant_types.append(cleaned)
    if len(restaurant_types) > 2:
        raise HTTPException(status_code=400, detail="餐厅类型最多选择 2 个")
    bank_account_name = request.bank_account_name.strip()
    bank_account_number = request.bank_account_number.strip()
    bank_account = request.bank_account.strip() or " / ".join(
        value for value in [bank_account_name, bank_account_number] if value
    )
    business_hours_open = _normalize_business_hour(request.business_hours_open, "")
    business_hours_close = _normalize_business_hour(request.business_hours_close, "")
    if not business_hours_open or not business_hours_close:
        raise HTTPException(status_code=400, detail="请选择营业时间")
    payment_qr_url = _stored_url(request.payment_qr_url) if request.payment_qr_url else None
    if (bank_account_name and not bank_account_number) or (bank_account_number and not bank_account_name):
        raise HTTPException(status_code=400, detail="请填写银行名字和账号")
    if not payment_qr_url and not bank_account:
        raise HTTPException(status_code=400, detail="请上传收款码或填写银行账号")
    if len(request.license_urls) > 3:
        raise HTTPException(status_code=400, detail="营业执照最多上传 3 张")
    if len(request.menu_urls) > 10:
        raise HTTPException(status_code=400, detail="菜单最多上传 10 张")
    store_city, store_township = _validate_delivery_city_township(request.store_city, request.store_township)
    store_coordinate = _coordinate_from_text(request.store_location)
    if store_coordinate and not _coordinate_inside_city(store_coordinate[0], store_coordinate[1], store_city):
        raise HTTPException(status_code=400, detail=f"请选择 {store_city} 内的 Google Map Location")

    return FoodStoreApplicationResponse(
        id=application_id,
        user_phone=user_phone,
        store_name=request.store_name.strip(),
        owner_name=request.owner_name.strip(),
        owner_nrc_front_url=_stored_url(request.owner_nrc_front_url) or "",
        owner_nrc_back_url=_stored_url(request.owner_nrc_back_url) or "",
        primary_phone=request.primary_phone.strip(),
        secondary_phone=request.secondary_phone.strip(),
        payment_qr_url=payment_qr_url,
        bank_account=bank_account,
        bank_account_name=bank_account_name,
        bank_account_number=bank_account_number,
        store_address=request.store_address.strip(),
        store_city=store_city,
        store_township=store_township,
        store_location=request.store_location.strip(),
        business_hours_open=business_hours_open,
        business_hours_close=business_hours_close,
        service_types=service_types,
        restaurant_types=restaurant_types,
        signature_dish_image_url=_stored_url(request.signature_dish_image_url) or "",
        license_urls=[_stored_url(url) or url for url in request.license_urls],
        menu_urls=[_stored_url(url) or url for url in request.menu_urls],
        photo_urls=[_stored_url(url) or url for url in request.photo_urls],
        status=status,
        rejection_reason=rejection_reason,
        reviewed_at=reviewed_at,
        created_at=created_at,
    )


def _menu_item_from_row(row: sqlite3.Row, sign_url: SignUrl | None = None) -> FoodMenuItemResponse:
    payload = json.loads(row["payload"] or "{}")
    image_url = row["image_url"]
    original_price_mmk = payload.get("original_price_mmk")
    return FoodMenuItemResponse(
        id=row["id"],
        restaurant_id=row["restaurant_id"],
        category=payload.get("category", ""),
        name=row["name"],
        description=row["description"],
        price_mmk=row["price_mmk"],
        original_price_mmk=original_price_mmk if original_price_mmk is not None else row["price_mmk"],
        click_count=int(payload.get("click_count", 0) or 0),
        image_url=sign_url(image_url) if sign_url else image_url,
        is_available=bool(row["is_available"]),
        status=row["status"],
        rejection_reason=row["rejection_reason"],
        reviewed_at=row["reviewed_at"],
        created_at=row["created_at"],
    )


def load_admin_store_applications(db_path: Path, sign_url: SignUrl | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT payload, status, rejection_reason, reviewed_at
            FROM food_store_applications
            WHERE json_extract(payload, '$.deleted_at') IS NULL
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [_signed_application(_application_from_row(row), sign_url).model_dump(mode="json") for row in rows]


def load_admin_deleted_store_applications(db_path: Path, sign_url: SignUrl | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT payload, status, rejection_reason, reviewed_at
            FROM food_store_applications
            WHERE json_extract(payload, '$.deleted_at') IS NOT NULL
            ORDER BY json_extract(payload, '$.deleted_at') DESC
            """
        ).fetchall()
    return [_signed_application(_application_from_row(row), sign_url).model_dump(mode="json") for row in rows]


def load_admin_menu_items(db_path: Path, sign_url: SignUrl | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT item.id, item.restaurant_id, item.name, item.description, item.price_mmk,
                   item.image_url, item.is_available, item.status, item.rejection_reason,
                   item.reviewed_at, item.created_at, item.payload,
                   store.store_name, store.owner_name, store.primary_phone, store.user_phone
            FROM food_menu_items item
            LEFT JOIN food_store_applications store ON store.id = item.restaurant_id
            ORDER BY item.created_at DESC
            """
        ).fetchall()
    items: list[dict] = []
    for row in rows:
        data = _menu_item_from_row(row, sign_url).model_dump(mode="json")
        data["store_name"] = row["store_name"] or ""
        data["owner_name"] = row["owner_name"] or ""
        data["primary_phone"] = row["primary_phone"] or ""
        data["user_phone"] = row["user_phone"] or ""
        items.append(data)
    return items


def _food_order_from_payload(payload: str | None) -> FoodOrderResponse:
    return FoodOrderResponse(**json.loads(payload or "{}"))


def _food_order_payload(order: FoodOrderResponse) -> str:
    return json.dumps(order.model_dump(mode="json"), ensure_ascii=False)


def _is_valid_food_order_phone(phone: str) -> bool:
    return phone.isdigit() and len(phone) in (9, 11)


def _enrich_food_order_items(connection: sqlite3.Connection, order: FoodOrderResponse) -> FoodOrderResponse:
    enriched_items: list[FoodOrderItemRequest] = []
    for order_item in order.items:
        row = connection.execute(
            """
            SELECT name, price_mmk
            FROM food_menu_items
            WHERE id = ? AND restaurant_id = ?
            """,
            (order_item.menu_item_id, order.restaurant_id),
        ).fetchone()
        if row:
            enriched_items.append(
                order_item.model_copy(
                    update={
                        "menu_item_name": row["name"] or order_item.menu_item_name,
                        "price_mmk": float(row["price_mmk"] or 0),
                    }
                )
            )
        else:
            enriched_items.append(order_item)
    return order.model_copy(update={"items": enriched_items})


def _food_order_for_admin(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    sign_url: SignUrl | None = None,
) -> dict:
    order = _enrich_food_order_items(connection, _food_order_from_payload(row["payload"]))
    if sign_url and order.rider_deposit_proof_url:
        order = order.model_copy(update={"rider_deposit_proof_url": sign_url(order.rider_deposit_proof_url)})
    if sign_url and order.payment_proof_url:
        order = order.model_copy(update={"payment_proof_url": sign_url(order.payment_proof_url)})
    data = order.model_dump(mode="json")
    data["user_phone"] = row["user_phone"]
    data["rider_phone"] = row["rider_phone"]
    data["user_nickname"] = row["user_nickname"]
    data["user_email"] = row["user_email"]
    data["rider_nickname"] = row["rider_nickname"]
    data["rider_email"] = row["rider_email"]
    return data


def load_admin_food_orders(
    db_path: Path,
    sign_url: SignUrl | None = None,
    limit: int = 500,
) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                food_orders.user_phone,
                food_orders.rider_phone,
                user_account.nickname AS user_nickname,
                user_account.email AS user_email,
                rider_account.nickname AS rider_nickname,
                rider_account.email AS rider_email,
                food_orders.payload
            FROM food_orders
            LEFT JOIN accounts AS user_account
                ON user_account.phone = food_orders.user_phone
            LEFT JOIN accounts AS rider_account
                ON rider_account.phone = food_orders.rider_phone
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_food_order_for_admin(connection, row, sign_url) for row in rows]


def update_admin_food_order(
    db_path: Path,
    order_id: str,
    request: AdminUpdateFoodOrderRequest,
    sign_url: SignUrl | None = None,
) -> dict:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT payload FROM food_orders WHERE id = ? LIMIT 1",
            (order_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="外卖订单不存在")
        order = _food_order_from_payload(row["payload"])
        updates = {
            name: value
            for name, value in {
                "status": request.status,
                "rider_deposit_status": request.rider_deposit_status,
                "payment_status": request.payment_status,
                "payment_feedback": request.payment_feedback,
            }.items()
            if value is not None
        }
        if request.payment_status == "confirmed" and order.status == "payment_pending":
            updates["status"] = "pending"
        if request.payment_status == "rejected":
            updates["status"] = "cancelled"
        if request.rider_deposit_status == "confirmed":
            updates["rider_deposit_due_at"] = None
        updated = _enrich_food_order_items(connection, order.model_copy(update=updates))
        connection.execute(
            """
            UPDATE food_orders
            SET status = ?, rider_phone = ?, payload = ?
            WHERE id = ?
            """,
            (updated.status, updated.rider_phone, _food_order_payload(updated), updated.id),
        )
        admin_row = connection.execute(
            """
            SELECT
                food_orders.user_phone,
                food_orders.rider_phone,
                user_account.nickname AS user_nickname,
                user_account.email AS user_email,
                rider_account.nickname AS rider_nickname,
                rider_account.email AS rider_email,
                food_orders.payload
            FROM food_orders
            LEFT JOIN accounts AS user_account
                ON user_account.phone = food_orders.user_phone
            LEFT JOIN accounts AS rider_account
                ON rider_account.phone = food_orders.rider_phone
            WHERE food_orders.id = ?
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        return _food_order_for_admin(connection, admin_row, sign_url)


def delete_admin_food_order(
    db_path: Path,
    order_id: str,
    delete_url: DeleteUrl | None = None,
) -> dict:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT payload FROM food_orders WHERE id = ? LIMIT 1",
            (order_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="外卖订单不存在")

        order = _food_order_from_payload(row["payload"])
        image_urls = [
            order.payment_proof_url,
            order.rider_deposit_proof_url,
        ]
        connection.execute("DELETE FROM food_orders WHERE id = ?", (order_id,))

    if delete_url:
        for image_url in dict.fromkeys(url for url in image_urls if url):
            delete_url(image_url)

    return {"status": "deleted", "id": order_id}


def update_admin_store_application(
    db_path: Path,
    application_id: str,
    request: AdminUpdateFoodStoreApplicationRequest,
    sign_url: SignUrl | None = None,
) -> FoodStoreApplicationResponse:
    if request.status not in {"pending", "confirmed", "rejected"}:
        raise HTTPException(status_code=400, detail="店铺状态不正确")
    reason = (request.rejection_reason or "").strip()
    if request.status == "rejected" and not reason:
        raise HTTPException(status_code=400, detail="请填写拒绝原因")

    reviewed_at = datetime.now(timezone.utc).isoformat() if request.status in {"confirmed", "rejected"} else None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, payload, status, rejection_reason, reviewed_at
            FROM food_store_applications
            WHERE id = ?
            """,
            (application_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="店铺注册不存在")

        application = _application_from_row(row).model_copy(
            update={
                "status": request.status,
                "rejection_reason": reason if request.status == "rejected" else None,
                "reviewed_at": reviewed_at,
            }
        )
        connection.execute(
            """
            UPDATE food_store_applications
            SET status = ?, rejection_reason = ?, reviewed_at = ?, payload = ?
            WHERE id = ?
            """,
            (
                application.status,
                application.rejection_reason,
                application.reviewed_at,
                json.dumps(application.model_dump(mode="json"), ensure_ascii=False),
                application.id,
            ),
        )
        if application.status == "confirmed":
            restaurant_id = application.id
            connection.execute(
                """
                INSERT INTO food_restaurants (id, name, description, image_url, is_open, payload, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    image_url = excluded.image_url,
                    is_open = 1,
                    payload = excluded.payload
                """,
                (
                    restaurant_id,
                    application.store_name,
                    " / ".join([*application.service_types, *application.restaurant_types]),
                    application.signature_dish_image_url or (application.photo_urls[0] if application.photo_urls else None),
                    json.dumps(application.model_dump(mode="json"), ensure_ascii=False),
                    application.created_at,
                ),
            )
    return _signed_application(application, sign_url)


def update_admin_menu_item(
    db_path: Path,
    menu_item_id: str,
    request: AdminUpdateFoodMenuItemRequest,
    sign_url: SignUrl | None = None,
) -> FoodMenuItemResponse:
    if request.status not in {"pending", "confirmed", "rejected"}:
        raise HTTPException(status_code=400, detail="菜品状态不正确")
    reason = (request.rejection_reason or "").strip()
    if request.status == "rejected" and not reason:
        raise HTTPException(status_code=400, detail="请填写拒绝原因")

    reviewed_at = datetime.now(timezone.utc).isoformat() if request.status in {"confirmed", "rejected"} else None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                   status, rejection_reason, reviewed_at, created_at, payload
            FROM food_menu_items
            WHERE id = ?
            """,
            (menu_item_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="菜品不存在")

        item = _menu_item_from_row(row).model_copy(
            update={
                "status": request.status,
                "rejection_reason": reason if request.status == "rejected" else None,
                "reviewed_at": reviewed_at,
                "is_available": request.status == "confirmed",
            }
        )
        connection.execute(
            """
            UPDATE food_menu_items
            SET status = ?, rejection_reason = ?, reviewed_at = ?, is_available = ?, payload = ?
            WHERE id = ?
            """,
            (
                item.status,
                item.rejection_reason,
                item.reviewed_at,
                1 if item.is_available else 0,
                json.dumps(item.model_dump(mode="json"), ensure_ascii=False),
                item.id,
            ),
        )
    if sign_url and item.image_url:
        item = item.model_copy(update={"image_url": sign_url(item.image_url)})
    return item


def delete_admin_store_application(
    db_path: Path,
    application_id: str,
    delete_url: DeleteUrl | None = None,
) -> dict:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, payload, status, rejection_reason, reviewed_at
            FROM food_store_applications
            WHERE id = ?
            """,
            (application_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="店铺注册不存在")

        application = _application_from_row(row)
        rows = connection.execute(
            """
            SELECT image_url
            FROM food_menu_items
            WHERE restaurant_id = ?
            """,
            (application_id,),
        ).fetchall()
        menu_image_urls = [item["image_url"] for item in rows]

        connection.execute("DELETE FROM food_menu_items WHERE restaurant_id = ?", (application_id,))
        connection.execute("DELETE FROM food_restaurants WHERE id = ?", (application_id,))
        connection.execute("DELETE FROM food_store_applications WHERE id = ?", (application_id,))

    image_urls = [
        application.owner_nrc_front_url,
        application.owner_nrc_back_url,
        application.payment_qr_url,
        application.signature_dish_image_url,
        *application.license_urls,
        *application.menu_urls,
        *application.photo_urls,
        *menu_image_urls,
    ]
    if delete_url:
        for image_url in image_urls:
            delete_url(image_url)

    return {"status": "deleted", "id": application_id}


def delete_admin_menu_item(
    db_path: Path,
    menu_item_id: str,
    delete_url: DeleteUrl | None = None,
) -> dict:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, image_url
            FROM food_menu_items
            WHERE id = ?
            """,
            (menu_item_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="菜品不存在")

        image_url = row["image_url"]
        connection.execute("DELETE FROM food_menu_items WHERE id = ?", (menu_item_id,))

    if delete_url:
        delete_url(image_url)

    return {"status": "deleted", "id": menu_item_id}


def create_food_router(
    db_path: Path,
    require_account_phone: Callable[[str | None], str],
    sign_url: SignUrl | None = None,
    notify_restaurant_update: Callable[[str, str, str, str], None] | None = None,
    notify_user: Callable[[str | None, str, str, str], None] | None = None,
    require_approved_rider: Callable[[str], None] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/food", tags=["food"])

    def connect_db() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def food_order_from_row(row: sqlite3.Row) -> FoodOrderResponse:
        payload = json.loads(row["payload"] or "{}")
        payload.setdefault("delivery_city", "")
        payload.setdefault("delivery_township", "")
        payload.setdefault("fulfillment_type", "delivery")
        payload.setdefault("payment_method", "")
        payload.setdefault("payment_status", "not_required")
        payload.setdefault("phone_no", "")
        payload.setdefault("secondary_phone_no", "")
        payload.setdefault("subtotal_mmk", 0)
        payload.setdefault("delivery_fee_mmk", 0)
        payload.setdefault("discount_mmk", 0)
        payload.setdefault("goods_amount", 0)
        payload.setdefault("voucher_code", "")
        payload.setdefault("items", [])
        payload.setdefault("note", "")
        payload.setdefault("restaurant_name", "")
        payload.setdefault("restaurant_location", "")
        payload.setdefault("restaurant_city", "")
        payload.setdefault("restaurant_township", "")
        payload.setdefault("rider_deposit_status", "not_required")
        return FoodOrderResponse(**payload)

    def enrich_food_order_items(connection: sqlite3.Connection, order: FoodOrderResponse) -> FoodOrderResponse:
        enriched_items: list[FoodOrderItemRequest] = []
        for order_item in order.items:
            row = connection.execute(
                """
                SELECT name, price_mmk
                FROM food_menu_items
                WHERE id = ? AND restaurant_id = ?
                """,
                (order_item.menu_item_id, order.restaurant_id),
            ).fetchone()
            if row:
                enriched_items.append(
                    order_item.model_copy(
                        update={
                            "menu_item_name": row["name"] or order_item.menu_item_name,
                            "price_mmk": float(row["price_mmk"] or 0),
                        }
                    )
                )
            else:
                enriched_items.append(order_item)
        update: dict[str, object] = {"items": enriched_items}
        restaurant_row = connection.execute(
            """
            SELECT payload
            FROM food_store_applications
            WHERE id = ?
            LIMIT 1
            """,
            (order.restaurant_id,),
        ).fetchone()
        if restaurant_row:
            restaurant_payload = json.loads(restaurant_row["payload"] or "{}")
            update.update(
                {
                    "restaurant_name": order.restaurant_name or restaurant_payload.get("store_name") or "",
                    "restaurant_location": _restaurant_location_text(restaurant_payload) or order.restaurant_location or "",
                    "restaurant_city": restaurant_payload.get("store_city") or order.restaurant_city or "",
                    "restaurant_township": restaurant_payload.get("store_township") or order.restaurant_township or "",
                }
            )
        return order.model_copy(update=update)

    def save_food_order(connection: sqlite3.Connection, order: FoodOrderResponse) -> None:
        connection.execute(
            """
            UPDATE food_orders
            SET status = ?, rider_phone = ?, payload = ?
            WHERE id = ?
            """,
            (
                order.status,
                order.rider_phone,
                json.dumps(order.model_dump(mode="json"), ensure_ascii=False),
                order.id,
            ),
        )

    def notify_food_order_user(order: FoodOrderResponse, key_suffix: str, title: str, message: str) -> None:
        if notify_user:
            notify_user(order.user_phone, f"food-order-{order.id}-{key_suffix}", title, message)

    def food_order_matches_rider_city(order: FoodOrderResponse, requested_city: str) -> bool:
        normalized_city = requested_city.strip().casefold()
        if not normalized_city:
            return True
        if order.rider_phone:
            return True
        if order.status != "pending":
            return False
        candidate_cities = [
            order.restaurant_city,
            order.delivery_city,
        ]
        known_cities = [city.strip().casefold() for city in candidate_cities if city.strip()]
        if not known_cities:
            return True
        return any(city == normalized_city for city in known_cities)

    @router.get("/restaurants", response_model=list[FoodRestaurantResponse])
    def list_restaurants() -> list[FoodRestaurantResponse]:
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT store.id, store.payload,
                       COALESCE(MAX(
                           CASE
                               WHEN item.status = 'confirmed'
                                AND item.is_available = 1
                                AND json_extract(item.payload, '$.original_price_mmk') > item.price_mmk
                               THEN CAST(
                                   ROUND(
                                       (json_extract(item.payload, '$.original_price_mmk') - item.price_mmk)
                                       / json_extract(item.payload, '$.original_price_mmk') * 100
                                   ) AS INTEGER
                               )
                               ELSE 0
                           END
                       ), 0) AS discount_percent
                FROM food_store_applications store
                LEFT JOIN food_menu_items item ON item.restaurant_id = store.id
                WHERE store.status = 'confirmed'
                  AND json_extract(store.payload, '$.deleted_at') IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM food_menu_items item
                      WHERE item.restaurant_id = store.id
                        AND item.status = 'confirmed'
                        AND item.is_available = 1
                  )
                GROUP BY store.id, store.payload
                ORDER BY store.store_name COLLATE NOCASE
                """
            ).fetchall()
        restaurants: list[FoodRestaurantResponse] = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            service_types = payload.get("service_types") or []
            restaurant_types = payload.get("restaurant_types") or []
            business_hours_open = _normalize_business_hour(payload.get("business_hours_open") or "09:00", "09:00")
            business_hours_close = _normalize_business_hour(payload.get("business_hours_close") or "21:00", "21:00")
            description = " / ".join([*service_types, *restaurant_types]).strip(" /")
            image_url = payload.get("signature_dish_image_url") or (payload.get("photo_urls") or [None])[0]
            restaurants.append(
                FoodRestaurantResponse(
                    id=row["id"],
                    name=payload.get("store_name") or "",
                    description=description,
                    store_city=payload.get("store_city") or "",
                    image_url=sign_url(image_url) if sign_url else image_url,
                    is_open=not bool(payload.get("is_temporarily_closed")) and _is_restaurant_open(business_hours_open, business_hours_close),
                    business_hours_open=business_hours_open,
                    business_hours_close=business_hours_close,
                    discount_percent=int(row["discount_percent"] or 0),
                    rating=float(payload.get("rating") or 5.0),
                )
            )
        return restaurants

    @router.get("/restaurants/{restaurant_id}/menu", response_model=list[FoodMenuItemResponse])
    def list_menu_items(restaurant_id: str) -> list[FoodMenuItemResponse]:
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                       status, rejection_reason, reviewed_at, created_at, payload
                FROM food_menu_items
                WHERE restaurant_id = ? AND status = 'confirmed' AND is_available = 1
                ORDER BY name COLLATE NOCASE
                """,
                (restaurant_id,),
            ).fetchall()
        return [_menu_item_from_row(row, sign_url) for row in rows]

    @router.post("/stores/menu-items", response_model=FoodMenuItemResponse)
    def create_store_menu_item(
        request: CreateFoodMenuItemRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodMenuItemResponse:
        user_phone = require_account_phone(authorization)
        category = request.category.strip()
        title = request.title.strip()
        description = request.description.strip()
        price_mmk = request.price_mmk
        original_price_mmk = request.original_price_mmk if request.original_price_mmk is not None else price_mmk
        image_url = request.image_url.strip()
        if not category:
            raise HTTPException(status_code=400, detail="请填写菜品类型")
        if category in PROMOTIONAL_MENU_CATEGORIES:
            raise HTTPException(status_code=400, detail="请选择普通菜品类型")
        if not title:
            raise HTTPException(status_code=400, detail="请填写菜品标题")
        if not image_url:
            raise HTTPException(status_code=400, detail="请上传菜品图片")
        if price_mmk > original_price_mmk:
            raise HTTPException(status_code=400, detail="优惠价格不能高于原价")

        created_at = datetime.now(timezone.utc).isoformat()
        with connect_db() as connection:
            if request.restaurant_id.strip():
                application_row = connection.execute(
                    """
                    SELECT id, payload
                    FROM food_store_applications
                    WHERE id = ? AND user_phone = ? AND status = 'confirmed'
                    LIMIT 1
                    """,
                    (request.restaurant_id.strip(), user_phone),
                ).fetchone()
            else:
                application_row = connection.execute(
                    """
                    SELECT id, payload
                    FROM food_store_applications
                    WHERE user_phone = ? AND status = 'confirmed'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_phone,),
                ).fetchone()
            if not application_row:
                raise HTTPException(status_code=403, detail="店铺审核确认后才能上传菜品")

            restaurant_id = application_row["id"]
            menu_item = FoodMenuItemResponse(
                id=str(uuid4()),
                restaurant_id=restaurant_id,
                category=category,
                name=title,
                description=description,
                price_mmk=price_mmk,
                original_price_mmk=original_price_mmk,
                click_count=0,
                image_url=image_url,
                is_available=False,
                status="pending",
                created_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO food_menu_items (
                    id, restaurant_id, name, description, price_mmk,
                    image_url, is_available, status, payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    menu_item.id,
                    restaurant_id,
                    menu_item.name,
                    menu_item.description,
                    menu_item.price_mmk,
                    menu_item.image_url,
                    menu_item.status,
                    json.dumps(menu_item.model_dump(mode="json"), ensure_ascii=False),
                    created_at,
                ),
            )
        return menu_item

    @router.patch("/stores/menu-items/{menu_item_id}", response_model=FoodMenuItemResponse)
    def update_store_menu_item(
        menu_item_id: str,
        request: UpdateFoodMenuItemRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodMenuItemResponse:
        user_phone = require_account_phone(authorization)
        category = request.category.strip()
        title = request.title.strip()
        description = request.description.strip()
        image_url = request.image_url.strip()
        if not category:
            raise HTTPException(status_code=400, detail="请填写菜品类型")
        if category in PROMOTIONAL_MENU_CATEGORIES:
            raise HTTPException(status_code=400, detail="请选择普通菜品类型")
        if not title:
            raise HTTPException(status_code=400, detail="请填写菜品标题")
        if not image_url:
            raise HTTPException(status_code=400, detail="请上传菜品图片")

        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                       status, rejection_reason, reviewed_at, created_at, item.payload, store.payload AS store_payload
                FROM food_menu_items item
                JOIN food_store_applications store ON store.id = item.restaurant_id
                WHERE item.id = ? AND store.user_phone = ? AND store.status = 'confirmed'
                """,
                (menu_item_id, user_phone),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="菜品不存在")
            if request.restaurant_id.strip() and row["restaurant_id"] != request.restaurant_id.strip():
                raise HTTPException(status_code=404, detail="菜品不存在")

            existing_item = _menu_item_from_row(row)
            if request.original_price_mmk is not None:
                original_price_mmk = request.original_price_mmk
                if request.price_mmk > original_price_mmk:
                    raise HTTPException(status_code=400, detail="优惠价格不能高于原价")
            else:
                original_price_mmk = existing_item.original_price_mmk or existing_item.price_mmk
                if request.price_mmk < existing_item.price_mmk:
                    original_price_mmk = max(original_price_mmk, existing_item.price_mmk)
                elif request.price_mmk >= original_price_mmk:
                    original_price_mmk = request.price_mmk

            item = existing_item.model_copy(
                update={
                    "category": category,
                    "name": title,
                    "description": description,
                    "price_mmk": request.price_mmk,
                    "original_price_mmk": original_price_mmk,
                    "image_url": image_url,
                }
            )
            connection.execute(
                """
                UPDATE food_menu_items
                SET name = ?, description = ?, price_mmk = ?, image_url = ?, payload = ?
                WHERE id = ?
                """,
                (
                    item.name,
                    item.description,
                    item.price_mmk,
                    item.image_url,
                    json.dumps(item.model_dump(mode="json"), ensure_ascii=False),
                    item.id,
                ),
            )
            store_payload = json.loads(row["store_payload"] or "{}")
            store_name = store_payload.get("store_name") or "Restaurant"
            was_discounted = (
                existing_item.original_price_mmk is not None
                and existing_item.price_mmk < existing_item.original_price_mmk
            )
            is_discounted = item.original_price_mmk is not None and item.price_mmk < item.original_price_mmk
        if notify_restaurant_update and existing_item.status == "confirmed" and is_discounted and not was_discounted:
            notify_restaurant_update(
                item.id,
                item.restaurant_id,
                "Restaurant update",
                f"{store_name} has a discount: {item.name}.",
            )
        if sign_url and item.image_url:
            item = item.model_copy(update={"image_url": sign_url(item.image_url)})
        return item

    @router.get("/stores/menu-items", response_model=list[FoodMenuItemResponse])
    def list_my_store_menu_items(
        restaurant_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> list[FoodMenuItemResponse]:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            selected_restaurant_id = (restaurant_id or "").strip()
            if selected_restaurant_id:
                application_row = connection.execute(
                    """
                    SELECT id
                    FROM food_store_applications
                    WHERE id = ? AND user_phone = ? AND status = 'confirmed'
                    LIMIT 1
                    """,
                    (selected_restaurant_id, user_phone),
                ).fetchone()
            else:
                application_row = connection.execute(
                    """
                    SELECT id
                    FROM food_store_applications
                    WHERE user_phone = ? AND status = 'confirmed'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_phone,),
                ).fetchone()
            if not application_row:
                return []
            rows = connection.execute(
                """
                SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                       status, rejection_reason, reviewed_at, created_at, payload
                FROM food_menu_items
                WHERE restaurant_id = ? AND status = 'confirmed'
                ORDER BY created_at DESC
                """,
                (application_row["id"],),
            ).fetchall()
        return [_menu_item_from_row(row, sign_url) for row in rows]

    @router.patch("/stores/menu-items/{menu_item_id}/availability", response_model=FoodMenuItemResponse)
    def update_store_menu_item_availability(
        menu_item_id: str,
        request: UpdateFoodMenuAvailabilityRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodMenuItemResponse:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                       status, rejection_reason, reviewed_at, created_at, item.payload
                FROM food_menu_items item
                JOIN food_store_applications store ON store.id = item.restaurant_id
                WHERE item.id = ? AND item.status = 'confirmed'
                  AND store.user_phone = ? AND store.status = 'confirmed'
                """,
                (menu_item_id, user_phone),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="菜品不存在")

            item = _menu_item_from_row(row).model_copy(update={"is_available": request.is_available})
            connection.execute(
                """
                UPDATE food_menu_items
                SET is_available = ?, payload = ?
                WHERE id = ?
                """,
                (
                    1 if item.is_available else 0,
                    json.dumps(item.model_dump(mode="json"), ensure_ascii=False),
                    item.id,
                ),
            )
        if sign_url and item.image_url:
            item = item.model_copy(update={"image_url": sign_url(item.image_url)})
        return item

    @router.post("/menu-items/{menu_item_id}/click", response_model=FoodMenuItemResponse)
    def record_menu_item_click(
        menu_item_id: str,
        request: FoodMenuItemClickRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodMenuItemResponse:
        require_account_phone(authorization)
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                       status, rejection_reason, reviewed_at, created_at, payload
                FROM food_menu_items
                WHERE id = ? AND status = 'confirmed' AND is_available = 1
                """,
                (menu_item_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="菜品不存在")
            existing_item = _menu_item_from_row(row)
            item = existing_item.model_copy(
                update={"click_count": existing_item.click_count + request.amount}
            )
            connection.execute(
                """
                UPDATE food_menu_items
                SET payload = ?
                WHERE id = ?
                """,
                (
                    json.dumps(item.model_dump(mode="json"), ensure_ascii=False),
                    item.id,
                ),
            )
        if sign_url and item.image_url:
            item = item.model_copy(update={"image_url": sign_url(item.image_url)})
        return item

    @router.get("/orders", response_model=list[FoodOrderResponse])
    def list_food_orders(authorization: str | None = Header(default=None)) -> list[FoodOrderResponse]:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT user_phone, payload
                FROM food_orders
                WHERE user_phone = ?
                ORDER BY created_at DESC
                """,
                (user_phone,),
            ).fetchall()
            return [enrich_food_order_items(connection, food_order_from_row(row)) for row in rows]

    @router.get("/stores/orders", response_model=list[FoodOrderResponse])
    def list_store_food_orders(
        restaurant_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> list[FoodOrderResponse]:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            selected_restaurant_id = (restaurant_id or "").strip()
            if selected_restaurant_id:
                application_rows = connection.execute(
                    """
                    SELECT id
                    FROM food_store_applications
                    WHERE id = ? AND user_phone = ? AND status = 'confirmed'
                    """,
                    (selected_restaurant_id, user_phone),
                ).fetchall()
            else:
                application_rows = connection.execute(
                    """
                    SELECT id
                    FROM food_store_applications
                    WHERE user_phone = ? AND status = 'confirmed'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_phone,),
                ).fetchall()
            restaurant_ids = [row["id"] for row in application_rows]
            if not restaurant_ids:
                return []
            placeholders = ",".join("?" for _ in restaurant_ids)
            rows = connection.execute(
                f"""
                SELECT payload
                FROM food_orders
                WHERE restaurant_id IN ({placeholders})
                  AND payment_status != 'pending'
                  AND status != 'payment_pending'
                ORDER BY created_at DESC
                """,
                tuple(restaurant_ids),
            ).fetchall()
            return [enrich_food_order_items(connection, food_order_from_row(row)) for row in rows]

    @router.get("/rider/orders", response_model=list[FoodOrderResponse])
    def list_rider_food_orders(
        city: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> list[FoodOrderResponse]:
        rider_phone = require_account_phone(authorization)
        requested_city = (city or "").strip()
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM food_orders
                WHERE (status = 'pending' AND payment_status != 'pending') OR rider_phone = ?
                ORDER BY created_at DESC
                """,
                (rider_phone,),
            ).fetchall()
            orders: list[FoodOrderResponse] = []
            for row in rows:
                try:
                    orders.append(enrich_food_order_items(connection, food_order_from_row(row)))
                except (json.JSONDecodeError, TypeError, ValidationError):
                    logger.exception("Skipping invalid rider food order row for rider %s", rider_phone)
            if not requested_city:
                return orders
            return [
                order
                for order in orders
                if order.rider_phone == rider_phone or food_order_matches_rider_city(order, requested_city)
            ]

    @router.post("/rider/orders/{order_id}/accept", response_model=FoodOrderResponse)
    def accept_food_order(
        order_id: str,
        request: FoodAcceptOrderRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodOrderResponse:
        rider_phone = require_account_phone(authorization)
        rider_name = request.rider_name
        if require_approved_rider:
            rider_registration = require_approved_rider(rider_phone)
            rider_name = getattr(rider_registration, "rider_name", "").strip() or rider_name
        with connect_db() as connection:
            row = connection.execute(
                "SELECT payload FROM food_orders WHERE id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="外卖订单不存在")
            order = food_order_from_row(row)
            if order.status != "pending":
                raise HTTPException(status_code=400, detail="外卖订单已经被接单")
            order = order.model_copy(
                update={
                    "status": "accepted",
                    "rider_name": rider_name,
                    "rider_phone": rider_phone,
                    "rider_deposit_status": "not_required",
                    "rider_deposit_due_at": None,
                    "rider_deposit_submitted_at": None,
                    "rider_deposit_proof_url": None,
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            order = enrich_food_order_items(connection, order)
            save_food_order(connection, order)
        notify_food_order_user(order, "accepted", "Rider accepted", f"{rider_name} accepted your food order.")
        return order

    @router.post("/rider/orders/{order_id}/deposit", response_model=FoodOrderResponse)
    def mark_food_rider_deposit_transferred(
        order_id: str,
        request: FoodRiderDepositTransferRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodOrderResponse:
        rider_phone = require_account_phone(authorization)
        with connect_db() as connection:
            row = connection.execute(
                "SELECT payload FROM food_orders WHERE id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="外卖订单不存在")
            order = food_order_from_row(row)
            if order.rider_phone != rider_phone:
                raise HTTPException(status_code=403, detail="不能更新其他骑手的押金状态")
            if order.rider_deposit_status == "confirmed":
                return enrich_food_order_items(connection, order)
            payment_proof_url = (request.payment_proof_url or "").strip()
            if not payment_proof_url:
                raise HTTPException(status_code=400, detail="请上传转账截图")
            order = order.model_copy(
                update={
                    "rider_deposit_status": "pending",
                    "rider_deposit_due_at": None,
                    "rider_deposit_submitted_at": datetime.now(timezone.utc).isoformat(),
                    "rider_deposit_proof_url": payment_proof_url,
                }
            )
            order = enrich_food_order_items(connection, order)
            save_food_order(connection, order)
            return order

    @router.post("/rider/orders/{order_id}/status", response_model=FoodOrderResponse)
    def update_food_order_status(
        order_id: str,
        request: FoodUpdateOrderStatusRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodOrderResponse:
        rider_phone = require_account_phone(authorization)
        allowed_statuses = {"picking_up", "delivering", "completed"}
        if request.status not in allowed_statuses:
            raise HTTPException(status_code=400, detail="外卖订单状态不正确")
        with connect_db() as connection:
            row = connection.execute(
                "SELECT payload FROM food_orders WHERE id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="外卖订单不存在")
            order = food_order_from_row(row)
            if order.rider_phone != rider_phone:
                raise HTTPException(status_code=403, detail="只能更新自己的外卖订单")
            if order.rider_deposit_status not in {"not_required", "confirmed"}:
                raise HTTPException(status_code=403, detail="平台确认骑手押金后才能开始取件配送")
            now = datetime.now(timezone.utc).isoformat()
            updates = {"status": request.status}
            if request.status == "picking_up":
                updates["pickup_started_at"] = now
            elif request.status == "delivering":
                updates["delivery_started_at"] = now
            elif request.status == "completed":
                updates["completed_at"] = now
            order = order.model_copy(update=updates)
            order = enrich_food_order_items(connection, order)
            save_food_order(connection, order)
        title_by_status = {
            "picking_up": "Rider is picking up food",
            "delivering": "Rider is delivering food",
            "completed": "Food delivered",
        }
        message_by_status = {
            "picking_up": "Your rider is heading to the restaurant.",
            "delivering": "Your rider picked up the food and is on the way.",
            "completed": "Your food order has been completed.",
        }
        notify_food_order_user(order, request.status, title_by_status[request.status], message_by_status[request.status])
        return order

    @router.post("/rider/orders/{order_id}/location", response_model=FoodOrderResponse)
    def update_food_rider_location(
        order_id: str,
        request: FoodUpdateRiderLocationRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodOrderResponse:
        rider_phone = require_account_phone(authorization)
        with connect_db() as connection:
            row = connection.execute(
                "SELECT payload FROM food_orders WHERE id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="外卖订单不存在")
            order = food_order_from_row(row)
            if order.rider_phone != rider_phone:
                raise HTTPException(status_code=403, detail="只能更新自己的外卖订单")
            first_location = order.rider_location_updated_at is None
            order = order.model_copy(
                update={
                    "rider_lat": request.lat,
                    "rider_lng": request.lng,
                    "rider_location_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            order = enrich_food_order_items(connection, order)
            save_food_order(connection, order)
        if first_location:
            notify_food_order_user(order, "rider-location", "Rider location available", "You can now track your food rider on the map.")
        return order

    @router.get("/stores/my-application", response_model=FoodStoreApplicationResponse | None)
    def my_store_application(authorization: str | None = Header(default=None)) -> FoodStoreApplicationResponse | None:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT payload, status, rejection_reason, reviewed_at
                FROM food_store_applications
                WHERE user_phone = ?
                  AND json_extract(payload, '$.deleted_at') IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_phone,),
            ).fetchone()
        return _signed_application(_application_from_row(row), sign_url) if row else None

    @router.get("/stores/my-applications", response_model=list[FoodStoreApplicationResponse])
    def my_store_applications(authorization: str | None = Header(default=None)) -> list[FoodStoreApplicationResponse]:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT payload, status, rejection_reason, reviewed_at
                FROM food_store_applications
                WHERE user_phone = ?
                  AND json_extract(payload, '$.deleted_at') IS NULL
                ORDER BY created_at DESC
                """,
                (user_phone,),
            ).fetchall()
        return [_signed_application(_application_from_row(row), sign_url) for row in rows]

    def _update_my_store_application(
        connection: sqlite3.Connection,
        request: FoodStoreApplicationRequest,
        *,
        user_phone: str,
        application_id: str | None,
    ) -> FoodStoreApplicationResponse:
        if application_id:
            row = connection.execute(
                """
                SELECT payload, status, rejection_reason, reviewed_at
                FROM food_store_applications
                WHERE id = ? AND user_phone = ?
                  AND json_extract(payload, '$.deleted_at') IS NULL
                LIMIT 1
                """,
                (application_id, user_phone),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT payload, status, rejection_reason, reviewed_at
                FROM food_store_applications
                WHERE user_phone = ?
                  AND json_extract(payload, '$.deleted_at') IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_phone,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="没有店铺资料")
        existing = _application_from_row(row)
        if existing.status not in {"confirmed", "rejected"}:
            raise HTTPException(status_code=400, detail="店铺资料审核中，暂时不能修改")
        application = _build_store_application(
            request,
            application_id=existing.id,
            user_phone=user_phone,
            status=existing.status,
            rejection_reason=existing.rejection_reason,
            reviewed_at=existing.reviewed_at,
            created_at=existing.created_at,
        )
        application_payload = json.dumps(application.model_dump(mode="json"), ensure_ascii=False)
        connection.execute(
            """
            UPDATE food_store_applications
            SET store_name = ?, owner_name = ?, primary_phone = ?,
                secondary_phone = ?, payload = ?
            WHERE id = ? AND user_phone = ?
            """,
            (
                application.store_name,
                application.owner_name,
                application.primary_phone,
                application.secondary_phone,
                application_payload,
                application.id,
                user_phone,
            ),
        )
        if application.status == "confirmed":
            service_types = application.service_types or []
            restaurant_types = application.restaurant_types or []
            image_url = str(application.signature_dish_image_url or "")
            if not image_url and application.photo_urls:
                image_url = str(application.photo_urls[0])
            connection.execute(
                """
                UPDATE food_restaurants
                SET name = ?, description = ?, image_url = ?, payload = ?
                WHERE id = ?
                """,
                (
                    application.store_name,
                    " / ".join([*service_types, *restaurant_types]),
                    image_url,
                    application_payload,
                    application.id,
                ),
            )
        return application

    @router.put("/stores/my-applications/{application_id}", response_model=FoodStoreApplicationResponse)
    def update_my_store_application_by_id(
        application_id: str,
        request: FoodStoreApplicationRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodStoreApplicationResponse:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            application = _update_my_store_application(
                connection,
                request,
                user_phone=user_phone,
                application_id=application_id,
            )
        return _signed_application(application, sign_url)

    @router.put("/stores/my-application", response_model=FoodStoreApplicationResponse)
    def update_my_store_application(
        request: FoodStoreApplicationRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodStoreApplicationResponse:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            application = _update_my_store_application(
                connection,
                request,
                user_phone=user_phone,
                application_id=None,
            )
        return _signed_application(application, sign_url)

    def _update_my_store_application_payload(
        connection: sqlite3.Connection,
        *,
        application_id: str,
        user_phone: str,
        updates: dict[str, object],
    ) -> FoodStoreApplicationResponse:
        row = connection.execute(
            """
            SELECT payload, status, rejection_reason, reviewed_at
            FROM food_store_applications
            WHERE id = ? AND user_phone = ?
            LIMIT 1
            """,
            (application_id, user_phone),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="没有店铺资料")
        application = _application_from_row(row)
        if application.status != "confirmed":
            raise HTTPException(status_code=400, detail="店铺审核确认后才能操作")
        payload = application.model_dump(mode="json")
        payload.update(updates)
        updated = FoodStoreApplicationResponse(**payload)
        application_payload = json.dumps(updated.model_dump(mode="json"), ensure_ascii=False)
        connection.execute(
            """
            UPDATE food_store_applications
            SET payload = ?
            WHERE id = ? AND user_phone = ?
            """,
            (application_payload, application_id, user_phone),
        )
        connection.execute(
            """
            UPDATE food_restaurants
            SET is_open = ?, payload = ?
            WHERE id = ?
            """,
            (
                0 if updated.is_temporarily_closed or updated.deleted_at else 1,
                application_payload,
                application_id,
            ),
        )
        return updated

    @router.patch("/stores/my-applications/{application_id}/temporary-closure", response_model=FoodStoreApplicationResponse)
    def update_my_store_temporary_closure(
        application_id: str,
        request: UpdateFoodStoreTemporaryClosureRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodStoreApplicationResponse:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            application = _update_my_store_application_payload(
                connection,
                application_id=application_id,
                user_phone=user_phone,
                updates={"is_temporarily_closed": request.is_closed},
            )
        return _signed_application(application, sign_url)

    @router.delete("/stores/my-applications/{application_id}", response_model=FoodStoreApplicationResponse)
    def delete_my_store_application(
        application_id: str,
        authorization: str | None = Header(default=None),
    ) -> FoodStoreApplicationResponse:
        user_phone = require_account_phone(authorization)
        deleted_at = datetime.now(timezone.utc).isoformat()
        with connect_db() as connection:
            application = _update_my_store_application_payload(
                connection,
                application_id=application_id,
                user_phone=user_phone,
                updates={"deleted_at": deleted_at, "is_temporarily_closed": True},
            )
        return _signed_application(application, sign_url)

    @router.post("/orders", response_model=FoodOrderResponse)
    def create_food_order(
        request: CreateFoodOrderRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodOrderResponse:
        user_phone = require_account_phone(authorization)
        if not request.items:
            raise HTTPException(status_code=400, detail="请选择餐品")
        phone_no = request.phone_no.strip()
        secondary_phone_no = request.secondary_phone_no.strip()
        if not _is_valid_food_order_phone(phone_no):
            raise HTTPException(status_code=400, detail="Phone No. must be 9 or 11 digits.")
        if secondary_phone_no and not _is_valid_food_order_phone(secondary_phone_no):
            raise HTTPException(status_code=400, detail="Phone No. must be 9 or 11 digits.")
        delivery_city = request.delivery_city.strip()
        delivery_township = request.delivery_township.strip()
        if request.fulfillment_type != "pickup":
            delivery_city, delivery_township = _validate_delivery_city_township(delivery_city, delivery_township)
            if (
                request.delivery_lat is not None
                and request.delivery_lng is not None
                and not _coordinate_inside_city(request.delivery_lat, request.delivery_lng, delivery_city)
            ):
                raise HTTPException(status_code=400, detail=f"请选择 {delivery_city} 内的 Google Map Location")

        created_at = datetime.now(timezone.utc).isoformat()
        with connect_db() as connection:
            restaurant_row = connection.execute(
                """
                SELECT payload
                FROM food_store_applications
                WHERE id = ? AND status = 'confirmed'
                  AND json_extract(payload, '$.deleted_at') IS NULL
                """,
                (request.restaurant_id,),
            ).fetchone()
            if not restaurant_row:
                raise HTTPException(status_code=404, detail="餐厅不存在")
            restaurant_payload = json.loads(restaurant_row["payload"] or "{}")
            if bool(restaurant_payload.get("is_temporarily_closed")):
                raise HTTPException(status_code=400, detail="店铺已打烊，暂时不能下单")
            open_text = _normalize_business_hour(restaurant_payload.get("business_hours_open") or "09:00", "09:00")
            close_text = _normalize_business_hour(restaurant_payload.get("business_hours_close") or "21:00", "21:00")
            if not _is_restaurant_open(open_text, close_text):
                raise HTTPException(status_code=400, detail="店铺已打烊，暂时不能下单")

            discount_mmk = 0.0
            voucher_code = request.voucher_code.strip()
            if voucher_code:
                today = datetime.now(ZoneInfo("Asia/Yangon")).date().isoformat()
                coupon_row = connection.execute(
                    """
                    SELECT name, min_cart_mmk, discount_mmk, discount_type, discount_percent
                    FROM coupons
                    WHERE lower(name) = lower(?)
                      AND is_active = 1
                      AND start_date <= ?
                      AND end_date >= ?
                      AND (scope = 'food' OR scope = 'both')
                      AND (target_type = 'all' OR target_user_phone = ?)
                    """,
                    (voucher_code, today, today, user_phone),
                ).fetchone()
                if coupon_row and request.subtotal_mmk >= float(coupon_row["min_cart_mmk"] or 0):
                    if coupon_row["discount_type"] == "percent":
                        discount_mmk = request.subtotal_mmk * float(coupon_row["discount_percent"] or 0) / 100
                    else:
                        discount_mmk = float(coupon_row["discount_mmk"] or 0)
                    discount_mmk = min(discount_mmk, request.subtotal_mmk)

            is_qr_pay = request.payment_method.strip().casefold() == "qr pay"
            payment_proof_url = (request.payment_proof_url or "").strip()
            if is_qr_pay and not payment_proof_url:
                raise HTTPException(status_code=400, detail="请先上传付款截图")
            order = FoodOrderResponse(
                id=str(uuid4()),
                user_phone=user_phone,
                restaurant_id=request.restaurant_id,
                delivery_address=request.delivery_address,
                delivery_city=delivery_city,
                delivery_township=delivery_township,
                delivery_lat=request.delivery_lat,
                delivery_lng=request.delivery_lng,
                fulfillment_type=request.fulfillment_type,
                payment_method=request.payment_method,
                payment_proof_url=payment_proof_url or None,
                payment_status="pending" if is_qr_pay else "not_required",
                phone_no=phone_no,
                secondary_phone_no=secondary_phone_no,
                subtotal_mmk=request.subtotal_mmk,
                delivery_fee_mmk=request.delivery_fee_mmk,
                discount_mmk=discount_mmk,
                goods_amount=request.subtotal_mmk - discount_mmk,
                voucher_code=voucher_code,
                status="payment_pending" if is_qr_pay else "pending",
                items=request.items,
                note=request.note,
                restaurant_name=restaurant_payload.get("store_name") or "",
                restaurant_location=_restaurant_location_text(restaurant_payload),
                restaurant_city=restaurant_payload.get("store_city") or "",
                restaurant_township=restaurant_payload.get("store_township") or "",
                created_at=created_at,
            )
            order = enrich_food_order_items(connection, order)
            connection.execute(
                """
                INSERT INTO food_orders (id, user_phone, restaurant_id, rider_phone, status, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    user_phone,
                    order.restaurant_id,
                    order.rider_phone,
                    order.status,
                    created_at,
                    json.dumps(order.model_dump(mode="json"), ensure_ascii=False),
                ),
            )
            for order_item in request.items:
                row = connection.execute(
                    """
                    SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available,
                           status, rejection_reason, reviewed_at, created_at, payload
                    FROM food_menu_items
                    WHERE id = ? AND restaurant_id = ?
                    """,
                    (order_item.menu_item_id, request.restaurant_id),
                ).fetchone()
                if not row:
                    continue
                existing_menu_item = _menu_item_from_row(row)
                menu_item = existing_menu_item.model_copy(
                    update={"click_count": existing_menu_item.click_count + order_item.quantity}
                )
                connection.execute(
                    """
                    UPDATE food_menu_items
                    SET payload = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(menu_item.model_dump(mode="json"), ensure_ascii=False),
                        menu_item.id,
                    ),
                )
        return order

    @router.post("/stores/register", response_model=FoodStoreApplicationResponse)
    def register_store(
        request: FoodStoreApplicationRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodStoreApplicationResponse:
        user_phone = require_account_phone(authorization)
        created_at = datetime.now(timezone.utc).isoformat()
        application = _build_store_application(
            request,
            application_id=str(uuid4()),
            user_phone=user_phone,
            status="pending",
            created_at=created_at,
        )
        with connect_db() as connection:
            connection.execute(
                """
                INSERT INTO food_store_applications (
                    id, user_phone, store_name, owner_name, primary_phone,
                    secondary_phone, status, created_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application.id,
                    user_phone,
                    application.store_name,
                    application.owner_name,
                    application.primary_phone,
                    application.secondary_phone,
                    application.status,
                    created_at,
                    json.dumps(application.model_dump(mode="json"), ensure_ascii=False),
                ),
            )
        return application

    return router
