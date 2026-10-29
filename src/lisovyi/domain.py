"""Словник предметної області, спільний для БД, імпорту й бота."""

from enum import StrEnum


class DocumentKind(StrEnum):
    GLOVE = "glove"
    PART = "part"
    GENERAL = "general"
    MAP = "map"


class TextSource(StrEnum):
    TEXT = "text"
    TRANSCRIPT = "transcript"
    OCR = "ocr"


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    REPLACED = "replaced"
    DELETED = "deleted"


class ForestPart(StrEnum):
    """Частини лісу фіксовані в коді: вони задають словник області дії.

    Які рукавички до яких частин належать, задає документ «Карта лісу», не код.
    """

    KHVOINYI = "khvoinyi"
    LYSTIANYI = "lystianyi"
    RICHKA = "richka"


GENERAL_SCOPE = "general"
