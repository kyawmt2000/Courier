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

    return router
