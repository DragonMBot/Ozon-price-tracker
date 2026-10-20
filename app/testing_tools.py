from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PricePoint, Product, User, utc_now
from .schemas import AuthInput
from .security import hash_password

TEST_FIXTURE_PREFIX = "[ТЕСТОВЫЕ ДАННЫЕ]"

# Price observations are deliberately inside the 7/30/180-day windows, not on
# their exact boundaries, so results do not depend on query timing to the second.
_OBSERVATION_DAYS_AGO = (179, 120, 90, 60, 29, 14, 6, 1)
_FIXTURE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "slot": 1,
        "label": "Беспроводные наушники (демо)",
        "prices": (15999, 15499, 14999, 13999, 13999, 12999, 12499, 11999),
    },
    {
        "slot": 2,
        "label": "Механическая клавиатура (демо)",
        "prices": (8999, 9299, 8799, 8499, 8999, 8299, 7999, 7799),
    },
    {
        "slot": 3,
        "label": "Умная лампа (демо)",
        "prices": (3499, 3299, 2999, 3199, 2899, 2799, 2599, 2499),
    },
)


def normalize_email(email: str) -> str:
    # This password is only a schema validation placeholder and is never persisted.
    return AuthInput(email=email, password="test-fixture-validation-only").email


def _find_or_create_user(db: Session, email: str) -> tuple[User, bool, str | None]:
    normalized_email = normalize_email(email)
    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is not None:
        return user, False, None

    temporary_password = secrets.token_urlsafe(18)
    user = User(email=normalized_email, password_hash=hash_password(temporary_password))
    db.add(user)
    db.flush()
    return user, True, temporary_password


def _fixture_ozon_id(db: Session, user_id: int, slot: int) -> str:
    candidate_number = 99_000_000_000_000 + user_id * 100 + slot
    while True:
        candidate = str(candidate_number)
        existing = db.scalar(
            select(Product.id).where(Product.user_id == user_id, Product.ozon_id == candidate)
        )
        if existing is None:
            return candidate
        candidate_number += 1


def seed_test_fixtures(db: Session, email: str) -> dict[str, Any]:
    user, user_created, temporary_password = _find_or_create_user(db, email)
    now = utc_now()
    seeded_products: list[dict[str, Any]] = []
    products_created = 0
    history_points_created = 0

    try:
        for spec in _FIXTURE_SPECS:
            fixture_name = f"{TEST_FIXTURE_PREFIX} {spec['label']}"
            product = db.scalar(
                select(Product).where(
                    Product.user_id == user.id,
                    Product.name == fixture_name,
                    Product.url.like("https://www.ozon.ru/product/%"),
                )
            )
            if product is None:
                ozon_id = _fixture_ozon_id(db, user.id, int(spec["slot"]))
                last_observed_at = now - timedelta(days=_OBSERVATION_DAYS_AGO[-1])
                product = Product(
                    user_id=user.id,
                    ozon_id=ozon_id,
                    name=fixture_name,
                    url=f"https://www.ozon.ru/product/{ozon_id}/",
                    current_price=Decimal(str(spec["prices"][-1])),
                    currency="RUB",
                    created_at=now - timedelta(days=_OBSERVATION_DAYS_AGO[0]),
                    last_checked_at=last_observed_at,
                    last_error=None,
                )
                db.add(product)
                db.flush()
                products_created += 1

            existing_points = db.scalar(
                select(PricePoint.id).where(PricePoint.product_id == product.id).limit(1)
            )
            if existing_points is None:
                for days_ago, raw_price in zip(_OBSERVATION_DAYS_AGO, spec["prices"], strict=True):
                    db.add(
                        PricePoint(
                            product_id=product.id,
                            price=Decimal(str(raw_price)),
                            currency="RUB",
                            observed_at=now - timedelta(days=days_ago),
                        )
                    )
                    history_points_created += 1
                # Repair the latest state from the latest deterministic fixture point.
                product.current_price = Decimal(str(spec["prices"][-1]))
                product.currency = "RUB"
                product.last_checked_at = now - timedelta(days=_OBSERVATION_DAYS_AGO[-1])
                product.last_error = None

            # Sessions are configured with autoflush=False, so flush before counting
            # the pending synthetic observations.
            db.flush()
            point_count = int(
                db.query(PricePoint.id).filter(PricePoint.product_id == product.id).count()
            )
            seeded_products.append(
                {
                    "id": int(product.id),
                    "ozon_id": str(product.ozon_id),
                    "name": product.name,
                    "current_price": float(product.current_price),
                    "history_points": point_count,
                    "is_test_fixture": True,
                }
            )

        db.commit()
        return {
            "email": user.email,
            "user_id": int(user.id),
            "user_created": user_created,
            "temporary_password": temporary_password,
            "products_created": products_created,
            "history_points_created": history_points_created,
            "fixtures": seeded_products,
            "message": "Созданы фиктивные данные; реальные запросы к Ozon не выполнялись.",
        }
    except Exception:
        db.rollback()
        raise
