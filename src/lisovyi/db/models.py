from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from lisovyi.domain import DocumentKind, DocumentStatus, TextSource

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=lambda members: [m.value for m in members])


class Beast(Base):
    __tablename__ = "beasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Для одного місця й теми активна тільки одна версія документа.
        Index(
            "uq_documents_active_scope_topic",
            "scope",
            "topic",
            unique=True,
            postgresql_where=sql_text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[DocumentKind] = mapped_column(_pg_enum(DocumentKind, "document_kind"))
    scope: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    text_source: Mapped[TextSource] = mapped_column(_pg_enum(TextSource, "text_source"))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # NULL: документ не з OCR. False/True виставляє діалог перепитування чисел.
    ocr_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    # NULL: документ з архіву.
    author_id: Mapped[int | None] = mapped_column(ForeignKey("beasts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[DocumentStatus] = mapped_column(
        _pg_enum(DocumentStatus, "document_status"),
        default=DocumentStatus.ACTIVE,
        server_default=DocumentStatus.ACTIVE.value,
    )
    replaces_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))


class ForestMapEntry(Base):
    """Рядок легенди активної «Карти лісу»: рукавичка і частини лісу, до яких вона належить."""

    __tablename__ = "forest_map"

    glove: Mapped[str] = mapped_column(Text, primary_key=True)
    parts: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sql_text("'{}'"))
    map_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
