from __future__ import annotations

import os

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

load_dotenv()

_e = lambda k, d="": Field(default_factory=lambda k=k, d=d: os.getenv(k, d))

_MODES = {"brand_size", "keyword_pair", "brand_only"}


class Target(BaseModel):
    brand: str
    aliases: list[str]
    sizes: list[str] = []
    gender: str = ""
    exclude: list[str] = []
    match_mode: str = "brand_size"
    category: int | None = None
    max_price: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Target:
        if self.match_mode not in _MODES:
            raise ValueError(
                f"{self.brand}: unknown match_mode {self.match_mode!r}. "
                f"Expected one of {sorted(_MODES)}."
            )
        if not self.aliases:
            raise ValueError(f"{self.brand}: aliases must not be empty, nothing would match.")
        # An empty `sizes` under brand_size used to match every listing carrying
        # the brand, silently. Forgetting a size is a plausible typo, and the
        # failure looked like a flood of unrelated results rather than an error.
        # Matching on brand alone is now something you have to ask for.
        if self.match_mode == "brand_size" and not self.sizes:
            raise ValueError(
                f"{self.brand}: brand_size requires sizes. "
                f"Use match_mode: \"brand_only\" if matching on brand alone is intended."
            )
        return self


class Profile(BaseModel):
    name: str
    email: str
    targets: list[Target]


class Config(BaseModel):
    profiles: list[Profile]
    sgw_username: str = _e("SGW_USERNAME")
    sgw_password: str = _e("SGW_PASSWORD")
    email_sender: str = _e("EMAIL_SENDER")
    email_password: str = _e("EMAIL_PASSWORD")
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    request_delay_min: float = 2.0
    request_delay_max: float = 5.0
    max_items_per_email: int = 50
    send_empty_email: bool = True
    page_size: int = 40
    max_pages: int = 5


def load_config(path: str = "config.yaml") -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)
    settings = data.get("settings", {})

    # Multi-profile format
    if "profiles" in data:
        profiles = [
            Profile(name=p["name"], email=p["email"],
                    targets=[Target(**t) for t in p.get("targets", [])])
            for p in data["profiles"]
        ]
    else:
        # Legacy: flat targets list → single default profile
        profiles = [Profile(
            name="default",
            email=os.getenv("EMAIL_RECIPIENT", ""),
            targets=[Target(**t) for t in data.get("targets", [])],
        )]

    return Config(profiles=profiles, **settings)
