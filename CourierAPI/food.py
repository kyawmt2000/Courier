import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field


class FoodRestaurantResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    image_url: str | None = None
    is_open: bool = True


class FoodMenuItemResponse(BaseModel):
    id: str
    restaurant_id: str
    category: str = ""
    name: str
    description: str = ""
    price_mmk: float
    image_url: str | None = None
    is_available: bool = True


class CreateFoodMenuItemRequest(BaseModel):
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    image_url: str = Field(min_length=1)


class FoodOrderItemRequest(BaseModel):
    menu_item_id: str
    quantity: int = Field(ge=1, le=99)
    note: str = ""


class CreateFoodOrderRequest(BaseModel):
    restaurant_id: str
    delivery_address: str
    delivery_lat: float | None = None
    delivery_lng: float | None = None
    items: list[FoodOrderItemRequest] = Field(default_factory=list)
    note: str = ""


class FoodOrderResponse(BaseModel):
    id: str
    user_phone: str
    restaurant_id: str
    delivery_address: str
    status: str
    items: list[FoodOrderItemRequest]
    note: str = ""
    created_at: str


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
    service_types: list[str] = Field(min_length=1)
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
    service_types: list[str]
    license_urls: list[str] = Field(default_factory=list)
    menu_urls: list[str] = Field(default_factory=list)
    photo_urls: list[str]
    status: str
    rejection_reason: str | None = None
    reviewed_at: str | None = None
    created_at: str


class AdminUpdateFoodStoreApplicationRequest(BaseModel):
    status: str
    rejection_reason: str | None = None


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


def _application_from_row(row: sqlite3.Row) -> FoodStoreApplicationResponse:
    payload = json.loads(row["payload"])
    payload.setdefault("owner_nrc_front_url", "")
    payload.setdefault("owner_nrc_back_url", "")
    payload.setdefault("payment_qr_url", None)
    payload.setdefault("bank_account", "")
    payload.setdefault("bank_account_name", "")
    payload.setdefault("bank_account_number", "")
    payload.setdefault("store_address", "")
    payload.setdefault("license_urls", [])
    payload.setdefault("menu_urls", [])
    payload["status"] = row["status"]
    payload["rejection_reason"] = row["rejection_reason"]
    payload["reviewed_at"] = row["reviewed_at"]
    return FoodStoreApplicationResponse(**payload)


def load_admin_store_applications(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT payload, status, rejection_reason, reviewed_at
            FROM food_store_applications
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [_application_from_row(row).model_dump(mode="json") for row in rows]


def update_admin_store_application(
    db_path: Path,
    application_id: str,
    request: AdminUpdateFoodStoreApplicationRequest,
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
                    " / ".join([*application.service_types, application.store_address]),
                    application.photo_urls[0] if application.photo_urls else None,
                    json.dumps(application.model_dump(mode="json"), ensure_ascii=False),
                    application.created_at,
                ),
            )
    return application


