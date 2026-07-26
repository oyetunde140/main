from __future__ import annotations

import base64
import binascii
import hashlib
import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Max page size for GET /api/proofs cursor pagination.
PROOF_EVENTS_MAX_LIMIT = 100
PROOF_EVENTS_DEFAULT_LIMIT = 25


def encode_proof_events_cursor(event_id: int) -> str:
    """Return a stable opaque cursor for the given proof_events.id."""
    return base64.urlsafe_b64encode(str(event_id).encode("ascii")).decode("ascii")


def decode_proof_events_cursor(cursor: str) -> int:
    """Decode an opaque proof-events cursor.

    Raises ValueError when the cursor is malformed.
    """
    if not isinstance(cursor, str) or not cursor.strip():
        raise ValueError("invalid cursor")

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        text = raw.decode("ascii")
        event_id = int(text)
    except (ValueError, UnicodeDecodeError, binascii.Error, TypeError) as exc:
        raise ValueError("invalid cursor") from exc

    if event_id < 1:
        raise ValueError("invalid cursor")
    return event_id


def clamp_proof_events_limit(limit: int) -> int:
    return max(1, min(limit, PROOF_EVENTS_MAX_LIMIT))


def database_url() -> str | None:
    return os.getenv("DATABASE_URL")


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")

    with psycopg.connect(url, row_factory=dict_row) as connection:
        yield connection


