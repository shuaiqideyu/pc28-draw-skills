import os
from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_dotenv() -> None:
    """读当前目录或仓库根的 `.env`，不覆盖已经存在的环境变量。"""
    here = Path(__file__).resolve()
    candidates = [Path.cwd() / ".env", here.parents[2] / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


class CollectorEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # 加拿大机房 + Chrome/H2 直连；BCLC 与 PlayNow 同后端、域名级冗余
    BCLC_PRIMARY_URL: HttpUrl = Field(
        default="https://www.bclc.com/services2/keno/nextdraw/"
    )
    BCLC_FALLBACK_URL: HttpUrl = Field(
        default="https://www.bclc.com/services2/keno/draw/latest/10"
    )
    BCLC_DRAW_URL_TEMPLATE: str = Field(
        default="https://www.bclc.com/services2/keno/draw/{draw_nbr}/1"
    )
    PLAYNOW_PRIMARY_URL: HttpUrl = Field(
        default="https://www.playnow.com/services2/keno/nextdraw/"
    )
    PLAYNOW_FALLBACK_URL: HttpUrl = Field(
        default="https://www.playnow.com/services2/keno/draw/latest/10"
    )
    # 与官方同时轮询。普通 HTTP，不走 Chrome 指纹。时间已是北京时间。
    HELP_KENO_URL: HttpUrl = Field(
        default="https://pc28.help/api/keno.json?nbr=1"
    )


def load_collector_env() -> CollectorEnv:
    _load_dotenv()
    try:
        return CollectorEnv()
    except Exception as exc:
        raise RuntimeError("采集环境变量校验失败") from exc
