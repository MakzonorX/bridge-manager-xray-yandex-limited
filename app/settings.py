from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bridge_domain: str
    user_port: int = 443
    user_host_for_uri: str | None = None
    user_path: str = "/user-xh"
    user_transport_mode: str = "reality"
    user_network: str = "tcp"
    user_security: str = "reality"
    user_flow: str = "xtls-rprx-vision"

    reality_server_name: str = "ads.x5.ru"
    reality_public_key: str = ""
    reality_short_id: str = ""
    reality_fingerprint: str = "chrome"
    reality_spider_x: str = "/"

    xray_config: str = "/usr/local/etc/xray/config.json"
    xray_service: str = "xray"
    xray_api_addr: str = "127.0.0.1:10085"
    xray_bin: str = "/usr/local/bin/xray"

    api_token: str
    api_bind: str = "127.0.0.1"
    api_port: int = 8080

    db_path: str = "/opt/bridge-manager/data/bridge_manager.db"

    model_config = SettingsConfigDict(
        env_file="/etc/bridge-manager/env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
