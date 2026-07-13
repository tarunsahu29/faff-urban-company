"""Central config. All secrets come from env (.env) — never hard-coded."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Urban Company session / context (populated from DevTools recon)
    uc_cookie: str = ""
    uc_auth_token: str = ""
    uc_city: str = ""

    # Parser backend: "heuristic" (default) | "gemini" | "openai"
    parser_backend: str = "heuristic"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""

    @property
    def effective_parser(self) -> str:
        # Auto-use Gemini if a key is present, even if backend left as default.
        if self.gemini_api_key and self.parser_backend in ("heuristic", "gemini"):
            return "gemini"
        return self.parser_backend

    # Geocoding
    nominatim_user_agent: str = "faff-urban-company-demo/1.0"

    # BONUS safety gate. Must be True to allow a real (paid, dispatching) booking.
    # Stays False in the repo — flip only in your local .env for one cancellable run.
    allow_real_booking: bool = False

    @property
    def has_uc_session(self) -> bool:
        return bool(self.uc_cookie or self.uc_auth_token)


settings = Settings()
