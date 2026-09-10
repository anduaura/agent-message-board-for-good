from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from amb.models import RiskTier


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="AMB_", extra="ignore", protected_namespaces=()
    )

    env: str = "dev"
    database_url: str = "sqlite:///./amb.db"

    # Governance
    quorum_size: int = 3
    quorum_threshold: int = 2
    human_signoff_tier: RiskTier = RiskTier.HIGH

    # Model-backed reviewers. Left unset, the board reviews with policy rules only,
    # which is strictly more conservative: every judgement call becomes a rejection.
    reviewer_model: str = "claude-opus-5"
    reviewer_effort: str = "high"
    anthropic_api_key: str | None = None

    # Economics
    treasury_share_bps: int = 1000

    # Board hygiene
    max_message_bytes: int = 16_000
    messages_per_agent_per_hour: int = 120
    claim_lease_minutes: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
