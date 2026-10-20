from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.models import PricePoint, Product, User, utc_now
from app.ozon import OzonProductData
from app.schemas import AuthInput, ProductResolveInput
from app.security import (
    create_access_token,
    hash_password,
    load_or_create_secret,
    verify_access_token,
    verify_password,
)
from app.testing_tools import TEST_FIXTURE_PREFIX, seed_test_fixtures
import app.main as api_main


class SchemaAndSecurityTests(unittest.TestCase):
    def test_auth_input_normalizes_email_and_rejects_invalid_email(self) -> None:
        request = AuthInput(email="  PERSON@Example.COM ", password="a-long-test-password")
        self.assertEqual(request.email, "person@example.com")
        with self.assertRaises(ValueError):
            AuthInput(email="invalid-email", password="a-long-test-password")

    def test_auth_input_enforces_password_length(self) -> None:
        with self.assertRaises(ValueError):
            AuthInput(email="person@example.com", password="short")

    def test_product_id_accepts_only_numeric_ids_in_allowed_length(self) -> None:
        self.assertEqual(ProductResolveInput(ozon_id=" 123456789 ").ozon_id, "123456789")
        for invalid in ("1234", "12345x", "1" * 25, ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ProductResolveInput(ozon_id=invalid)

    def test_password_hash_is_not_plaintext_and_verifies(self) -> None:
        password = "correct-test-password"
        encoded = hash_password(password)
        self.assertNotEqual(encoded, password)
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(verify_password(password, encoded))
        self.assertFalse(verify_password("incorrect-password", encoded))

    def test_access_token_signature_is_verified(self) -> None:
        secret = b"unit-test-secret-that-is-at-least-32-bytes-long"
        token = create_access_token(123, secret)
        self.assertEqual(verify_access_token(token, secret), 123)
        self.assertIsNone(verify_access_token(token + "tampered", secret))
        self.assertIsNone(verify_access_token(token, b"different-secret-that-is-also-32-bytes"))

    def test_secret_is_created_once_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.key"
            first = load_or_create_secret(path)
            second = load_or_create_secret(path)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(first, second)


class DatabaseFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, class_=Session)
        self.db = self.session_factory()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_fixture_seed_creates_three_products_with_history_and_is_idempotent(self) -> None:
        first = seed_test_fixtures(self.db, "Fixture.User@example.com")
        self.assertTrue(first["user_created"])
        self.assertTrue(first["temporary_password"])
        self.assertEqual(first["email"], "fixture.user@example.com")
        self.assertEqual(first["products_created"], 3)
        self.assertEqual(first["history_points_created"], 24)
        self.assertEqual(len(first["fixtures"]), 3)
        self.assertTrue(all(item["is_test_fixture"] for item in first["fixtures"]))
        self.assertTrue(all(item["name"].startswith(TEST_FIXTURE_PREFIX) for item in first["fixtures"]))
        self.assertTrue(all(item["history_points"] == 8 for item in first["fixtures"]))

        second = seed_test_fixtures(self.db, "fixture.user@example.com")
        self.assertFalse(second["user_created"])
        self.assertIsNone(second["temporary_password"])
        self.assertEqual(second["products_created"], 0)
        self.assertEqual(second["history_points_created"], 0)
        self.assertEqual(len(second["fixtures"]), 3)
        self.assertEqual(self.db.scalar(select(func.count(Product.id))), 3)
        self.assertEqual(self.db.scalar(select(func.count(PricePoint.id))), 24)

    def test_fixture_seed_does_not_change_existing_account_password(self) -> None:
        existing_password = "keep-this-account-password"
        user = User(email="existing@example.com", password_hash=hash_password(existing_password))
        self.db.add(user)
        self.db.commit()
        old_hash = user.password_hash

        seeded = seed_test_fixtures(self.db, "existing@example.com")
        self.assertFalse(seeded["user_created"])
        self.assertIsNone(seeded["temporary_password"])
        self.assertEqual(user.password_hash, old_hash)
        self.assertTrue(verify_password(existing_password, user.password_hash))
        self.assertEqual(self.db.scalar(select(func.count(Product.id)).where(Product.user_id == user.id)), 3)


