from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    import_root: Path = Path("/media")  # read-only mount for "import from folder"
    public_base_url: str = "http://localhost:8090"  # what browsers use to load media

    admin_api_key: str = "change-me-admin"
    tool_api_key: str = "change-me-tool"
    signing_secret: str = "change-me-secret"

    ollama_url: str = "http://localhost:11434"
    vision_model: str = "qwen3-vl:27b"
    embed_model: str = "nomic-embed-text"
    # Task prefixes some embedding models expect; None = auto-detect from the model name.
    embed_doc_prefix: str | None = None
    embed_query_prefix: str | None = None
    ollama_timeout: float = 600.0
    ollama_keep_alive: str = "5m"

    min_score: float = 0.35
    search_k: int = 50
    default_cooldown_turns: int = 4
    keyframes: int = 4
    thumb_size: int = 384
    tag_max_attempts: int = 3

    def prefixes(self) -> tuple[str, str]:
        """(document_prefix, query_prefix) for the embedding model."""
        m = self.embed_model.lower()
        if "nomic" in m:
            auto = ("search_document: ", "search_query: ")
        elif "qwen3-embedding" in m:
            auto = ("", "Instruct: Given a description of a photo or video a character wants to send, retrieve the best matching media caption\nQuery: ")
        elif "mxbai" in m:
            auto = ("", "Represent this sentence for searching relevant passages: ")
        else:
            auto = ("", "")
        return (
            self.embed_doc_prefix if self.embed_doc_prefix is not None else auto[0],
            self.embed_query_prefix if self.embed_query_prefix is not None else auto[1],
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rpmedia.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"


@lru_cache
def get_settings() -> Settings:
    return Settings()
