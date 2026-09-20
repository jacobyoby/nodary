"""Typed schemas for the local dashboard REST API.

The dashboard is localhost-only (``127.0.0.1``) and all schemas describe
local SQLite projections — no subjects, filenames, full URLs, or body text
are ever included.  These ``TypedDict`` definitions are the single source
of truth for the wire contract; ``server.py`` returns exactly these shapes
via ``jsonify`` and the OpenAPI document below is derived from them so
tooling can validate without adding a runtime Pydantic dependency.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

TrustTier = Literal[0, 1, 2, 3]
TierLabel = Literal[
    "never seen",
    "sender new, organization known",
    "prior one-way contact",
    "established",
]


class ScoreFeature(TypedDict):
    """One weighted feature contribution for a scored message."""

    feature: str
    raw_value: float
    weight: float
    contribution: float
    explanation: str


class ScoredMessage(TypedDict):
    """One row from ``GET /api/messages`` (current tier + scored payload)."""

    id: int
    message_id: str | None
    sender_id: int
    from_email_norm: str
    from_display_name: str | None
    sent_at: int | None
    n_attachments: int
    n_links: int
    size_bytes: int | None
    anomaly_score: float
    tier: TrustTier
    trust_tier_at_scoring: TrustTier
    baseline_n: int
    engine_version: str
    tier_label: TierLabel
    features: list[ScoreFeature]
    account_id: int
    sender_msg_count: int


class MessagesQuery(TypedDict, total=False):
    """Query string for ``GET /api/messages``."""

    limit: NotRequired[str]  # int-like, clamped 1..1000, default 200
    tier: NotRequired[str]  # int-like 0..3; malformed -> ignored
    account: NotRequired[str]  # int id, "all", or "" (default "all")


class AccountStatusRow(TypedDict):
    id: int
    email: str
    auth_method: str
    last_error: str | None
    last_synced_at: int | None
    skip_count: int


class ServerDeletedByFolder(TypedDict):
    folder: str
    count: int


class StatusResponse(TypedDict):
    messages: int
    incoming: int
    unique_incoming: int
    senders: int
    encryption: str | None
    accounts: list[AccountStatusRow]
    total_skipped: int
    has_errors: bool
    psl_version: str
    psl_stored_version: str
    psl_drift: bool
    server_deleted_count: int
    server_deleted_by_folder: list[ServerDeletedByFolder]


class StatusQuery(TypedDict, total=False):
    account: NotRequired[str]


class SkippedRow(TypedDict):
    id: int
    account_id: int
    uid: int
    reason: str
    skipped_at: int
    folder_name: str
    account_email: str


class AccountRow(TypedDict):
    id: int
    email: str


class SizeBaselines(TypedDict):
    typical_bytes: int | None
    log_mean: float | None
    log_std: float | None


class LinksBaselines(TypedDict):
    mean: float | None
    std: float | None
    n_with_links: int


class AttachmentTypeEntry(TypedDict):
    extension: str
    mime_type: str
    n: int


class LinkDomainEntry(TypedDict):
    reg_domain: str
    n: int


class ReplyToEntry(TypedDict):
    email_norm: str
    n: int


class SenderDetailResponse(TypedDict):
    """Payload for ``GET /api/senders/<id>``."""

    id: int
    email_norm: str
    display_name: str | None
    domain: str
    reg_domain: str
    is_freemail: bool
    first_seen_at: int | None
    last_seen_at: int | None
    trust_tier: TrustTier
    tier_label: TierLabel
    tier_rule: str
    n_messages: int
    n_threads: int
    n_replied_threads: int
    n_user_initiated: int
    n_with_attachments: int
    n_with_links: int
    n_replyto_divergent: int
    first_msg_at: int | None
    last_msg_at: int | None
    span_seconds: int | None
    hour_histogram: list[int]  # length 24
    size: SizeBaselines
    links: LinksBaselines
    attachment_types: list[AttachmentTypeEntry]
    link_domains: list[LinkDomainEntry]
    reply_to: list[ReplyToEntry]
    recent_messages: list[ScoredMessage]


class ErrorResponse(TypedDict):
    error: str
    code: str
    details: NotRequired[str]


class Pagination(TypedDict):
    limit: int
    returned: int
    total: NotRequired[int]


class PaginatedMessages(TypedDict):
    data: list[ScoredMessage]
    pagination: Pagination


class PaginatedSkipped(TypedDict):
    data: list[SkippedRow]
    pagination: Pagination


class PaginatedAccounts(TypedDict):
    data: list[AccountRow]
    pagination: Pagination


# ---------------------------------------------------------------------------
# OpenAPI generation (no external dep).  Kept in this module so the spec
# cannot drift from the TypedDict contracts above.
# ---------------------------------------------------------------------------

OPENAPI_VERSION = "3.0.3"


def get_openapi_spec() -> dict:
    """Return an OpenAPI 3.0 dict describing the dashboard API."""
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "nodary local dashboard",
            "version": "0.3.1",
            "description": "Localhost-only dashboard — binds 127.0.0.1, no external assets, no outbound requests.",
        },
        "servers": [{"url": "http://127.0.0.1:8321", "description": "local dashboard"}],
        "paths": {
            "/": {
                "get": {
                    "summary": "Self-contained HTML dashboard",
                    "responses": {"200": {"description": "HTML page"}},
                }
            },
            "/api/status": {
                "get": {
                    "summary": "Sync health and aggregate counts",
                    "parameters": [
                        {
                            "name": "account",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "default": "all",
                                "example": "all",
                            },
                            "description": "Account id or 'all' (default). Unknown id -> 404.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "StatusResponse",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/StatusResponse"
                                    }
                                }
                            },
                        },
                        "404": {
                            "description": "Unknown account",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ErrorResponse"
                                    }
                                }
                            },
                        },
                    },
                }
            },
            "/api/messages": {
                "get": {
                    "summary": "Top scored incoming messages, one per sender",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1000,
                                "default": 200,
                            },
                        },
                        {
                            "name": "tier",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "enum": [0, 1, 2, 3]},
                            "description": "Filter by current trust tier; malformed -> 400 invalid_tier.",
                        },
                        {
                            "name": "account",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "all"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "PaginatedMessages",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedMessages"
                                    }
                                }
                            },
                        },
                        "400": {
                            "description": "Invalid limit or tier",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ErrorResponse"
                                    }
                                }
                            },
                        },
                        "404": {
                            "description": "Unknown account",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ErrorResponse"
                                    }
                                }
                            },
                        },
                    },
                }
            },
            "/api/accounts": {
                "get": {
                    "summary": "List configured accounts",
                    "description": "Wrapped in {data, pagination} — wire is snake_case anomaly_score for Python parity (P2-1).",
                    "responses": {
                        "200": {
                            "description": "PaginatedAccounts",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedAccounts"
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/skipped": {
                "get": {
                    "summary": "Permanently skipped messages (no content)",
                    "responses": {
                        "200": {
                            "description": "PaginatedSkipped",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedSkipped"
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/senders/{sender_id}": {
                "get": {
                    "summary": "Sender drill-down (baselines, histogram, recent messages)",
                    "parameters": [
                        {
                            "name": "sender_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "SenderDetailResponse",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/SenderDetailResponse"
                                    }
                                }
                            },
                        },
                        "404": {
                            "description": "Not found",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ErrorResponse"
                                    }
                                }
                            },
                        },
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "ScoreFeature": {
                    "type": "object",
                    "required": [
                        "feature",
                        "raw_value",
                        "weight",
                        "contribution",
                        "explanation",
                    ],
                    "properties": {
                        "feature": {"type": "string"},
                        "raw_value": {"type": "number"},
                        "weight": {"type": "number"},
                        "contribution": {"type": "number"},
                        "explanation": {"type": "string"},
                    },
                },
                "ScoredMessage": {
                    "type": "object",
                    "required": [
                        "id",
                        "sender_id",
                        "from_email_norm",
                        "anomaly_score",
                        "tier",
                        "tier_label",
                        "features",
                        "account_id",
                        "sender_msg_count",
                    ],
                    "properties": {
                        "id": {"type": "integer"},
                        "message_id": {"type": "string", "nullable": True},
                        "sender_id": {"type": "integer"},
                        "from_email_norm": {"type": "string", "format": "email"},
                        "from_display_name": {"type": "string", "nullable": True},
                        "sent_at": {
                            "type": "integer",
                            "nullable": True,
                            "description": "Unix seconds",
                        },
                        "n_attachments": {"type": "integer"},
                        "n_links": {"type": "integer"},
                        "size_bytes": {"type": "integer", "nullable": True},
                        "anomaly_score": {"type": "number"},
                        "tier": {"type": "integer", "enum": [0, 1, 2, 3]},
                        "trust_tier_at_scoring": {
                            "type": "integer",
                            "enum": [0, 1, 2, 3],
                        },
                        "baseline_n": {"type": "integer"},
                        "engine_version": {"type": "string"},
                        "tier_label": {
                            "type": "string",
                            "enum": [
                                "never seen",
                                "sender new, organization known",
                                "prior one-way contact",
                                "established",
                            ],
                        },
                        "features": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ScoreFeature"},
                        },
                        "account_id": {"type": "integer"},
                        "sender_msg_count": {"type": "integer"},
                    },
                },
                "AccountStatusRow": {
                    "type": "object",
                    "required": ["id", "email", "auth_method", "skip_count"],
                    "properties": {
                        "id": {"type": "integer"},
                        "email": {"type": "string", "format": "email"},
                        "auth_method": {
                            "type": "string",
                            "enum": ["oauth2", "app_password", "mail_store"],
                        },
                        "last_error": {"type": "string", "nullable": True},
                        "last_synced_at": {"type": "integer", "nullable": True},
                        "skip_count": {"type": "integer"},
                    },
                },
                "StatusResponse": {
                    "type": "object",
                    "required": [
                        "messages",
                        "incoming",
                        "unique_incoming",
                        "senders",
                        "accounts",
                        "total_skipped",
                        "has_errors",
                    ],
                    "properties": {
                        "messages": {"type": "integer"},
                        "incoming": {"type": "integer"},
                        "unique_incoming": {"type": "integer"},
                        "senders": {"type": "integer"},
                        "encryption": {
                            "type": "string",
                            "nullable": True,
                            "enum": ["sqlcipher", "none"],
                        },
                        "accounts": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/AccountStatusRow"},
                        },
                        "total_skipped": {"type": "integer"},
                        "has_errors": {"type": "boolean"},
                        "psl_version": {"type": "string"},
                        "psl_stored_version": {"type": "string"},
                        "psl_drift": {"type": "boolean"},
                        "server_deleted_count": {"type": "integer"},
                        "server_deleted_by_folder": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["folder", "count"],
                                "properties": {
                                    "folder": {"type": "string"},
                                    "count": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
                "SkippedRow": {
                    "type": "object",
                    "required": [
                        "id",
                        "account_id",
                        "uid",
                        "reason",
                        "skipped_at",
                        "folder_name",
                        "account_email",
                    ],
                    "properties": {
                        "id": {"type": "integer"},
                        "account_id": {"type": "integer"},
                        "uid": {"type": "integer"},
                        "reason": {"type": "string"},
                        "skipped_at": {"type": "integer"},
                        "folder_name": {"type": "string"},
                        "account_email": {"type": "string", "format": "email"},
                    },
                },
                "AccountRow": {
                    "type": "object",
                    "required": ["id", "email"],
                    "properties": {
                        "id": {"type": "integer"},
                        "email": {"type": "string", "format": "email"},
                    },
                },
                "SenderDetailResponse": {
                    "type": "object",
                    "required": [
                        "id",
                        "email_norm",
                        "domain",
                        "reg_domain",
                        "trust_tier",
                        "tier_label",
                        "tier_rule",
                    ],
                    "properties": {
                        "id": {"type": "integer"},
                        "email_norm": {"type": "string", "format": "email"},
                        "display_name": {"type": "string", "nullable": True},
                        "domain": {"type": "string"},
                        "reg_domain": {"type": "string"},
                        "is_freemail": {"type": "boolean"},
                        "trust_tier": {"type": "integer", "enum": [0, 1, 2, 3]},
                        "tier_label": {"type": "string"},
                        "tier_rule": {"type": "string"},
                        "hour_histogram": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 24,
                            "maxItems": 24,
                        },
                        "recent_messages": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ScoredMessage"},
                        },
                    },
                },
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error", "code"],
                    "properties": {
                        "error": {"type": "string"},
                        "code": {"type": "string"},
                        "details": {"type": "string"},
                    },
                },
                "Pagination": {
                    "type": "object",
                    "required": ["limit", "returned"],
                    "properties": {
                        "limit": {"type": "integer"},
                        "returned": {"type": "integer"},
                        "total": {"type": "integer"},
                    },
                },
                "PaginatedMessages": {
                    "type": "object",
                    "required": ["data", "pagination"],
                    "properties": {
                        "data": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ScoredMessage"},
                        },
                        "pagination": {"$ref": "#/components/schemas/Pagination"},
                    },
                },
                "PaginatedSkipped": {
                    "type": "object",
                    "required": ["data", "pagination"],
                    "properties": {
                        "data": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/SkippedRow"},
                        },
                        "pagination": {"$ref": "#/components/schemas/Pagination"},
                    },
                },
                "PaginatedAccounts": {
                    "type": "object",
                    "required": ["data", "pagination"],
                    "properties": {
                        "data": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/AccountRow"},
                        },
                        "pagination": {"$ref": "#/components/schemas/Pagination"},
                    },
                },
            }
        },
    }
