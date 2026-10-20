from __future__ import annotations

from camoufox.async_api import AsyncCamoufox
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import MAX_OZON_HTML_BYTES

try:
    from playwright.async_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError as exc:  # Keep pure HTML parsing tests usable without browser extras.
    async_playwright = None  # type: ignore[assignment]
    PlaywrightError = RuntimeError  # type: ignore[misc,assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[misc,assignment]
    _PLAYWRIGHT_IMPORT_ERROR: ImportError | None = exc
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

logger = logging.getLogger("ozon_tracker.ozon")

# Price texts are accepted only when they contain a currency marker, or when a
# dedicated price node returns a single unambiguous numeric amount.
_PRICE_WITH_CURRENCY_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0\u202f]\d{3})*(?:[,.]\d{1,2})?|\d+(?:[,.]\d{1,2})?)"
    r"\s*(?:₽|руб(?:\.?|ля|лей)?|RUB)(?![A-Za-z])",
    re.IGNORECASE,
)
_PLAIN_PRICE_RE = re.compile(
    r"\s*(?:\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d+)"
    r"(?:[,.]\d{1,2})?\s*"
)
_ANTI_BOT_MARKERS = (
    "доступ ограничен",
    "вы не робот",
    "подтвердите, что вы не робот",
    "подтвердите что вы не робот",
    "подозрительный трафик",
    "captcha",
    "access denied",
    "verify you are human",
    "unusual traffic",
)


@dataclass(frozen=True, slots=True)
class OzonProductData:
    ozon_id: str
    name: str
    price: Decimal
    currency: str
    url: str


