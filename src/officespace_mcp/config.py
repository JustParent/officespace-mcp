"""Process-level configuration: one OfficeSpace tenant per server."""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    graphql_url: str = ""
    auth_header: str = "Authorization"
    auth_value: str = field(default="", repr=False)
    enable_mutations: bool = False
    schema_path: str | None = None
    timeout_seconds: float = 30
    max_response_bytes: int = 5_000_000
    max_records: int = 1000
    mcp_token: str = field(default="", repr=False)

    def __post_init__(self):
        if self.graphql_url:
            parsed = urlsplit(self.graphql_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "OFFICESPACE_GRAPHQL_URL must be HTTPS without credentials or query."
                )
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.auth_header):
            raise ValueError("OFFICESPACE_AUTH_HEADER must be an HTTP header name.")
        if self.auth_header.lower() in {"host", "content-type", "content-length", "accept"}:
            raise ValueError("OFFICESPACE_AUTH_HEADER must be an authentication header.")
        if any(c in self.auth_value + self.mcp_token for c in "\r\n"):
            raise ValueError("Authentication values must not contain newlines.")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0 or self.max_records <= 0:
            raise ValueError("Timeout and response limits must be positive.")

    @classmethod
    def from_env(cls):
        writes = os.getenv("OFFICESPACE_ENABLE_MUTATIONS", "false").lower()
        if writes not in {"true", "false"}:
            raise ValueError("OFFICESPACE_ENABLE_MUTATIONS must be true or false.")
        return cls(
            graphql_url=os.getenv("OFFICESPACE_GRAPHQL_URL", ""),
            auth_header=os.getenv("OFFICESPACE_AUTH_HEADER", "Authorization"),
            auth_value=os.getenv("OFFICESPACE_AUTH_VALUE", ""),
            enable_mutations=writes == "true",
            schema_path=os.getenv("OFFICESPACE_SCHEMA_PATH") or None,
            mcp_token=os.getenv("MCP_BEARER_TOKEN", ""),
        )
