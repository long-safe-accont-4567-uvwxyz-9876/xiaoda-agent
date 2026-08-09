import contextlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite


@dataclass(frozen=True, slots=True)
class ProfileField:
    id: int
    user_id: str
    agent_id: str
    namespace: str
    field_key: str
    value: Any
    value_type: str
    valid_from: float
    valid_to: float | None
    learned_at: float
    expired_at: float | None
    superseded_by: int | None
    source_type: str
    source_id: str


class ProfileStore:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        write_transaction: Callable[[], contextlib.AbstractAsyncContextManager],
    ) -> None:
        self._conn = conn
        self._write_transaction = write_transaction

    @staticmethod
    def _validate_identity(value: str, name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} must not be empty")
        return normalized

    @staticmethod
    def _decode(row: aiosqlite.Row | None) -> ProfileField | None:
        if row is None:
            return None
        return ProfileField(
            id=row["id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            namespace=row["namespace"],
            field_key=row["field_key"],
            value=json.loads(row["value_json"]),
            value_type=row["value_type"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            learned_at=row["learned_at"],
            expired_at=row["expired_at"],
            superseded_by=row["superseded_by"],
            source_type=row["source_type"],
            source_id=row["source_id"],
        )

    @staticmethod
    def _validate_time(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        return float(value)

    @staticmethod
    def _encode_value(value: Any) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as e:
            raise ValueError("profile value must be strict JSON") from e

    def _validate_scope(
        self,
        user_id: str,
        agent_id: str,
        namespace: str,
        field_key: str,
    ) -> tuple[str, str, str, str]:
        return (
            self._validate_identity(user_id, "user_id"),
            self._validate_identity(agent_id, "agent_id"),
            self._validate_identity(namespace, "namespace"),
            self._validate_identity(field_key, "field_key"),
        )

    async def put(
        self,
        *,
        user_id: str,
        agent_id: str,
        namespace: str,
        field_key: str,
        value: Any,
        value_type: str,
        source_type: str,
        source_id: str,
        effective_at: float | None = None,
        known_at: float | None = None,
    ) -> ProfileField:
        user_id = self._validate_identity(user_id, "user_id")
        agent_id = self._validate_identity(agent_id, "agent_id")
        namespace = self._validate_identity(namespace, "namespace")
        field_key = self._validate_identity(field_key, "field_key")
        value_type = self._validate_identity(value_type, "value_type")
        source_type = self._validate_identity(source_type, "source_type")
        source_id = self._validate_identity(source_id, "source_id")
        known_at = time.time() if known_at is None else known_at
        effective_at = known_at if effective_at is None else effective_at
        known_at = self._validate_time(known_at, "known_at")
        effective_at = self._validate_time(effective_at, "effective_at")
        value_json = self._encode_value(value)
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """SELECT * FROM profile_fields
                   WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                     AND expired_at IS NULL
                   ORDER BY learned_at DESC, id DESC LIMIT 1""",
                (user_id, agent_id, namespace, field_key),
            )
            old = await cursor.fetchone()
            if old is not None and known_at < old["learned_at"]:
                raise ValueError("known_at is earlier than current learned_at")
            if old is not None and effective_at < old["valid_from"]:
                raise ValueError("effective_at is earlier than current valid_from")
            carry_id = None
            if old is not None and effective_at > old["valid_from"]:
                carry = await conn.execute(
                    """INSERT INTO profile_fields
                       (user_id, agent_id, namespace, field_key, value_json, value_type,
                        valid_from, valid_to, learned_at, source_type, source_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id, agent_id, namespace, field_key, old["value_json"],
                        old["value_type"], old["valid_from"], effective_at, known_at,
                        old["source_type"], old["source_id"], known_at, known_at,
                    ),
                )
                carry_id = carry.lastrowid
            insert = await conn.execute(
                """INSERT INTO profile_fields
                   (user_id, agent_id, namespace, field_key, value_json, value_type,
                    valid_from, learned_at, source_type, source_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    agent_id,
                    namespace,
                    field_key,
                    value_json,
                    value_type,
                    effective_at,
                    known_at,
                    source_type,
                    source_id,
                    known_at,
                    known_at,
                ),
            )
            new_id = insert.lastrowid
            if old is not None:
                await conn.execute(
                    """UPDATE profile_fields
                       SET expired_at=?, superseded_by=?, updated_at=?
                       WHERE id=?""",
                    (known_at, carry_id or new_id, known_at, old["id"]),
                )
            cursor = await conn.execute("SELECT * FROM profile_fields WHERE id=?", (new_id,))
            created = self._decode(await cursor.fetchone())
        if created is None:
            raise RuntimeError("profile field insert did not return a record")
        return created

    async def put_with_event(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        namespace: str,
        field_key: str,
        value: Any,
        value_type: str,
        confidence: float,
        source_type: str,
        source_id: str,
        effective_at: float | None = None,
        known_at: float | None = None,
    ) -> ProfileField:
        user_id = self._validate_identity(user_id, "user_id")
        agent_id = self._validate_identity(agent_id, "agent_id")
        namespace = self._validate_identity(namespace, "namespace")
        field_key = self._validate_identity(field_key, "field_key")
        known_at = time.time() if known_at is None else known_at
        effective_at = known_at if effective_at is None else effective_at
        known_at = self._validate_time(known_at, "known_at")
        effective_at = self._validate_time(effective_at, "effective_at")
        value_json = self._encode_value(value)
        async with self._write_transaction() as conn:
            duplicate_cursor = await conn.execute(
                """SELECT candidate_json, field_id FROM profile_events
                   WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                     AND source_type=? AND source_id=? AND status='accepted'
                   LIMIT 1""",
                (user_id, agent_id, namespace, field_key, source_type, source_id),
            )
            duplicate = await duplicate_cursor.fetchone()
            if duplicate is not None:
                if duplicate["candidate_json"] != value_json:
                    raise ValueError("source id was already used with a different profile value")
                cursor = await conn.execute(
                    "SELECT * FROM profile_fields WHERE id=?", (duplicate["field_id"],)
                )
                existing = self._decode(await cursor.fetchone())
                if existing is None:
                    raise RuntimeError("idempotent profile event references a missing field")
                return existing
            cursor = await conn.execute(
                """SELECT * FROM profile_fields
                   WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                     AND expired_at IS NULL
                   ORDER BY learned_at DESC, id DESC LIMIT 1""",
                (user_id, agent_id, namespace, field_key),
            )
            old = await cursor.fetchone()
            if old is not None and known_at < old["learned_at"]:
                raise ValueError("known_at is earlier than current learned_at")
            if old is not None and effective_at < old["valid_from"]:
                raise ValueError("effective_at is earlier than current valid_from")
            carry_id = None
            if old is not None and effective_at > old["valid_from"]:
                carry = await conn.execute(
                    """INSERT INTO profile_fields
                       (user_id, agent_id, namespace, field_key, value_json, value_type,
                        valid_from, valid_to, learned_at, source_type, source_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id, agent_id, namespace, field_key, old["value_json"],
                        old["value_type"], old["valid_from"], effective_at, known_at,
                        old["source_type"], old["source_id"], known_at, known_at,
                    ),
                )
                carry_id = carry.lastrowid
            insert = await conn.execute(
                """INSERT INTO profile_fields
                   (user_id, agent_id, namespace, field_key, value_json, value_type,
                    valid_from, learned_at, source_type, source_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, agent_id, namespace, field_key,
                    value_json,
                    value_type, effective_at, known_at, source_type, source_id,
                    known_at, known_at,
                ),
            )
            new_id = insert.lastrowid
            if old is not None:
                await conn.execute(
                    """UPDATE profile_fields
                       SET expired_at=?, superseded_by=?, updated_at=? WHERE id=?""",
                    (known_at, carry_id or new_id, known_at, old["id"]),
                )
            await conn.execute(
                """INSERT INTO profile_events
                   (user_id, agent_id, session_id, namespace, field_key,
                    candidate_json, confidence, status, reason, source_type,
                    source_id, field_id, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', 'accepted', ?, ?, ?, ?)""",
                (
                    user_id, agent_id, session_id, namespace, field_key,
                    value_json,
                    confidence, source_type, source_id, new_id, known_at,
                ),
            )
            cursor = await conn.execute("SELECT * FROM profile_fields WHERE id=?", (new_id,))
            created = self._decode(await cursor.fetchone())
        if created is None:
            raise RuntimeError("profile field insert did not return a record")
        return created

    async def get_current(
        self,
        *,
        user_id: str,
        agent_id: str,
        namespace: str,
        field_key: str,
    ) -> ProfileField | None:
        user_id, agent_id, namespace, field_key = self._validate_scope(
            user_id, agent_id, namespace, field_key
        )
        cursor = await self._conn.execute(
            """SELECT * FROM profile_fields
               WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                 AND valid_to IS NULL AND expired_at IS NULL
               ORDER BY learned_at DESC, id DESC LIMIT 1""",
            (user_id, agent_id, namespace, field_key),
        )
        return self._decode(await cursor.fetchone())

    async def get_as_of(
        self,
        valid_time: float,
        *,
        known_at: float | None = None,
        user_id: str,
        agent_id: str,
        namespace: str,
        field_key: str,
    ) -> ProfileField | None:
        known_at = time.time() if known_at is None else known_at
        valid_time = self._validate_time(valid_time, "valid_time")
        known_at = self._validate_time(known_at, "known_at")
        user_id, agent_id, namespace, field_key = self._validate_scope(
            user_id, agent_id, namespace, field_key
        )
        cursor = await self._conn.execute(
            """SELECT * FROM profile_fields
               WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                 AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
                 AND learned_at <= ? AND (expired_at IS NULL OR expired_at > ?)
               ORDER BY learned_at DESC, id DESC LIMIT 1""",
            (
                user_id,
                agent_id,
                namespace,
                field_key,
                valid_time,
                valid_time,
                known_at,
                known_at,
            ),
        )
        return self._decode(await cursor.fetchone())

    async def get_history(
        self,
        *,
        user_id: str,
        agent_id: str,
        namespace: str,
        field_key: str,
    ) -> list[ProfileField]:
        user_id, agent_id, namespace, field_key = self._validate_scope(
            user_id, agent_id, namespace, field_key
        )
        cursor = await self._conn.execute(
            """SELECT * FROM profile_fields
               WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
               ORDER BY learned_at DESC, id DESC""",
            (user_id, agent_id, namespace, field_key),
        )
        return [self._decode(row) for row in await cursor.fetchall()]

    async def get_current_many(
        self,
        *,
        user_id: str,
        agent_id: str,
        fields: list[tuple[str, str]],
    ) -> list[ProfileField]:
        user_id = self._validate_identity(user_id, "user_id")
        agent_id = self._validate_identity(agent_id, "agent_id")
        results = []
        for namespace, field_key in fields:
            field = await self.get_current(
                user_id=user_id,
                agent_id=agent_id,
                namespace=namespace,
                field_key=field_key,
            )
            if field is not None:
                results.append(field)
        return results

    async def forget(
        self,
        *,
        user_id: str,
        agent_id: str,
        namespace: str,
        field_key: str,
        forgotten_at: float | None = None,
    ) -> ProfileField | None:
        forgotten_at = time.time() if forgotten_at is None else forgotten_at
        forgotten_at = self._validate_time(forgotten_at, "forgotten_at")
        user_id, agent_id, namespace, field_key = self._validate_scope(
            user_id, agent_id, namespace, field_key
        )
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """SELECT * FROM profile_fields
                   WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                     AND valid_to IS NULL AND expired_at IS NULL
                   ORDER BY learned_at DESC, id DESC LIMIT 1""",
                (user_id, agent_id, namespace, field_key),
            )
            current = self._decode(await cursor.fetchone())
            if current is None:
                return None
            await conn.execute(
                """UPDATE profile_fields
                   SET expired_at=?, updated_at=?
                   WHERE id=? AND valid_to IS NULL AND expired_at IS NULL""",
                (forgotten_at, forgotten_at, current.id),
            )
        return current

    async def forget_with_event(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        namespace: str,
        field_key: str,
        source_type: str,
        source_id: str,
        forgotten_at: float | None = None,
    ) -> ProfileField | None:
        forgotten_at = time.time() if forgotten_at is None else forgotten_at
        forgotten_at = self._validate_time(forgotten_at, "forgotten_at")
        user_id, agent_id, namespace, field_key = self._validate_scope(
            user_id, agent_id, namespace, field_key
        )
        session_id = self._validate_identity(session_id, "session_id")
        source_type = self._validate_identity(source_type, "source_type")
        source_id = self._validate_identity(source_id, "source_id")
        async with self._write_transaction() as conn:
            cursor = await conn.execute(
                """SELECT * FROM profile_fields
                   WHERE user_id=? AND agent_id=? AND namespace=? AND field_key=?
                     AND valid_to IS NULL AND expired_at IS NULL
                   ORDER BY learned_at DESC, id DESC LIMIT 1""",
                (user_id, agent_id, namespace, field_key),
            )
            current = self._decode(await cursor.fetchone())
            if current is None:
                return None
            await conn.execute(
                """UPDATE profile_fields
                   SET expired_at=?, updated_at=?
                   WHERE id=? AND valid_to IS NULL AND expired_at IS NULL""",
                (forgotten_at, forgotten_at, current.id),
            )
            await conn.execute(
                """INSERT INTO profile_events
                   (user_id, agent_id, session_id, namespace, field_key,
                    candidate_json, confidence, status, reason, source_type,
                    source_id, field_id, recorded_at)
                   VALUES (?, ?, ?, ?, ?, 'null', 1.0, 'forgotten',
                           'explicit_forget', ?, ?, ?, ?)""",
                (
                    user_id, agent_id, session_id, namespace, field_key,
                    source_type, source_id, current.id, forgotten_at,
                ),
            )
        return current

    async def record_event(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        namespace: str,
        field_key: str,
        candidate_value: Any,
        confidence: float,
        status: str,
        reason: str,
        source_type: str,
        source_id: str,
        field_id: int | None,
        recorded_at: float,
    ) -> None:
        user_id, agent_id, namespace, field_key = self._validate_scope(
            user_id, agent_id, namespace, field_key
        )
        session_id = self._validate_identity(session_id, "session_id")
        status = self._validate_identity(status, "status")
        reason = self._validate_identity(reason, "reason")
        source_type = self._validate_identity(source_type, "source_type")
        source_id = self._validate_identity(source_id, "source_id")
        recorded_at = self._validate_time(recorded_at, "recorded_at")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")
        candidate_json = self._encode_value(candidate_value)
        async with self._write_transaction() as conn:
            await conn.execute(
                """INSERT INTO profile_events
                   (user_id, agent_id, session_id, namespace, field_key,
                    candidate_json, confidence, status, reason, source_type,
                    source_id, field_id, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    agent_id,
                    session_id,
                    namespace,
                    field_key,
                    candidate_json,
                    confidence,
                    status,
                    reason,
                    source_type,
                    source_id,
                    field_id,
                    recorded_at,
                ),
            )

    async def list_events(self, *, user_id: str, agent_id: str) -> list[dict[str, Any]]:
        user_id = self._validate_identity(user_id, "user_id")
        agent_id = self._validate_identity(agent_id, "agent_id")
        cursor = await self._conn.execute(
            """SELECT * FROM profile_events
               WHERE user_id=? AND agent_id=? ORDER BY id""",
            (user_id, agent_id),
        )
        return [dict(row) for row in await cursor.fetchall()]