class OzonFetchError(RuntimeError):

    def __init__(
        self,
        message: str,
        *,
        code: str = "fetch_failed",
        provider: str = "unknown",
        http_status: int | None = None,
        retryable: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.http_status = http_status
        self.retryable = retryable
        self.diagnostics = dict(diagnostics or {})

    def add_context(
        self,
        *,
        provider: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> "OzonFetchError":
        if provider and self.provider == "unknown":
            self.provider = provider
        if diagnostics:
            self.diagnostics.update(diagnostics)
        return self

    def as_public_dict(self) -> dict[str, Any]:
        allowed = {
            "product_id", "navigation_status", "final_host", "page_title", "html_bytes",
            "failed_request_count", "failed_resource_types", "duration_ms", "endpoint",
            "provider", "http_status", "response_bytes", "returned_items", "exception_type",
        }
        safe: dict[str, Any] = {}
        for key, value in self.diagnostics.items():
            if key not in allowed:
                continue
            if value is None or isinstance(value, (bool, int, float)):
                safe[key] = value
            elif isinstance(value, str):
                safe[key] = value[:180]
            elif key == "failed_resource_types" and isinstance(value, list):
                safe[key] = [str(item)[:40] for item in value[:12]]
        return {
            "message": str(self),
            "code": self.code,
            "provider": self.provider,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "diagnostics": safe,
        }


def product_url(ozon_id: str) -> str:
    return f"https://www.ozon.ru/product/{ozon_id}/"


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _clean_decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            return None
    else:
        value = str(raw).replace("\u00a0", " ").replace("\u202f", " ").strip()
        # This function expects one amount, not a sentence containing several prices.
        if not _PLAIN_PRICE_RE.fullmatch(value):
            return None
        value = re.sub(r"\s+", "", value)
        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            tail = len(value.rsplit(",", 1)[1])
            value = value.replace(",", ".") if tail in (1, 2) else value.replace(",", "")
        elif value.count(".") > 1:
            chunks = value.split(".")
            value = "".join(chunks[:-1]) + ("." + chunks[-1] if len(chunks[-1]) in (1, 2) else chunks[-1])
        try:
            amount = Decimal(value)
        except InvalidOperation:
            return None
    if not amount.is_finite() or amount < Decimal("0.01") or amount > Decimal("100000000"):
        return None
    return amount.quantize(Decimal("0.01"))


def _price_from_text(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    value = raw.replace("\u00a0", " ").replace("\u202f", " ").strip()
    currency_matches = list(_PRICE_WITH_CURRENCY_RE.finditer(value))
    if len(currency_matches) == 1:
        return _clean_decimal(currency_matches[0].group(1))
    if len(currency_matches) > 1:
        # Multiple amounts may include both old and discounted prices; don't guess.
        return None
    # Permit no-currency text only for a single isolated amount from a known price node.
    if _PLAIN_PRICE_RE.fullmatch(value):
        return _clean_decimal(value)
    return None


def _node_type_is_product(node: dict[str, Any]) -> bool:
    node_type = node.get("@type", "")
    types = node_type if isinstance(node_type, list) else [node_type]
    return any(str(item).lower() == "product" for item in types)


def _price_from_offers(offers: Any) -> Decimal | None:
    for node in _walk(offers):
        if not isinstance(node, dict):
            continue
        for key in ("price", "lowPrice", "priceSpecification"):
            if key not in node:
                continue
            candidate = node[key]
            if key == "priceSpecification" and isinstance(candidate, dict):
                candidate = candidate.get("price")
            parsed = _clean_decimal(candidate)
            if parsed is not None:
                return parsed
    return None


def _is_anti_bot_page(html: str, soup: BeautifulSoup | None = None) -> bool:
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    visible_text = soup.get_text(" ", strip=True).lower()[:30_000]
    combined = f"{title} {visible_text}"
    return any(marker in combined for marker in _ANTI_BOT_MARKERS)


def _extract_product_name(soup: BeautifulSoup) -> str | None:
    name: str | None = None
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(decoded):
            if isinstance(node, dict) and _node_type_is_product(node):
                candidate = node.get("name")
                if isinstance(candidate, str) and candidate.strip():
                    name = candidate.strip()
                    break
        if name:
            break

    if not name:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            name = str(og_title["content"]).strip()
    if not name:
        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(" ", strip=True)
    if not name and soup.title:
        name = soup.title.get_text(" ", strip=True)
    if name:
        name = re.sub(r"\s*[|—-]\s*(?:интернет-магазин\s*)?Ozon.*$", "", name, flags=re.I).strip()
        return name[:500] or None
    return None


def _extract_structured_price(soup: BeautifulSoup) -> Decimal | None:
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(decoded):
            if isinstance(node, dict) and _node_type_is_product(node):
                price = _price_from_offers(node.get("offers"))
                if price is None:
                    price = _clean_decimal(node.get("price"))
                if price is not None:
                    return price

    for selector in (
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
        '[itemprop="price"][content]',
    ):
        element = soup.select_one(selector)
        if element:
            raw_value = element.get("content") or element.get_text(" ", strip=True)
            price = _clean_decimal(raw_value)
            if price is not None:
                return price
    return None


def parse_product_html(
    html: str,
    ozon_id: str,
    url: str | None = None,
    rendered_price_candidates: Sequence[str] = (),
) -> OzonProductData:
    if not html.strip():
        raise OzonFetchError("Ozon вернул пустую страницу.")
    if len(html.encode("utf-8", errors="ignore")) > MAX_OZON_HTML_BYTES:
        raise OzonFetchError("Страница Ozon слишком большая для безопасного разбора.")

    soup = BeautifulSoup(html, "html.parser")
    if _is_anti_bot_page(html, soup):
        raise OzonFetchError(
            "Ozon показал CAPTCHA или антибот-проверку. Расширение не пытается обходить её. "
            "Повторите попытку вручную позже либо используйте разрешённый API/поставщика данных.",
            code="challenge_detected",
            provider="playwright",
            retryable=False,
        )

    name = _extract_product_name(soup)
    price = _extract_structured_price(soup)
    if price is None:
        for candidate in rendered_price_candidates:
            price = _price_from_text(candidate)
            if price is not None:
                break

    if not name or price is None:
        missing = []
        if not name:
            missing.append("название")
        if price is None:
            missing.append("цена")
        logger.warning("Could not parse %s for Ozon product id=%s", " и ".join(missing), ozon_id)
        raise OzonFetchError(
            f"Не удалось достоверно определить {' и '.join(missing)} товара. "
            "Страница открылась, но обязательные данные не найдены в JSON-LD, метаданных "
            "или выделенном ценовом блоке. Товар не сохранён.",
            code="parse_failed",
            provider="playwright",
            retryable=False,
        )

    return OzonProductData(
        ozon_id=ozon_id,
        name=name,
        price=price,
        currency="RUB",
        url=url or product_url(ozon_id),
    )


async def _read_rendered_price_candidates(page: Any) -> list[str]:
    candidates: list[str] = []
    selectors = (
        '[data-widget="webPrice"]',
        '[data-widget="price"]',
        '[itemprop="price"]',
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 5)
            for index in range(count):
                element = locator.nth(index)
                try:
                    if selector != '[itemprop="price"]' and not await element.is_visible():
                        continue
                    content = await element.get_attribute("content")
                    text = content or await element.inner_text(timeout=1_500)
                except PlaywrightTimeoutError:
                    continue
                if text and text.strip() and text not in candidates:
                    candidates.append(text.strip()[:500])
        except PlaywrightError as exc:
            logger.debug("Could not read price selector %s (%s)", selector, type(exc).__name__)
    return candidates


def _check_response_status(
    status_code: int | None,
    *,
    provider: str = "playwright",
    diagnostics: dict[str, Any] | None = None,
) -> None:
    if status_code is None or 200 <= status_code < 300:
        return
    details = dict(diagnostics or {})
    if status_code == 404:
        raise OzonFetchError(
            "Ozon не нашёл товар (HTTP 404). Проверьте ID и доступность карточки.",
            code="not_found", provider=provider, http_status=404, retryable=False, diagnostics=details,
        )
    if status_code in (401, 403):
        raise OzonFetchError(
            f"Ozon отказал в доступе к публичной карточке (HTTP {status_code}). "
            "Это ограничение на стороне источника, а не ошибка разбора HTML. "
            "Расширение не обходит CAPTCHA или другие проверки доступа; "
            "попробуйте открыть карточку вручную или используйте разрешённый источник данных.",
            code="public_page_access_denied", provider=provider, http_status=status_code,
            retryable=False, diagnostics=details,
        )
    if status_code == 429:
        raise OzonFetchError(
            "Ozon вернул HTTP 429 — ограничение частоты запросов. Автоматических повторов нет; "
            "дождитесь следующего планового обновления и проверьте допустимую частоту запросов.",
            code="rate_limited", provider=provider, http_status=429, retryable=True, diagnostics=details,
        )
    if status_code in (502, 503, 504):
        raise OzonFetchError(
            f"Ozon временно недоступен (HTTP {status_code}). Автоматический повтор не выполняется; "
            "проверьте соединение и повторите позже.",
            code="upstream_unavailable", provider=provider, http_status=status_code,
            retryable=True, diagnostics=details,
        )
    if status_code >= 500:
        raise OzonFetchError(
            f"Ozon вернул серверную ошибку HTTP {status_code}.",
            code="upstream_error", provider=provider, http_status=status_code,
            retryable=True, diagnostics=details,
        )


# Capture behavior is based on the navigation-stability approach: wait until the
# main frame has been quiet, read page.content(), then atomically save the snapshot.
NAVIGATION_QUIET_SECONDS = 8.0
MAX_SETTLE_SECONDS = 60.0
NAVIGATION_TIMEOUT_MS = 30_000
CONTENT_RETRY_LIMIT = 5
OUTPUT_FILE = Path(os.getenv("OZON_HTML_OUTPUT", "parser_output/page.html"))
_BROWSER_LOCK = asyncio.Lock()


async def _wait_for_navigation_quiet(state: dict[str, Any]) -> bool:
    deadline = time.monotonic() + MAX_SETTLE_SECONDS
    while time.monotonic() < deadline:
        last_navigation = float(state["last_navigation_at"])
        if time.monotonic() - last_navigation >= NAVIGATION_QUIET_SECONDS:
            return True
        await asyncio.sleep(0.1)
    return False


def _save_html_snapshot(html: str, output_file: Path | None = None) -> Path:
    destination = output_file or OUTPUT_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


async def _capture_html_and_parse(ozon_id: str) -> OzonProductData:
    normalized_id = str(ozon_id).strip()
    if not re.fullmatch(r"\d{1,20}", normalized_id):
        raise OzonFetchError("ID товара Ozon должен состоять только из цифр.", code="invalid_product_id")
    if async_playwright is None:
        raise OzonFetchError(
            "Библиотека Playwright не установлена. Выполните: python -m pip install -r requirements.txt",
            code="playwright_not_installed", provider="playwright", retryable=False,
        ) from _PLAYWRIGHT_IMPORT_ERROR

    url = product_url(normalized_id)
    started_at = time.monotonic()
    state: dict[str, Any] = {
        "last_navigation_at": time.monotonic(),
        "navigation_count": 0,
    }
    diagnostics: dict[str, Any] = {
        "product_id": normalized_id,
        "navigation_status": None,
        "final_host": None,
        "page_title": None,
        "html_bytes": None,
        "failed_request_count": 0,
        "failed_resource_types": [],
        "duration_ms": None,
    }
    logger.info("Capturing public Ozon product HTML with standard Playwright: product_id=%s", normalized_id)

    try:
        async with AsyncCamoufox(headless=False) as browser:
            try:
                page = await browser.new_page()
                page.set_default_timeout(8_000)
                page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
                failed_resource_types: list[str] = []
                failed_request_count = 0

                def on_main_frame_navigation(frame: Any) -> None:
                    if frame == page.main_frame:
                        state["last_navigation_at"] = time.monotonic()
                        state["navigation_count"] += 1

                def record_failed_request(request: Any) -> None:
                    nonlocal failed_request_count
                    failed_request_count += 1
                    resource_type = str(getattr(request, "resource_type", "other"))[:40]
                    if resource_type not in failed_resource_types and len(failed_resource_types) < 12:
                        failed_resource_types.append(resource_type)

                page.on("framenavigated", on_main_frame_navigation)
                page.on("requestfailed", record_failed_request)

                response = None
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError as exc:
                    if page.url in ("about:blank", ""):
                        raise OzonFetchError(
                            "Истекло время ожидания ответа Ozon до создания документа. Проверьте сеть, DNS и доступность сайта.",
                            code="navigation_timeout", provider="playwright", retryable=True,
                            diagnostics=diagnostics,
                        ) from exc
                    logger.warning("Navigation timed out after a document was created: product_id=%s", normalized_id)
                except PlaywrightError as exc:
                    if page.url in ("about:blank", ""):
                        raise OzonFetchError(
                            "Playwright не смог создать документ. Проверьте сеть и журнал приложения.",
                            code="navigation_failed", provider="playwright", retryable=True,
                            diagnostics={**diagnostics, "exception_type": type(exc).__name__},
                        ) from exc
                    logger.warning("Navigation raised after document creation: product_id=%s error=%s", normalized_id, type(exc).__name__)

                try:
                    await page.locator("body").wait_for(state="attached", timeout=NAVIGATION_TIMEOUT_MS)
                except PlaywrightTimeoutError as exc:
                    raise OzonFetchError(
                        "После навигации не появился элемент body; стабильный HTML получить не удалось.",
                        code="document_body_missing", provider="playwright", retryable=True,
                        diagnostics={**diagnostics, "exception_type": type(exc).__name__},
                    ) from exc

                html: str | None = None
                for _attempt in range(CONTENT_RETRY_LIMIT):
                    if not await _wait_for_navigation_quiet(state):
                        raise OzonFetchError(
                            "Страница продолжает обновляться; стабильный HTML получить не удалось.",
                            code="navigation_unstable", provider="playwright", retryable=True,
                            diagnostics=diagnostics,
                        )
                    navigation_count_before = state["navigation_count"]
                    try:
                        candidate_html = await page.content()
                    except PlaywrightError:
                        await asyncio.sleep(10)
                        if navigation_count_before != state["navigation_count"]:
                            continue
                        raise
                    await asyncio.sleep(0.2)
                    if navigation_count_before != state["navigation_count"]:
                        continue
                    candidate_size = len(candidate_html.encode("utf-8", errors="ignore"))
                    if candidate_size > MAX_OZON_HTML_BYTES:
                        diagnostics["html_bytes"] = candidate_size
                        raise OzonFetchError(
                            "HTML страницы превышает допустимый размер; слишком большой снимок не сохранён.",
                            code="html_snapshot_too_large", provider="playwright", retryable=False,
                            diagnostics=diagnostics,
                        )
                    _save_html_snapshot(candidate_html)
                    if navigation_count_before != state["navigation_count"]:
                        continue
                    html = candidate_html
                    print(html)
                    break

                if html is None:
                    raise OzonFetchError(
                        f"Страница обновлялась во время чтения {CONTENT_RETRY_LIMIT} раз подряд.",
                        code="html_capture_unstable", provider="playwright", retryable=True,
                        diagnostics=diagnostics,
                    )

                try:
                    title = await page.title()
                except PlaywrightError:
                    title = ""
                try:
                    visible_text = await page.locator("body").inner_text(timeout=2_000)
                except PlaywrightTimeoutError:
                    visible_text = ""

                final_url = page.url or url
                final_host = urlparse(final_url).hostname
                response_status = response.status if response is not None else None
                diagnostics.update({
                    "navigation_status": response_status,
                    "final_host": final_host,
                    "page_title": title[:180],
                    "html_bytes": len(html.encode("utf-8", errors="ignore")),
                    "failed_request_count": failed_request_count,
                    "failed_resource_types": failed_resource_types,
                    "duration_ms": round((time.monotonic() - started_at) * 1000),
                })

                # The HTML snapshot is saved before classifying the upstream response,
                # so an access denial can be diagnosed locally without being bypassed.
                _check_response_status(response_status, provider="playwright", diagnostics=diagnostics)

                combined_visible_text = f"{title}\n{visible_text}".lower()
                if any(marker in combined_visible_text for marker in _ANTI_BOT_MARKERS):
                    raise OzonFetchError(
                        "Ozon показал CAPTCHA или антибот-проверку. HTML сохранён локально, автоматического обхода проверки нет.",
                        code="challenge_detected", provider="playwright", retryable=False,
                        diagnostics=diagnostics,
                    )

                parsed_url = urlparse(final_url)
                if parsed_url.scheme != "https" or not (
                    parsed_url.hostname == "ozon.ru" or (parsed_url.hostname or "").endswith(".ozon.ru")
                ):
                    raise OzonFetchError(
                        "Ozon перенаправил браузер на неожиданный домен. Данные не будут извлечены.",
                        code="unexpected_redirect", provider="playwright", retryable=False,
                        diagnostics=diagnostics,
                    )

                try:
                    price_candidates = await _read_rendered_price_candidates(page)
                    return parse_product_html(
                        html,
                        normalized_id,
                        url=final_url,
                        rendered_price_candidates=price_candidates,
                    )
                except OzonFetchError as exc:
                    raise exc.add_context(provider="playwright", diagnostics=diagnostics) from exc
            finally:
                await browser.close()
    except OzonFetchError:
        raise
    except PlaywrightTimeoutError as exc:
        diagnostics["duration_ms"] = round((time.monotonic() - started_at) * 1000)
        logger.warning("Playwright timeout: product_id=%s diagnostics=%s", normalized_id, diagnostics)
        raise OzonFetchError(
            "Истекло время ожидания страницы Ozon. Проверьте соединение и журнал приложения.",
            code="playwright_timeout", provider="playwright", retryable=True,
            diagnostics=diagnostics,
        ) from exc
    except PlaywrightError as exc:
        diagnostics["duration_ms"] = round((time.monotonic() - started_at) * 1000)
        logger.warning(
            "Playwright error: product_id=%s exception_type=%s diagnostics=%s",
            normalized_id, type(exc).__name__, diagnostics,
        )
        message = str(exc).lower()
        if "executable doesn't exist" in message or "browser" in message and "install" in message:
            user_message = "Браузер Chromium не установлен. Выполните: python -m playwright install chromium"
            code = "playwright_browser_missing"
        else:
            user_message = "Не удалось открыть страницу Ozon через Playwright. Проверьте сеть, браузер и логи."
            code = "playwright_error"
        raise OzonFetchError(
            user_message, code=code, provider="playwright", retryable=True,
            diagnostics={**diagnostics, "exception_type": type(exc).__name__},
        ) from exc
    except OSError as exc:
        diagnostics["duration_ms"] = round((time.monotonic() - started_at) * 1000)
        logger.exception("Failed to write or read captured Ozon HTML: product_id=%s", normalized_id)
        raise OzonFetchError(
            "Не удалось сохранить HTML в parser_output/page.html. Проверьте права на запись и свободное место.",
            code="html_snapshot_write_failed", provider="playwright", retryable=False,
            diagnostics={**diagnostics, "exception_type": type(exc).__name__},
        ) from exc
    except Exception as exc:
        diagnostics["duration_ms"] = round((time.monotonic() - started_at) * 1000)
        logger.exception("Unexpected HTML capture error: product_id=%s", normalized_id)
        raise OzonFetchError(
            "Непредвиденная ошибка получения HTML. Проверьте журнал приложения.",
            code="html_capture_failed", provider="playwright", retryable=False,
            diagnostics={**diagnostics, "exception_type": type(exc).__name__},
        ) from exc


async def fetch_product(ozon_id: str) -> OzonProductData:
    # A single output path is preserved from the user's sample code. Serializing the
    # complete capture avoids concurrent requests overwriting each other's snapshot.
    async with _BROWSER_LOCK:
        return await _capture_html_and_parse(ozon_id)