def init_db() -> None:
    if not database_url():
        return

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists proof_events (
                    id bigserial primary key,
                    event_type text not null,
                    file_name text,
                    video_hash text,
                    metadata_hash text,
                    proof_id text,
                    tier text,
                    embedded_hash text,
                    metadata jsonb,
                    created_at timestamptz not null default now()
                );
                """
            )
            cursor.execute(
                """
                create index if not exists proof_events_video_hash_idx
                on proof_events (video_hash);
                """
            )
            cursor.execute(
                """
                create index if not exists proof_events_proof_id_idx
                on proof_events (proof_id);
                """
            )
            cursor.execute("alter table proof_events add column if not exists tx_hash text;")
            cursor.execute("alter table proof_events add column if not exists tx_status text;")
            cursor.execute("alter table proof_events add column if not exists source_address text;")
            cursor.execute("alter table proof_events add column if not exists contract_id text;")
            cursor.execute(
                """
                create index if not exists proof_events_tx_hash_idx
                on proof_events (tx_hash);
                """
            )
            # Idempotency support: unique key for register events only.
            cursor.execute("alter table proof_events add column if not exists idempotency_key text;")
            # Partial unique index: only enforced when idempotency_key is non-null
            # (i.e., only for 'register' events). embed/extract events are unaffected.
            cursor.execute(
                """
                create unique index if not exists proof_events_idempotency_key_idx
                on proof_events (idempotency_key)
                where idempotency_key is not null;
                """
            )
            cursor.execute(
                """
                create table if not exists webhook_subscriptions (
                    id bigserial primary key,
                    url text not null,
                    secret_key text not null,
                    is_active boolean not null default true,
                    created_at timestamptz not null default now()
                );
                """
            )
            cursor.execute(
                """
                create table if not exists proof_history_events (
                    id bigserial primary key,
                    proof_id text not null,
                    action text not null,
                    actor text,
                    reason_code integer not null,
                    contract_id text,
                    tx_hash text,
                    tx_status text,
                    created_at timestamptz not null default now()
                );
                """
            )
            cursor.execute(
                """
                create table if not exists webhook_deliveries (
                    id bigserial primary key,
                    subscription_id bigint not null references webhook_subscriptions(id) on delete cascade,
                    event_id bigint not null references proof_events(id) on delete cascade,
                    status text not null default 'pending',
                    retry_count integer not null default 0,
                    next_retry_at timestamptz not null default now(),
                    lease_expires_at timestamptz,
                    last_response_code integer,
                    created_at timestamptz not null default now(),
                    updated_at timestamptz not null default now()
                );
                """
            )
            cursor.execute(
                """
                create index if not exists webhook_deliveries_status_idx
                on webhook_deliveries (status, next_retry_at);
                """
            )
            cursor.execute(
                """
                create index if not exists proof_history_events_proof_id_idx
                on proof_history_events (proof_id);
                """
            )
        connection.commit()


def check_db() -> bool:
    if not database_url():
        return False

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("select 1 as ok")
            row = cursor.fetchone()
            return bool(row and row["ok"] == 1)


def insert_proof_event(
    *,
    event_type: str,
    file_name: str | None = None,
    video_hash: str | None = None,
    metadata_hash: str | None = None,
    proof_id: str | None = None,
    tier: str | None = None,
    embedded_hash: str | None = None,
    tx_hash: str | None = None,
    tx_status: str | None = None,
    source_address: str | None = None,
    contract_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not database_url():
        return None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into proof_events (
                    event_type,
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    embedded_hash,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id, created_at;
                """,
                (
                    event_type,
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    embedded_hash,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    Jsonb(metadata) if metadata is not None else None,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
        return dict(row) if row else None


def list_proof_events(
    limit: int = PROOF_EVENTS_DEFAULT_LIMIT,
    *,
    cursor_id: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List proof events with stable keyset (cursor) pagination.

    Ordering is deterministic: ``id DESC`` (primary key is the unique tie-breaker).
    Returns ``(events, next_cursor)`` where ``next_cursor`` is an opaque token for
    the next page, or ``None`` when there are no further rows.
    """
    if not database_url():
        return [], None

    page_size = clamp_proof_events_limit(limit)
    fetch_size = page_size + 1

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if cursor_id is None:
                cursor.execute(
                    """
                    select
                        id,
                        event_type,
                        file_name,
                        video_hash,
                        metadata_hash,
                        proof_id,
                        tier,
                        embedded_hash,
                        tx_hash,
                        tx_status,
                        source_address,
                        contract_id,
                        metadata,
                        created_at
                    from proof_events
                    order by id desc
                    limit %s;
                    """,
                    (fetch_size,),
                )
            else:
                cursor.execute(
                    """
                    select
                        id,
                        event_type,
                        file_name,
                        video_hash,
                        metadata_hash,
                        proof_id,
                        tier,
                        embedded_hash,
                        tx_hash,
                        tx_status,
                        source_address,
                        contract_id,
                        metadata,
                        created_at
                    from proof_events
                    where id < %s
                    order by id desc
                    limit %s;
                    """,
                    (cursor_id, fetch_size),
                )
            rows = [dict(row) for row in cursor.fetchall()]

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        next_cursor = encode_proof_events_cursor(int(rows[-1]["id"]))
    return rows, next_cursor


def find_proof_events_by_video(video_hash: str) -> list[dict[str, Any]]:
    if not database_url():
        return []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select
                    id,
                    event_type,
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    embedded_hash,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    metadata,
                    created_at
                from proof_events
                where video_hash = %s
                order by id desc;
                """,
                (video_hash,),
            )
            return [dict(row) for row in cursor.fetchall()]


def make_idempotency_key(video_hash: str, proof_id: str, tx_hash: str | None) -> str:
    """Derive the idempotency key for a register event.

    Key material: ``video_hash:proof_id:tx_hash`` where ``tx_hash`` defaults
    to the empty string when absent.  Using SHA-256 keeps the stored value a
    fixed-length hex string and avoids any length-extension ambiguity.
    """
    raw = f"{video_hash}:{proof_id}:{tx_hash or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


# Fields compared when deciding whether a retry carries a conflicting payload.
_CONFLICT_FIELDS = ("video_hash", "metadata_hash", "proof_id", "tier", "source_address", "contract_id")


def upsert_register_event(
    *,
    idempotency_key: str,
    file_name: str | None = None,
    video_hash: str | None = None,
    metadata_hash: str | None = None,
    proof_id: str | None = None,
    tier: str | None = None,
    tx_hash: str | None = None,
    tx_status: str | None = None,
    source_address: str | None = None,
    contract_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert a register event idempotently.

    Returns ``(row, created)`` where *created* is ``True`` when a new row was
    written and ``False`` when an existing row was found via the idempotency
    key.

    Raises ``ConflictError`` when the same idempotency key is reused with a
    payload that differs in one of the canonical proof-identity fields.

    The insert is done with ``ON CONFLICT DO NOTHING`` so that concurrent
    requests with the same key race safely: only one writer wins and the
    other falls back to the ``SELECT`` path.
    """
    if not database_url():
        # When no database is configured return a stub so the rest of the app
        # remains functional in minimal dev environments.
        stub: dict[str, Any] = {
            "id": None,
            "created_at": None,
            "video_hash": video_hash,
            "metadata_hash": metadata_hash,
            "proof_id": proof_id,
            "tier": tier,
            "source_address": source_address,
            "contract_id": contract_id,
        }
        return stub, True

    with get_connection() as connection:
        with connection.cursor() as cursor:
            # Attempt the insert; if the unique key already exists the row is
            # silently skipped and nothing is returned.
            cursor.execute(
                """
                insert into proof_events (
                    event_type,
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    metadata,
                    idempotency_key
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (idempotency_key)
                where idempotency_key is not null
                do nothing
                returning
                    id,
                    event_type,
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    embedded_hash,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    metadata,
                    created_at;
                """,
                (
                    "register",
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    Jsonb(metadata) if metadata is not None else None,
                    idempotency_key,
                ),
            )
            row = cursor.fetchone()

            if row is not None:
                # Fresh insert – committed below.
                connection.commit()
                return dict(row), True

            # Key already existed (concurrent insert or retry): fetch the
            # previously stored row.
            cursor.execute(
                """
                select
                    id,
                    event_type,
                    file_name,
                    video_hash,
                    metadata_hash,
                    proof_id,
                    tier,
                    embedded_hash,
                    tx_hash,
                    tx_status,
                    source_address,
                    contract_id,
                    metadata,
                    created_at
                from proof_events
                where idempotency_key = %s;
                """,
                (idempotency_key,),
            )
            existing = cursor.fetchone()

        # No commit needed for the read-only fallback path; any uncommitted
        # state is just the no-op insert.
        connection.rollback()

    if existing is None:
        # Should not happen – the constraint guarantees the row is visible.
        raise RuntimeError("idempotency key collision but row not found; please retry")

    existing_row = dict(existing)

    # Check whether the caller is re-submitting with a conflicting payload.
    incoming = {
        "video_hash": video_hash,
        "metadata_hash": metadata_hash,
        "proof_id": proof_id,
        "tier": tier,
        "source_address": source_address,
        "contract_id": contract_id,
    }
    for field in _CONFLICT_FIELDS:
        if existing_row.get(field) != incoming.get(field):
            raise ConflictError(
                idempotency_key=idempotency_key,
                field=field,
                existing_value=existing_row.get(field),
                incoming_value=incoming.get(field),
            )

    return existing_row, False


class ConflictError(Exception):
    """Raised when the same idempotency key is reused with a conflicting payload."""

    def __init__(
        self,
        *,
        idempotency_key: str,
        field: str,
        existing_value: object,
        incoming_value: object,
    ) -> None:
        super().__init__(
            f"idempotency key reused with conflicting value for '{field}'"
        )
        self.idempotency_key = idempotency_key
        self.field = field
        self.existing_value = existing_value
        self.incoming_value = incoming_value


def insert_proof_history_event(
    *,
    proof_id: str,
    action: str,
    actor: str | None = None,
    reason_code: int,
    contract_id: str | None = None,
    tx_hash: str | None = None,
    tx_status: str | None = None,
) -> dict[str, Any] | None:
    if not database_url():
        return None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into proof_history_events (
                    proof_id,
                    action,
                    actor,
                    reason_code,
                    contract_id,
                    tx_hash,
                    tx_status
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                returning id, created_at;
                """,
                (
                    proof_id,
                    action,
                    actor,
                    reason_code,
                    contract_id,
                    tx_hash,
                    tx_status,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
        return dict(row) if row else None


def list_proof_history_events(
    proof_id: str, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    if not database_url():
        return []

    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select
                    id,
                    proof_id,
                    action,
                    actor,
                    reason_code,
                    contract_id,
                    tx_hash,
                    tx_status,
                    created_at
                from proof_history_events
                where proof_id = %s
                order by id asc
                limit %s offset %s;
                """,
                (proof_id, limit, offset),
            )
            return [dict(row) for row in cursor.fetchall()]
