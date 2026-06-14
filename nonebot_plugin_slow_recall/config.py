from pathlib import Path

from nonebot import get_driver
from pydantic import BaseModel


class Config(BaseModel):
    slow_recall_data_file: str = "data/nonebot_plugin_slow_recall/config.json"
    slow_recall_allow_superuser: bool = True
    slow_recall_allow_group_admin: bool = True
    slow_recall_allow_group_owner: bool = True

    @property
    def data_path(self) -> Path:
        path = Path(self.slow_recall_data_file).expanduser()
        if path.is_absolute():
            return path
        return Path.cwd() / path


def load_config() -> Config:
    driver = get_driver()
    raw_config = (
        driver.config.model_dump()
        if hasattr(driver.config, "model_dump")
        else driver.config.dict()
    )
    if hasattr(Config, "model_validate"):
        return Config.model_validate(raw_config)
    return Config.parse_obj(raw_config)
