"""Settings, read from the environment (.env is loaded if present)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def anthropic_key() -> str:
    return get("ANTHROPIC_API_KEY")


def claude_model() -> str:
    return get("CLAUDE_MODEL", "claude-sonnet-5-5")


def sec_user_agent() -> str:
    return get("SEC_USER_AGENT")


def database_url() -> str:
    return get("DATABASE_URL", f"sqlite:///{Path(__file__).resolve().parent.parent / 'data' / 'finsight.db'}")


def neo4j_settings() -> tuple[str, str, str]:
    return (
        get("NEO4J_URI", "bolt://localhost:7687"),
        get("NEO4J_USER", "neo4j"),
        get("NEO4J_PASSWORD", "finsight-neo4j"),
    )


def alert_webhook() -> str:
    return get("ALERT_WEBHOOK_URL")
