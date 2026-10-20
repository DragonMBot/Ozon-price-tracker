from __future__ import annotations

import asyncio
import logging
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .config import HOST, PORT, REFRESH_INTERVAL_SECONDS
from .db import Base, SessionLocal, engine, get_db
from .logging_config import configure_logging
from .models import PricePoint, Product, User, utc_now
from .ozon import OzonFetchError, OzonProductData, fetch_product
from .schemas import AuthInput, ClientLogInput, ProductResolveInput, TestFixtureSeedInput
from .security import create_access_token, hash_password, load_or_create_secret, verify_access_token, verify_password
from .testing_tools import TEST_FIXTURE_PREFIX, seed_test_fixtures

configure_logging()
logger = logging.getLogger("ozon_tracker.api")
APP_SECRET = load_or_create_secret()
http_bearer = HTTPBearer(auto_error=False)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _user_payload(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "created_at": _ensure_utc(user.created_at).isoformat()}


def _http_status_for_ozon_error(exc: OzonFetchError) -> int:
    if exc.code in {"invalid_product_id", "parse_failed", "html_snapshot_too_large"}:
        return 422
    if exc.code == "not_found":
        return status.HTTP_404_NOT_FOUND
    if exc.code == "rate_limited":
        return status.HTTP_429_TOO_MANY_REQUESTS
    if exc.code in {"public_page_access_denied", "challenge_detected"}:
        # The local API could not complete the operation because the upstream provider denied it.
        return 424  # Failed Dependency; not the user's local account authorization.
    if exc.code in {"navigation_timeout", "playwright_timeout", "product_fetch_deadline_exceeded"}:
        return status.HTTP_504_GATEWAY_TIMEOUT
    if exc.code in {"playwright_not_installed", "playwright_browser_missing", "html_snapshot_write_failed"}:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    if exc.code in {"upstream_unavailable", "upstream_error"}:
        return status.HTTP_502_BAD_GATEWAY
    if exc.http_status in {502, 503, 504}:
        return status.HTTP_502_BAD_GATEWAY
    # Unknown browser/network failures are still gateway errors; their exact cause is in detail.
    return status.HTTP_502_BAD_GATEWAY


def _product_payload(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "ozon_id": product.ozon_id,
        "name": product.name,
        "url": product.url,
        "current_price": float(product.current_price),
        "currency": product.currency,
        "created_at": _ensure_utc(product.created_at).isoformat(),
        "last_checked_at": _ensure_utc(product.last_checked_at).isoformat(),
        "last_error": product.last_error,
    }


