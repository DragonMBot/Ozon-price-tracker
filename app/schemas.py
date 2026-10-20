from __future__ import annotations

import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Введите корректный email.")
        return value


class ProductResolveInput(BaseModel):
    ozon_id: str = Field(min_length=5, max_length=24)

    @field_validator("ozon_id")
    @classmethod
    def validate_ozon_id(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"\d{5,24}", value):
            raise ValueError("ID товара должен содержать от 5 до 24 цифр.")
        return value


class ClientLogInput(BaseModel):
    level: str = Field(default="INFO", max_length=10)
    message: str = Field(min_length=1, max_length=500)
    details: str | None = Field(default=None, max_length=1000)

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            return "INFO"
        return level

class TestFixtureSeedInput(BaseModel):

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Введите корректный email.")
        return value