class ApiFunctionalTests(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, class_=Session)

        def override_get_db():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        api_main.app.dependency_overrides[get_db] = override_get_db
        self._engine_patch = patch.object(api_main, "engine", self.engine)
        self._session_patch = patch.object(api_main, "SessionLocal", self.session_factory)
        self._secret_patch = patch.object(api_main, "APP_SECRET", b"isolated-test-secret-which-is-long-enough")
        self._fetch_patch = patch.object(api_main, "fetch_product", new_callable=AsyncMock)
        self._engine_patch.start()
        self._session_patch.start()
        self._secret_patch.start()
        self.fetch_mock = self._fetch_patch.start()
        self.current_price = Decimal("1234.50")

        async def fake_fetch(ozon_id: str) -> OzonProductData:
            return OzonProductData(
                ozon_id=ozon_id,
                name=f"Fake integration item {ozon_id}",
                price=self.current_price,
                currency="RUB",
                url=f"https://www.ozon.ru/product/{ozon_id}/",
            )

        self.fetch_mock.side_effect = fake_fetch
        self.client_context = TestClient(api_main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        try:
            self.client_context.__exit__(None, None, None)
        finally:
            api_main.app.dependency_overrides.clear()
            self._fetch_patch.stop()
            self._secret_patch.stop()
            self._session_patch.stop()
            self._engine_patch.stop()
            self.engine.dispose()

    def register(self, email: str = "tester@example.com", password: str = "test-password-123") -> dict:
        response = self.client.post("/api/auth/register", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def auth_headers(auth_response: dict) -> dict[str, str]:
        return {"Authorization": f"Bearer {auth_response['access_token']}"}

    def test_health_returns_local_service_status(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["refresh_interval_hours"], 12)

    def test_protected_routes_reject_missing_or_invalid_token(self) -> None:
        self.assertEqual(self.client.get("/api/products").status_code, 401)
        self.assertEqual(
            self.client.get("/api/products", headers={"Authorization": "Bearer invalid.token"}).status_code,
            401,
        )

    def test_registration_duplicate_email_login_and_me(self) -> None:
        created = self.register(" TESTER@Example.com ")
        self.assertEqual(created["user"]["email"], "tester@example.com")

        duplicate = self.client.post(
            "/api/auth/register", json={"email": "tester@example.com", "password": "test-password-123"}
        )
        self.assertEqual(duplicate.status_code, 409)

        bad_login = self.client.post(
            "/api/auth/login", json={"email": "tester@example.com", "password": "wrong-password"}
        )
        self.assertEqual(bad_login.status_code, 401)

        good_login = self.client.post(
            "/api/auth/login", json={"email": "tester@example.com", "password": "test-password-123"}
        )
        self.assertEqual(good_login.status_code, 200, good_login.text)
        me = self.client.get("/api/me", headers=self.auth_headers(good_login.json()))
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["email"], "tester@example.com")

    def test_invalid_product_id_does_not_call_product_resolver(self) -> None:
        auth = self.register()
        response = self.client.post(
            "/api/products", headers=self.auth_headers(auth), json={"ozon_id": "abc12345"}
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.fetch_mock.assert_not_awaited()

    def test_add_list_duplicate_history_refresh_and_delete(self) -> None:
        auth = self.register()
        headers = self.auth_headers(auth)
        empty = self.client.get("/api/products", headers=headers)
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["count"], 0)

        added = self.client.post("/api/products", headers=headers, json={"ozon_id": "123456789"})
        self.assertEqual(added.status_code, 201, added.text)
        item = added.json()["item"]
        product_id = item["id"]
        self.assertEqual(item["current_price"], 1234.5)
        self.assertEqual(item["name"], "Fake integration item 123456789")
        self.assertEqual(self.fetch_mock.await_count, 1)

        duplicate = self.client.post("/api/products", headers=headers, json={"ozon_id": "123456789"})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self.fetch_mock.await_count, 1, "Duplicate must be rejected before any parser call")

        listed = self.client.get("/api/products", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)

        with self.session_factory() as db:
            product = db.get(Product, product_id)
            self.assertIsNotNone(product)
            for days_ago, price in ((100, "1400.00"), (20, "1300.00"), (5, "1200.00")):
                db.add(
                    PricePoint(
                        product_id=product_id,
                        price=Decimal(price),
                        currency="RUB",
                        observed_at=utc_now() - timedelta(days=days_ago),
                    )
                )
            db.commit()

        seven_days = self.client.get(f"/api/products/{product_id}/history?days=7", headers=headers)
        thirty_days = self.client.get(f"/api/products/{product_id}/history?days=30", headers=headers)
        one_eighty_days = self.client.get(f"/api/products/{product_id}/history?days=180", headers=headers)
        self.assertEqual([seven_days.status_code, thirty_days.status_code, one_eighty_days.status_code], [200, 200, 200])
        self.assertEqual(len(seven_days.json()["items"]), 2)  # first measurement + 5-day measurement
        self.assertEqual(len(thirty_days.json()["items"]), 3)
        self.assertEqual(len(one_eighty_days.json()["items"]), 4)
        for invalid_days in (0, 181):
            self.assertEqual(
                self.client.get(f"/api/products/{product_id}/history?days={invalid_days}", headers=headers).status_code,
                422,
            )

        self.current_price = Decimal("999.00")
        refreshed = self.client.post(f"/api/products/{product_id}/refresh", headers=headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["item"]["current_price"], 999.0)

        deleted = self.client.delete(f"/api/products/{product_id}", headers=headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get("/api/products", headers=headers).json()["count"], 0)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count(PricePoint.id))), 0)

    def test_users_cannot_read_or_delete_another_users_product(self) -> None:
        first = self.register("first@example.com")
        first_headers = self.auth_headers(first)
        added = self.client.post("/api/products", headers=first_headers, json={"ozon_id": "123456789"})
        self.assertEqual(added.status_code, 201, added.text)
        product_id = added.json()["item"]["id"]

        second = self.register("second@example.com", "another-password-456")
        second_headers = self.auth_headers(second)
        self.assertEqual(self.client.get("/api/products", headers=second_headers).json()["count"], 0)
        self.assertEqual(
            self.client.get(f"/api/products/{product_id}/history?days=30", headers=second_headers).status_code,
            404,
        )
        self.assertEqual(self.client.delete(f"/api/products/{product_id}", headers=second_headers).status_code, 404)

    def test_client_log_endpoint_accepts_sanitized_diagnostics(self) -> None:
        response = self.client.post(
            "/api/client-log",
            json={"level": "warning", "message": "test log\nline", "details": "test\tdetails"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "accepted")

    def test_extension_fixture_endpoint_is_opt_in_authenticated_and_idempotent(self) -> None:
        auth = self.register("fixture@example.com")
        headers = self.auth_headers(auth)
        with patch.dict("os.environ", {"OZON_ENABLE_TEST_TOOLS": "1"}):
            seeded = self.client.post(
                "/api/test-tools/seed", headers=headers, json={"email": "FIXTURE@example.com"}
            )
            self.assertEqual(seeded.status_code, 200, seeded.text)
            result = seeded.json()
            self.assertEqual(result["products_created"], 3)
            self.assertEqual(result["history_points_created"], 24)
            self.assertNotIn("temporary_password", result)
            self.assertEqual(len(result["fixtures"]), 3)

            products_response = self.client.get("/api/products", headers=headers)
            self.assertEqual(products_response.status_code, 200)
            fixtures = [item for item in products_response.json()["items"] if item["name"].startswith(TEST_FIXTURE_PREFIX)]
            self.assertEqual(len(fixtures), 3)

            # Demo rows must never trigger real Playwright/Ozon traffic from manual or scheduled refresh.
            blocked_refresh = self.client.post(
                f"/api/products/{fixtures[0]['id']}/refresh", headers=headers
            )
            self.assertEqual(blocked_refresh.status_code, 409, blocked_refresh.text)
            self.fetch_mock.assert_not_awaited()
            asyncio.run(api_main.refresh_all_products())
            self.fetch_mock.assert_not_awaited()

            first_id = fixtures[0]["id"]
            expected_counts = {7: 2, 30: 4, 180: 8}
            for days, expected in expected_counts.items():
                history = self.client.get(f"/api/products/{first_id}/history?days={days}", headers=headers)
                self.assertEqual(history.status_code, 200, history.text)
                self.assertEqual(len(history.json()["items"]), expected)

            repeated = self.client.post(
                "/api/test-tools/seed", headers=headers, json={"email": "fixture@example.com"}
            )
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(repeated.json()["products_created"], 0)
            self.assertEqual(repeated.json()["history_points_created"], 0)

            mismatch = self.client.post(
                "/api/test-tools/seed", headers=headers, json={"email": "someone-else@example.com"}
            )
            self.assertEqual(mismatch.status_code, 403)

    def test_fixture_endpoint_is_disabled_without_opt_in(self) -> None:
        auth = self.register()
        with patch.dict("os.environ", {}, clear=False):
            # Explicitly ensure no inherited environment variable enables this route.
            import os
            previous = os.environ.pop("OZON_ENABLE_TEST_TOOLS", None)
            try:
                response = self.client.post(
                    "/api/test-tools/seed",
                    headers=self.auth_headers(auth),
                    json={"email": "tester@example.com"},
                )
            finally:
                if previous is not None:
                    os.environ["OZON_ENABLE_TEST_TOOLS"] = previous
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
