"""環境變數設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """必填設定缺漏時拋出。"""


_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    password: str
    insecure: bool

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"


def load_config() -> Config:
    """從環境變數讀取設定,缺必填即拋出 ConfigError。"""
    host = os.environ.get("VCENTER_HOST", "").strip()
    user = os.environ.get("VCENTER_USER", "").strip()
    password = os.environ.get("VCENTER_PASSWORD", "")

    missing = [
        name
        for name, value in (
            ("VCENTER_HOST", host),
            ("VCENTER_USER", user),
            ("VCENTER_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "缺少必要環境變數: " + ", ".join(missing)
        )

    insecure = os.environ.get("VCENTER_INSECURE", "").strip().lower() in _TRUE

    return Config(host=host, user=user, password=password, insecure=insecure)