def _require_owned_product(db: Session, user_id: int, product_id: int) -> Product:
    product = db.scalar(
        select(Product).where(Product.id == product_id, Product.user_id == user_id)
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден.")
    return product


def _add_price_point(db: Session, product: Product, price: Decimal, currency: str) -> None:
    db.add(PricePoint(product_id=product.id, price=price, currency=currency, observed_at=utc_now()))
    product.current_price = price
    product.currency = currency
    product.last_checked_at = utc_now()
    product.last_error = None


async def _refresh_product(product_id: int, user_id: int, ozon_id: str) -> tuple[bool, OzonFetchError | str | None]:
    try:
        data = await fetch_product(ozon_id)
    except OzonFetchError as exc:
        with SessionLocal() as db:
            product = db.scalar(select(Product).where(Product.id == product_id, Product.user_id == user_id))
            if product is not None:
                product.last_checked_at = utc_now()
                product.last_error = str(exc)[:500]
                db.commit()
        safe_diagnostics = json.dumps(exc.diagnostics, ensure_ascii=False, sort_keys=True)[:2000]
        logger.warning(
            "Price refresh failed: product_id=%s ozon_id=%s provider=%s code=%s http_status=%s retryable=%s reason=%s diagnostics=%s",
            product_id, ozon_id, exc.provider, exc.code, exc.http_status, exc.retryable, str(exc), safe_diagnostics,
        )
        return False, exc

    with SessionLocal() as db:
        product = db.scalar(select(Product).where(Product.id == product_id, Product.user_id == user_id))
        if product is None:
            logger.info("Skipping refreshed product that was deleted during network request: id=%s", product_id)
            return False, "Товар уже удалён."
        product.name = data.name
        product.url = data.url
        _add_price_point(db, product, data.price, data.currency)
        db.commit()
    logger.info("Price refreshed: product_id=%s ozon_id=%s price=%s", product_id, ozon_id, data.price)
    return True, None


async def refresh_all_products() -> None:
    with SessionLocal() as db:
        items = db.execute(
            select(Product.id, Product.user_id, Product.ozon_id)
            .where(~Product.name.startswith(TEST_FIXTURE_PREFIX))
            .order_by(Product.id)
        ).all()
        snapshot = [(int(row.id), int(row.user_id), str(row.ozon_id)) for row in items]
    logger.info("Starting scheduled refresh for %d tracked product(s)", len(snapshot))
    success_count = 0
    for product_id, user_id, ozon_id in snapshot:
        ok, _error = await _refresh_product(product_id, user_id, ozon_id)
        success_count += int(ok)
        # A short delay between product pages lowers request burstiness.
        await asyncio.sleep(1.0)
    logger.info("Scheduled refresh finished: successful=%d total=%d", success_count, len(snapshot))


async def _refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await refresh_all_products()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error in scheduled refresh loop")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("Backend started on http://%s:%d; SQLite database is ready", HOST, PORT)
    logger.info("Product data source: public Ozon page via standard Playwright; HTML snapshot: parser_output/page.html")
    logger.info("Scheduled price refresh interval: %d hours", REFRESH_INTERVAL_SECONDS // 3600)
    task = asyncio.create_task(_refresh_loop(), name="ozon-price-refresh-loop")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Backend shutdown completed")


app = FastAPI(
    title="Ozon Price Tracker Local API",
    version="1.0.0",
    description="Локальный API расширения Firefox для хранения товаров и истории цен.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(moz-extension://[a-z0-9-]+|http://127\.0\.0\.1(?::\d+)?|http://localhost(?::\d+)?)$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Необходим вход в аккаунт.", headers={"WWW-Authenticate": "Bearer"})
    user_id = verify_access_token(credentials.credentials, APP_SECRET)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Сессия истекла. Войдите снова.", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Аккаунт не найден. Войдите снова.", headers={"WWW-Authenticate": "Bearer"})
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ozon-price-tracker",
        "refresh_interval_hours": 12,
        "data_source": "playwright",
        "html_snapshot": "parser_output/page.html",
    }


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: AuthInput, db: DbSession) -> dict[str, Any]:
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Аккаунт с таким email уже существует.")
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        logger.info("Registration conflict handled after unique constraint");
        raise HTTPException(status_code=409, detail="Аккаунт с таким email уже существует.") from exc
    token = create_access_token(user.id, APP_SECRET)
    logger.info("Account registered: user_id=%s", user.id)
    return {"access_token": token, "token_type": "bearer", "user": _user_payload(user)}


@app.post("/api/auth/login")
def login(payload: AuthInput, db: DbSession) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        logger.warning("Authentication failed for submitted email")
        raise HTTPException(status_code=401, detail="Неверный email или пароль.")
    token = create_access_token(user.id, APP_SECRET)
    logger.info("Account login successful: user_id=%s", user.id)
    return {"access_token": token, "token_type": "bearer", "user": _user_payload(user)}


@app.get("/api/me")
def me(user: CurrentUser) -> dict[str, Any]:
    return _user_payload(user)


