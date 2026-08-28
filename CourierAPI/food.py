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
    name: str
    description: str = ""
    price_mmk: float
    image_url: str | None = None
    is_available: bool = True


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
    primary_phone: str = Field(min_length=6)
    secondary_phone: str = Field(min_length=6)
    service_types: list[str] = Field(min_length=1)
    photo_urls: list[str] = Field(min_length=5, max_length=10)


class FoodStoreApplicationResponse(BaseModel):
    id: str
    user_phone: str
    store_name: str
    owner_name: str
    primary_phone: str
    secondary_phone: str
    service_types: list[str]
    photo_urls: list[str]
    status: str
    created_at: str


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
                name=row["name"],
                description=row["description"],
                price_mmk=row["price_mmk"],
                image_url=row["image_url"],
                is_available=bool(row["is_available"]),
            )
            for row in rows
        ]

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

        created_at = datetime.now(timezone.utc).isoformat()
        application = FoodStoreApplicationResponse(
            id=str(uuid4()),
            user_phone=user_phone,
            store_name=request.store_name.strip(),
            owner_name=request.owner_name.strip(),
            primary_phone=request.primary_phone.strip(),
            secondary_phone=request.secondary_phone.strip(),
            service_types=service_types,
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
