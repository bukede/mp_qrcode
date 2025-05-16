from pydantic_settings import BaseSettings


class SettingsEnv(BaseSettings):
    api_token: str
    appid: str
    log_yaml: str
    mp_token: str
    secret_key: str

    class Config:
        env_file = ".env"