@app.get("/api/products")
def list_products(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    products = db.scalars(
        select(Product).where(Product.user_id == user.id).order_by(Product.created_at.desc(), Product.id.desc())
    ).all()
    return {"items": [_product_payload(item) for item in products], "count": len(products)}


@app.post("/api/products", status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductResolveInput, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    existing = db.scalar(select(Product).where(Product.user_id == user.id, Product.ozon_id == payload.ozon_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Этот товар уже есть в вашем списке.")
    # End the read transaction before waiting for an external HTTP request.
    db.rollback()
    try:
        data: OzonProductData = await fetch_product(payload.ozon_id)
    except OzonFetchError as exc:
        logger.warning(
            "Product resolution failed: ozon_id=%s provider=%s code=%s http_status=%s diagnostics=%s",
            payload.ozon_id, exc.provider, exc.code, exc.http_status,
            json.dumps(exc.diagnostics, ensure_ascii=False, sort_keys=True)[:2000],
        )
        raise HTTPException(status_code=_http_status_for_ozon_error(exc), detail=exc.as_public_dict()) from exc

    product = Product(
        user_id=user.id,
        ozon_id=data.ozon_id,
        name=data.name,
        url=data.url,
        current_price=data.price,
        currency=data.currency,
        created_at=utc_now(),
        last_checked_at=utc_now(),
    )
    db.add(product)
    try:
        db.flush()
        db.add(PricePoint(product_id=product.id, price=data.price, currency=data.currency, observed_at=utc_now()))
        db.commit()
        db.refresh(product)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Этот товар уже был добавлен.") from exc
    logger.info("Product added: user_id=%s product_id=%s ozon_id=%s", user.id, product.id, product.ozon_id)
    return {"item": _product_payload(product), "message": "Товар найден и добавлен."}


@app.delete("/api/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: int, user: CurrentUser, db: DbSession) -> None:
    product = _require_owned_product(db, user.id, product_id)
    db.delete(product)
    db.commit()
    logger.info("Product deleted: user_id=%s product_id=%s", user.id, product_id)


@app.get("/api/products/{product_id}/history")
def product_history(
    product_id: int,
    user: CurrentUser,
    db: DbSession,
    days: int = Query(default=30, ge=1, le=180),
) -> dict[str, Any]:
    product = _require_owned_product(db, user.id, product_id)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    points = db.scalars(
        select(PricePoint)
        .where(PricePoint.product_id == product.id, PricePoint.observed_at >= since)
        .order_by(PricePoint.observed_at.asc(), PricePoint.id.asc())
    ).all()
    return {
        "product": {"id": product.id, "ozon_id": product.ozon_id, "name": product.name, "currency": product.currency},
        "days": days,
        "items": [
            {"price": float(point.price), "currency": point.currency, "observed_at": _ensure_utc(point.observed_at).isoformat()}
            for point in points
        ],
    }


@app.post("/api/products/{product_id}/refresh")
async def refresh_product(product_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    product = _require_owned_product(db, user.id, product_id)
    if product.name.startswith(TEST_FIXTURE_PREFIX):
        raise HTTPException(status_code=409, detail="Это демонстрационный товар: фиктивные цены не обновляются с Ozon.")
    product_id_copy, user_id, ozon_id = product.id, user.id, product.ozon_id
    db.rollback()
    success, error = await _refresh_product(product_id_copy, user_id, ozon_id)
    with SessionLocal() as fresh_db:
        fresh_product = fresh_db.scalar(select(Product).where(Product.id == product_id_copy, Product.user_id == user_id))
        if fresh_product is None:
            raise HTTPException(status_code=404, detail="Товар был удалён.")
        if not success:
            detail = error.as_public_dict() if isinstance(error, OzonFetchError) else (error or "Не удалось обновить цену.")
            raise HTTPException(
                status_code=_http_status_for_ozon_error(error) if isinstance(error, OzonFetchError) else status.HTTP_502_BAD_GATEWAY,
                detail=detail,
            )
        return {"item": _product_payload(fresh_product), "message": "Цена обновлена."}


@app.post("/api/test-tools/seed")
def seed_test_fixtures_endpoint(
    payload: TestFixtureSeedInput,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    enabled = os.getenv("OZON_ENABLE_TEST_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        raise HTTPException(404, detail="Инструменты тестирования отключены. Запустите backend с OZON_ENABLE_TEST_TOOLS=1.")
    if payload.email != user.email:
        raise HTTPException(403, detail="Для безопасности email должен совпадать с аккаунтом, в который выполнен вход.")

    result = seed_test_fixtures(db, user.email)
    # A password for a newly created account is only generated by the CLI helper.
    result.pop("temporary_password", None)
    logger.info(
        "Test fixtures seeded: user_id=%s products_created=%s points_created=%s",
        user.id, result["products_created"], result["history_points_created"],
    )
    return result


@app.post("/api/client-log", status_code=status.HTTP_202_ACCEPTED)
def client_log(payload: ClientLogInput) -> dict[str, str]:
    message = re.sub(r"[\r\n\t]+", " ", payload.message)[:500]
    details = re.sub(r"[\r\n\t]+", " ", payload.details or "")[:1000]
    logger.log(getattr(logging, payload.level, logging.INFO), "extension: %s%s", message, f" | {details}" if details else "")
    return {"status": "accepted"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info", reload=False)
