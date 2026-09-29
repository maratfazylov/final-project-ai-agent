import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    token: str = field(repr=False, default="")
    api_key: str = field(repr=False, default="")
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    data_dir: Path = ROOT / "data"
    output_dir: Path = ROOT / "output"
    allowed_users: set[int] = field(default_factory=set)
    scholar_key: str = field(repr=False, default="")
    min_sources: int = 15
    max_sources: int = 24
    max_llm_calls: int = 48
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_backend: str = "fastembed"
    soffice: str = ""

    @classmethod
    def load(cls):
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)

        def path(name, default):
            p = Path(os.getenv(name, default))
            return p if p.is_absolute() else ROOT / p

        settings = cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            data_dir=path("DATA_DIR", "data"),
            output_dir=path("OUTPUT_DIR", "output"),
            allowed_users={int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()},
            scholar_key=os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""),
            min_sources=max(15, int(os.getenv("MIN_SOURCES", "15"))),
            max_sources=int(os.getenv("MAX_SOURCES", "24")),
            max_llm_calls=int(os.getenv("MAX_LLM_CALLS", "48")),
            embedding_model=os.getenv("EMBEDDING_MODEL", cls.embedding_model),
            embedding_backend=os.getenv("EMBEDDING_BACKEND", "fastembed"),
            soffice=os.getenv("SOFFICE_PATH", "")
            or shutil.which("soffice")
            or (
                "/Applications/LibreOffice.app/Contents/MacOS/soffice"
                if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists()
                else ""
            ),
        )
        if settings.max_sources < settings.min_sources:
            raise ValueError("MAX_SOURCES должен быть не меньше MIN_SOURCES")
        for directory in (settings.data_dir, settings.output_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ.setdefault("MPLCONFIGDIR", str(settings.data_dir / "matplotlib"))
        return settings

    def require_credentials(self):
        if not self.token or not self.api_key or "replace_me" in (self.token, self.api_key):
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN и DEEPSEEK_API_KEY в .env")
