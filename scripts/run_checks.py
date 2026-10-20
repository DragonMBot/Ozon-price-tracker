from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import LOG_DIR  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.testing_tools import normalize_email, seed_test_fixtures  # noqa: E402


def prompt_email() -> str:
    while True:
        raw_email = input("Введите email аккаунта для фиктивных тестовых данных: ").strip()
        try:
            return normalize_email(raw_email)
        except Exception as exc:
            print(f"Некорректный email: {exc}. Попробуйте ещё раз.")


def confirm_seed(email: str) -> bool:
    print("\nБудут выполнены тесты API, авторизации и базы данных без запуска парсера Ozon.")
    print(f"После успешных тестов в локальную базу будут добавлены 3 явно помеченных демо-товара для {email}.")
    print("У существующего аккаунта пароль не изменяется. Фиктивные товары останутся в базе до ручного удаления.")
    answer = input("Продолжить? [y/N]: ").strip().lower()
    return answer in {"y", "yes", "д", "да"}


def run_test_file(filename: str) -> bool:
    print(f"\n{'=' * 18} Тесты: {filename} {'=' * 18}", flush=True)
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        filename,
        "-v",
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return completed.returncode == 0


def seed_database(email: str) -> dict[str, Any]:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        result = seed_test_fixtures(db, email)
    # Never save the one-time generated password into a result file.
    safe_result = {key: value for key, value in result.items() if key != "temporary_password"}
    return {**safe_result, "temporary_password": result.get("temporary_password")}


def write_report(report: dict[str, Any]) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = LOG_DIR / f"test-report-{timestamp}.json"
    safe_report = dict(report)
    fixture_result = dict(safe_report.get("fixtures", {}))
    fixture_result.pop("temporary_password", None)
    safe_report["fixtures"] = fixture_result
    output.write_text(json.dumps(safe_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> int:
    print("Ozon Price Tracker — проверка компонентов без парсера")
    print("Эти тесты не открывают браузер и не отправляют запросы к Ozon.\n")
    email = prompt_email()
    if not confirm_seed(email):
        print("Отменено пользователем; тесты и база не изменены.")
        return 2

    files = ("test_api.py", "test_non_parser.py")
    results = {filename: run_test_file(filename) for filename in files}
    all_passed = all(results.values())
    report: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "parser_tests_included": False,
        "test_files": results,
        "all_tests_passed": all_passed,
        "fixtures": {},
    }

    if not all_passed:
        report_path = write_report(report)
        print(f"\nОШИБКА: часть тестов не прошла. Базу с фиктивными товарами не изменяли.\nОтчёт: {report_path}")
        return 1

    try:
        fixtures = seed_database(email)
        report["fixtures"] = {key: value for key, value in fixtures.items() if key != "temporary_password"}
    except Exception as exc:
        report["fixture_seed_error"] = f"{type(exc).__name__}: {exc}"
        report_path = write_report(report)
        print(f"\nТесты прошли, но не удалось подготовить фиктивные данные: {exc}\nОтчёт: {report_path}")
        return 1

    report_path = write_report(report)
    print("\nРЕЗУЛЬТАТ: все тесты прошли.")
    print(f"Email тестового аккаунта: {fixtures['email']}")
    if fixtures.get("user_created"):
        print("Создан новый локальный тестовый аккаунт. Пароль для первого входа (показывается только сейчас):")
        print(fixtures["temporary_password"])
    else:
        print("Аккаунт уже существовал; пароль не менялся.")
    print(f"Добавлено новых фиктивных товаров: {fixtures['products_created']}")
    print(f"Добавлено точек истории цены: {fixtures['history_points_created']}")
    print("\nВойди в расширении с указанным email, чтобы увидеть товары с префиксом [ТЕСТОВЫЕ ДАННЫЕ].")
    print("Открыть график можно как у обычного товара; история содержит демонстрационные цены, не цены Ozon.")
    print(f"Отчёт тестов: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
