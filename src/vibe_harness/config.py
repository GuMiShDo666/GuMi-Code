import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str


def load_settings() -> Settings:
    """
    1. 从当前工作目录加载 .env
    2. 从环境变量读取三个配置
    3. 任一配置缺失或为空白时抛出 RuntimeError
    4. 返回 Settings
    """
    load_dotenv(Path.cwd() / ".env")

    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")

    if not api_key or not api_key.strip():
        raise RuntimeError(
            "LLM_API_KEY 未设置，请在 .env 文件中配置（参考 .env.example）"
        )

    if not base_url or not base_url.strip():
        raise RuntimeError(
            "LLM_BASE_URL 未设置，请在 .env 文件中配置（参考 .env.example）"
        )

    if not model or not model.strip():
        raise RuntimeError(
            "LLM_MODEL 未设置，请在 .env 文件中配置（参考 .env.example）"
        )

    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
