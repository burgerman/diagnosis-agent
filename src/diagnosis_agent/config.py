from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "in-memory-analysis-agent"
    gemini_api_key: str
    gemini_model: str = "gemini-2.0-flash" # Standardizing for the agent
    
    max_log_snippets: int = 150
    max_context_files: int = 8
    max_context_excerpt_chars: int = 1600
    allowed_read_roots: str = "src,services,config"
    project_root: Path = Field(default_factory=Path.cwd)

    @property
    def read_roots(self) -> list[Path]:
        return [(self.project_root / r.strip()).resolve() for r in self.allowed_read_roots.split(",") if r.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()