def create_food_router(
    db_path: Path,
    require_account_phone: Callable[[str | None], str],
) -> APIRouter:
    router = APIRouter(prefix="/food", tags=["food"])

    def connect_db() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @router.get("/restaurants", response_model=list[FoodRestaurantResponse])
    def list_restaurants() -> list[FoodRestaurantResponse]:
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT id, name, description, image_url, is_open
                FROM food_restaurants
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()
        return [
            FoodRestaurantResponse(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                image_url=row["image_url"],
                is_open=bool(row["is_open"]),
            )
            for row in rows
        ]

    @router.get("/restaurants/{restaurant_id}/menu", response_model=list[FoodMenuItemResponse])
    def list_menu_items(restaurant_id: str) -> list[FoodMenuItemResponse]:
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT id, restaurant_id, name, description, price_mmk, image_url, is_available
                FROM food_menu_items
                WHERE restaurant_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (restaurant_id,),
            ).fetchall()
        return [
            FoodMenuItemResponse(
                id=row["id"],
                restaurant_id=row["restaurant_id"],
                category=json.loads(row["payload"]).get("category", ""),
                name=row["name"],
                description=row["description"],
                price_mmk=row["price_mmk"],
                image_url=row["image_url"],
                is_available=bool(row["is_available"]),
            )
            for row in rows
        ]

    @router.post("/stores/menu-items", response_model=FoodMenuItemResponse)
    def create_store_menu_item(
        request: CreateFoodMenuItemRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodMenuItemResponse:
        user_phone = require_account_phone(authorization)
        category = request.category.strip()
        title = request.title.strip()
        description = request.description.strip()
        image_url = request.image_url.strip()
        if not category:
            raise HTTPException(status_code=400, detail="请填写菜品类型")
        if not title:
            raise HTTPException(status_code=400, detail="请填写菜品标题")
        if not image_url:
            raise HTTPException(status_code=400, detail="请上传菜品图片")

        created_at = datetime.now(timezone.utc).isoformat()
        with connect_db() as connection:
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
                raise HTTPException(status_code=403, detail="店铺审核确认后才能上传菜品")

            restaurant_id = application_row["id"]
            menu_item = FoodMenuItemResponse(
                id=str(uuid4()),
                restaurant_id=restaurant_id,
                category=category,
                name=title,
                description=description,
                price_mmk=0,
                image_url=image_url,
                is_available=True,
            )
            connection.execute(
                """
                INSERT INTO food_menu_items (
                    id, restaurant_id, name, description, price_mmk,
                    image_url, is_available, payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    menu_item.id,
                    restaurant_id,
                    menu_item.name,
                    menu_item.description,
                    menu_item.price_mmk,
                    menu_item.image_url,
                    json.dumps(menu_item.model_dump(mode="json"), ensure_ascii=False),
                    created_at,
                ),
            )
        return menu_item

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
        return [FoodOrderResponse(**json.loads(row["payload"])) for row in rows]

    @router.get("/stores/my-application", response_model=FoodStoreApplicationResponse | None)
    def my_store_application(authorization: str | None = Header(default=None)) -> FoodStoreApplicationResponse | None:
        user_phone = require_account_phone(authorization)
        with connect_db() as connection:
            row = connection.execute(
                """
                SELECT payload, status, rejection_reason, reviewed_at
                FROM food_store_applications
                WHERE user_phone = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_phone,),
            ).fetchone()
        return _application_from_row(row) if row else None

    @router.post("/orders", response_model=FoodOrderResponse)
    def create_food_order(
        request: CreateFoodOrderRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodOrderResponse:
        user_phone = require_account_phone(authorization)
        if not request.items:
            raise HTTPException(status_code=400, detail="请选择餐品")

        created_at = datetime.now(timezone.utc).isoformat()
        order = FoodOrderResponse(
            id=str(uuid4()),
            user_phone=user_phone,
            restaurant_id=request.restaurant_id,
            delivery_address=request.delivery_address,
            status="pending",
            items=request.items,
            note=request.note,
            created_at=created_at,
        )
        with connect_db() as connection:
            connection.execute(
                """
                INSERT INTO food_orders (id, user_phone, restaurant_id, status, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    user_phone,
                    order.restaurant_id,
                    order.status,
                    created_at,
                    json.dumps(order.model_dump(mode="json"), ensure_ascii=False),
                ),
            )
        return order

    @router.post("/stores/register", response_model=FoodStoreApplicationResponse)
    def register_store(
        request: FoodStoreApplicationRequest,
        authorization: str | None = Header(default=None),
    ) -> FoodStoreApplicationResponse:
        user_phone = require_account_phone(authorization)
        service_types = [
            value.strip()
            for value in request.service_types
            if value.strip() in {"外卖", "堂食"}
        ]
        if not service_types:
            raise HTTPException(status_code=400, detail="请选择经营方式")
        bank_account_name = request.bank_account_name.strip()
        bank_account_number = request.bank_account_number.strip()
        bank_account = request.bank_account.strip() or " / ".join(
            value for value in [bank_account_name, bank_account_number] if value
        )
        payment_qr_url = request.payment_qr_url.strip() if request.payment_qr_url else None
        if (bank_account_name and not bank_account_number) or (bank_account_number and not bank_account_name):
            raise HTTPException(status_code=400, detail="请填写银行名字和账号")
        if not payment_qr_url and not bank_account:
            raise HTTPException(status_code=400, detail="请上传收款码或填写银行账号")
        if len(request.license_urls) > 3:
            raise HTTPException(status_code=400, detail="营业执照最多上传 3 张")
        if len(request.menu_urls) > 10:
            raise HTTPException(status_code=400, detail="菜单最多上传 10 张")

        created_at = datetime.now(timezone.utc).isoformat()
        application = FoodStoreApplicationResponse(
            id=str(uuid4()),
            user_phone=user_phone,
            store_name=request.store_name.strip(),
            owner_name=request.owner_name.strip(),
            owner_nrc_front_url=request.owner_nrc_front_url.strip(),
            owner_nrc_back_url=request.owner_nrc_back_url.strip(),
            primary_phone=request.primary_phone.strip(),
            secondary_phone=request.secondary_phone.strip(),
            payment_qr_url=payment_qr_url,
            bank_account=bank_account,
            bank_account_name=bank_account_name,
            bank_account_number=bank_account_number,
            store_address=request.store_address.strip(),
            service_types=service_types,
            license_urls=request.license_urls,
            menu_urls=request.menu_urls,
            photo_urls=request.photo_urls,
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
