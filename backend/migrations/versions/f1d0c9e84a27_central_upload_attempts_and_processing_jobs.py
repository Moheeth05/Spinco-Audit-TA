"""central storage: upload_attempts and processing_jobs tables

Revision ID: f1d0c9e84a27
Revises: c3a9f0b17d42
Create Date: 2026-11-06

Adds two NEW tables (nothing existing is altered, rewritten or dropped):
  * upload_attempts   one row per upload attempt (SUCCESS / DUPLICATE / FAILED / DEFERRED) with the authoritative UTC upload time, uploader, size, checksum and
                      the canonical-document / duplicate relationship;
  * processing_jobs   durable background work (EXTRACT, SHADOW_COPY) with attempts, schedule and a lease.

IDEMPOTENT: a database whose application already created the tables with create_all() (every database the current code has started on) has them in their
final shape -> nothing is done. If a table already exists with a different set of columns the migration REFUSES (fail closed) instead of guessing.
The downgrade removes nothing: these tables hold history that must never be lost by a downgrade (the revision cannot know whether it created them).
Offline (--sql) mode is not supported (the decision depends on what the database contains).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "f1d0c9e84a27"
down_revision: Union[str, None] = "c3a9f0b17d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPLOAD_ATTEMPT_COLUMNS = {
    "id", "uploaded_at_utc", "original_filename", "uploaded_by_id", "file_size", "sha256", "document_type", "client_id", "result", "error_message",
    "canonical_kind", "canonical_pk", "duplicate_reason", "storage_record_id", "retained_copy",
}
_PROCESSING_JOB_COLUMNS = {
    "id", "document_kind", "document_pk", "job_type", "state", "attempts", "failure_class", "last_error", "next_attempt_at", "lease_owner",
    "lease_expires_at", "created_at", "updated_at",
}


def _create_upload_attempts() -> None:
    op.create_table(
        "upload_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uploaded_at_utc", sa.DateTime(), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("client_id", sa.String(length=100), nullable=True),
        sa.Column("result", sa.String(length=12), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("canonical_kind", sa.String(length=10), nullable=True),
        sa.Column("canonical_pk", sa.Integer(), nullable=True),
        sa.Column("duplicate_reason", sa.String(length=60), nullable=True),
        sa.Column("storage_record_id", sa.Integer(), nullable=True),
        sa.Column("retained_copy", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["storage_record_id"], ["document_storage.id"]),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_attempts_canonical_pk", "upload_attempts", ["canonical_pk"])
    op.create_index("ix_upload_attempts_result", "upload_attempts", ["result"])
    op.create_index("ix_upload_attempts_sha256", "upload_attempts", ["sha256"])
    op.create_index("ix_upload_attempts_storage_record_id", "upload_attempts", ["storage_record_id"])
    op.create_index("ix_upload_attempts_uploaded_at_utc", "upload_attempts", ["uploaded_at_utc"])
    op.create_index("ix_upload_attempts_uploaded_by_id", "upload_attempts", ["uploaded_by_id"])


def _create_processing_jobs() -> None:
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_kind", sa.String(length=10), nullable=False),
        sa.Column("document_pk", sa.Integer(), nullable=False),
        sa.Column("job_type", sa.String(length=12), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("failure_class", sa.String(length=20), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(length=40), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_kind", "document_pk", "job_type", name="uq_processing_jobs_document_type"),
    )
    op.create_index("ix_processing_jobs_state", "processing_jobs", ["state"])


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Revision f1d0c9e84a27 cannot be rendered offline (--sql): it decides what to do from what the database really contains.")
    inspector = sa.inspect(op.get_bind())
    for name, columns, create in (
        ("upload_attempts", _UPLOAD_ATTEMPT_COLUMNS, _create_upload_attempts), ("processing_jobs", _PROCESSING_JOB_COLUMNS, _create_processing_jobs),
    ):
        if not inspector.has_table(name):
            if name == "upload_attempts" and not (inspector.has_table("users") and inspector.has_table("document_storage")):
                raise RuntimeError("Refusing to create upload_attempts: the tables it refers to (users, document_storage) do not exist. Apply revision c3a9f0b17d42 first.")
            create()
            continue
        have = {c["name"] for c in inspector.get_columns(name)}
        if have != columns:
            raise RuntimeError(f"Refusing to migrate: table {name} exists with an unexpected set of columns (missing: {sorted(columns - have)}; extra: {sorted(have - columns)}).")


def downgrade() -> None:
    """Deliberately removes nothing: upload history and processing state are records, and this revision cannot know whether it created the tables."""
    return None
