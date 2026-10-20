from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.ozon import OzonProductData
import app.main as api_main


class LocalApiTests(unittest.TestCase):
    def test_scraper_parse_failure_is_not_misreported_as_502(self) -> None:
        error = api_main.OzonFetchError(
            "Не удалось достоверно определить цену товара.",
            code="parse_failed",
            provider="playwright",
            retryable=False,
        )
        self.assertEqual(api_main._http_status_for_ozon_error(error), 422)

    def test_scraper_upstream_failures_have_distinct_http_statuses(self) -> None:
        self.assertEqual(
            api_main._http_status_for_ozon_error(api_main.OzonFetchError("Источник отказал", code="public_page_access_denied", http_status=403)),
            424,
        )
        self.assertEqual(
            api_main._http_status_for_ozon_error(api_main.OzonFetchError("Тайм-аут", code="navigation_timeout", retryable=True)),
            504,
        )
        self.assertEqual(
            api_main._http_status_for_ozon_error(api_main.OzonFetchError("Источник временно недоступен", code="upstream_unavailable", http_status=503, retryable=True)),
            502,
        )
        self.assertEqual(
            api_main._http_status_for_ozon_error(api_main.OzonFetchError("Общая длительность запроса превысила лимит", code="product_fetch_deadline_exceeded", retryable=True)),
            504,
        )

    def test_auth_add_history_and_owner_isolation(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def override_get_db():
            db = test_session_factory()
            try:
                yield db
            finally:
                db.close()

        api_main.app.dependency_overrides[get_db] = override_get_db
        fake_product = OzonProductData(
            ozon_id="123456789",
            name="Integration test item",
            price=Decimal("1234.50"),
            currency="RUB",
            url="https://www.ozon.ru/product/123456789/",
        )
        try:
            with (
                patch.object(api_main, "engine", engine),
                patch.object(api_main, "APP_SECRET", b"integration-test-secret-48-bytes-long"),
                patch.object(api_main, "fetch_product", return_value=fake_product),
                TestClient(api_main.app) as client,
            ):
                first_register = client.post(
                    "/api/auth/register",
                    json={"email": "first@example.com", "password": "test-password-123"},
                )
                self.assertEqual(first_register.status_code, 201, first_register.text)
                first_token = first_register.json()["access_token"]
                first_headers = {"Authorization": f"Bearer {first_token}"}
                self.assertEqual(client.get("/api/me", headers=first_headers).status_code, 200)

                added = client.post("/api/products", headers=first_headers, json={"ozon_id": "123456789"})
                self.assertEqual(added.status_code, 201, added.text)
                self.assertEqual(added.json()["item"]["current_price"], 1234.5)
                product_id = added.json()["item"]["id"]

                history = client.get(f"/api/products/{product_id}/history?days=30", headers=first_headers)
                self.assertEqual(history.status_code, 200, history.text)
                self.assertEqual(len(history.json()["items"]), 1)
                self.assertEqual(history.json()["items"][0]["price"], 1234.5)

                duplicate = client.post("/api/products", headers=first_headers, json={"ozon_id": "123456789"})
                self.assertEqual(duplicate.status_code, 409)

                second_register = client.post(
                    "/api/auth/register",
                    json={"email": "second@example.com", "password": "another-password-456"},
                )
                self.assertEqual(second_register.status_code, 201, second_register.text)
                second_headers = {"Authorization": f"Bearer {second_register.json()['access_token']}"}
                cross_account_history = client.get(f"/api/products/{product_id}/history?days=30", headers=second_headers)
                self.assertEqual(cross_account_history.status_code, 404)

                invalid_id = client.post("/api/products", headers=first_headers, json={"ozon_id": "12abc"})
                self.assertEqual(invalid_id.status_code, 422)
        finally:
            api_main.app.dependency_overrides.clear()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
