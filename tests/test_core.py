from __future__ import annotations

import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.ozon import (
    OzonFetchError, _check_response_status, _save_html_snapshot, fetch_product, parse_product_html,
)
from app.schemas import ProductResolveInput
from app.security import create_access_token, hash_password, load_or_create_secret, verify_access_token, verify_password


class OzonParserTests(unittest.TestCase):
    def test_parses_json_ld_product_and_price(self) -> None:
        html = '''<!doctype html><html><head><title>Карточка</title>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Тестовый товар",
         "offers":{"@type":"Offer","price":"12990.00","priceCurrency":"RUB"}}
        </script></head><body><h1>Тестовый товар</h1></body></html>'''
        parsed = parse_product_html(html, "123456789")
        self.assertEqual(parsed.name, "Тестовый товар")
        self.assertEqual(parsed.price, Decimal("12990.00"))
        self.assertEqual(parsed.currency, "RUB")

    def test_parses_meta_price_and_title(self) -> None:
        html = '''<html><head><meta property="og:title" content="Наушники | Ozon" />
        <meta property="product:price:amount" content="1 499,90" /></head><body></body></html>'''
        parsed = parse_product_html(html, "987654321")
        self.assertEqual(parsed.name, "Наушники")
        self.assertEqual(parsed.price, Decimal("1499.90"))

    def test_parses_price_from_rendered_product_widget(self) -> None:
        html = '<html><head><meta property="og:title" content="Наушники | Ozon" /></head><body></body></html>'
        parsed = parse_product_html(
            html,
            "987654321",
            rendered_price_candidates=["13 499 ₽"],
        )
        self.assertEqual(parsed.name, "Наушники")
        self.assertEqual(parsed.price, Decimal("13499.00"))

    def test_does_not_accept_ambiguous_multiple_prices_as_widget_value(self) -> None:
        html = '<html><head><meta property="og:title" content="Товар | Ozon" /></head><body></body></html>'
        with self.assertRaises(OzonFetchError):
            parse_product_html(
                html,
                "123456789",
                rendered_price_candidates=["Старая цена 15 999 ₽, новая цена 12 999 ₽"],
            )

    def test_does_not_guess_when_price_is_missing(self) -> None:
        with self.assertRaises(OzonFetchError):
            parse_product_html('<html><head><meta property="og:title" content="Товар" /></head></html>', "123456789")

    def test_refuses_captcha_page(self) -> None:
        with self.assertRaises(OzonFetchError):
            parse_product_html("<html><head><title>Доступ ограничен</title></head><body>captcha</body></html>", "123456789")

    def test_http_403_is_reported_as_access_denial_not_temporary_outage(self) -> None:
        with self.assertRaisesRegex(OzonFetchError, "отказал в доступе") as context:
            _check_response_status(403)
        self.assertIn("HTTP 403", str(context.exception))
        self.assertIn("не обходит CAPTCHA", str(context.exception))

    def test_http_429_and_503_have_distinct_messages(self) -> None:
        with self.assertRaisesRegex(OzonFetchError, "HTTP 429"):
            _check_response_status(429)
        with self.assertRaisesRegex(OzonFetchError, "HTTP 503"):
            _check_response_status(503)


    def test_http_errors_include_structured_diagnostics(self) -> None:
        with self.assertRaises(OzonFetchError) as context:
            _check_response_status(403, provider="playwright", diagnostics={"final_host": "www.ozon.ru"})
        error = context.exception
        self.assertEqual(error.code, "public_page_access_denied")
        self.assertEqual(error.http_status, 403)
        self.assertFalse(error.retryable)
        self.assertEqual(error.as_public_dict()["diagnostics"]["final_host"], "www.ozon.ru")
        secret_probe = OzonFetchError(
            "diagnostic test", diagnostics={"endpoint": "/v3/product/info/list", "api_key": "must-not-leak"}
        ).as_public_dict()
        self.assertNotIn("api_key", secret_probe["diagnostics"])


    def test_saves_html_snapshot_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "parser_output" / "page.html"
            saved = _save_html_snapshot("<html><body>captured</body></html>", output)
            self.assertEqual(saved, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "<html><body>captured</body></html>")
            self.assertFalse(output.with_suffix(output.suffix + ".tmp").exists())




class FetchDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_fetch_has_a_bounded_deadline_and_structured_error(self) -> None:
        async def delayed_capture(_ozon_id: str):
            await asyncio.sleep(0.05)

        with (
            patch("app.ozon.PRODUCT_FETCH_TIMEOUT_SECONDS", 0.01),
            patch("app.ozon._capture_html_and_parse", side_effect=delayed_capture),
        ):
            with self.assertRaises(OzonFetchError) as context:
                await fetch_product("123456789")

        self.assertEqual(context.exception.code, "product_fetch_deadline_exceeded")
        self.assertTrue(context.exception.retryable)
        self.assertEqual(context.exception.diagnostics["timeout_seconds"], 0.01)

class AuthPrimitiveTests(unittest.TestCase):
    def test_password_hash_and_verification(self) -> None:
        encoded = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong password", encoded))

    def test_token_signature(self) -> None:
        secret = b"s" * 48
        token = create_access_token(321, secret)
        self.assertEqual(verify_access_token(token, secret), 321)
        self.assertIsNone(verify_access_token(token + "bad", secret))

    def test_secret_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.key"
            first = load_or_create_secret(path)
            second = load_or_create_secret(path)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)

    def test_product_id_validation(self) -> None:
        self.assertEqual(ProductResolveInput(ozon_id="123456789").ozon_id, "123456789")
        with self.assertRaises(ValueError):
            ProductResolveInput(ozon_id="12a")


if __name__ == "__main__":
    unittest.main()
