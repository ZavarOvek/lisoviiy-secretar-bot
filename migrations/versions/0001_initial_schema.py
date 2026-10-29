"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-29 23:06:45.247403
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_NAMES = ("document_kind", "text_source", "document_status")


def upgrade() -> None:
    op.create_table(
        "beasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_beasts")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_beasts_telegram_id")),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("glove", "part", "general", "map", name="document_kind"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "text_source",
            sa.Enum("text", "transcript", "ocr", name="text_source"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("ocr_confirmed", sa.Boolean(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("active", "replaced", "deleted", name="document_status"),
            server_default="active",
            nullable=False,
        ),
        sa.Column("replaces_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"], ["beasts.id"], name=op.f("fk_documents_author_id_beasts")
        ),
        sa.ForeignKeyConstraint(
            ["replaces_id"], ["documents.id"], name=op.f("fk_documents_replaces_id_documents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_sha256"), "documents", ["sha256"], unique=False)
    # Для одного місця й теми активна тільки одна версія документа.
    op.create_index(
        "uq_documents_active_scope_topic",
        "documents",
        ["scope", "topic"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "forest_map",
        sa.Column("glove", sa.Text(), nullable=False),
        sa.Column(
            "parts", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("map_document_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["map_document_id"],
            ["documents.id"],
            name=op.f("fk_forest_map_map_document_id_documents"),
        ),
        sa.PrimaryKeyConstraint("glove", name=op.f("pk_forest_map")),
    )


def downgrade() -> None:
    op.drop_table("forest_map")
    op.drop_index(
        "uq_documents_active_scope_topic",
        table_name="documents",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(op.f("ix_documents_sha256"), table_name="documents")
    op.drop_table("documents")
    op.drop_table("beasts")
    for name in ENUM_NAMES:
        sa.Enum(name=name).drop(op.get_bind())
