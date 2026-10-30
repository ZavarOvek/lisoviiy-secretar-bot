"""Коротка пам'ять розмови: попереднє питання звіра для уточнень місця.

Живе в пам'яті процесу: після рестарту бот просто не пам'ятає попереднього питання.
"""

import time
from collections.abc import Callable

from lisovyi.search.places import PlaceResolver
from lisovyi.search.retrieve import is_place_followup, strip_places


class DialogueMemory:
    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        # chat_id → (базове питання, час останньої репліки).
        self._turns: dict[int, tuple[str, float]] = {}

    def merge(self, chat_id: int, message: str, resolver: PlaceResolver) -> str:
        """Питання для пошуку з урахуванням попередньої репліки.

        Уточнення місця («А біля дубів?») в межах TTL дає базове питання без його місця плюс
        нове місце. Базове питання при цьому не змінюється, тож друге уточнення поспіль
        знову спирається на нього. Будь-яке інше повідомлення стає новим базовим питанням.
        """
        now = self._clock()
        self._forget_expired(now)
        saved = self._turns.get(chat_id)
        if saved is not None and is_place_followup(message, resolver):
            base = saved[0]
            self._turns[chat_id] = (base, now)
            return f"{strip_places(base, resolver)} {message}".strip()
        self._turns[chat_id] = (message, now)
        return message

    def _forget_expired(self, now: float) -> None:
        expired = [chat for chat, (_, at) in self._turns.items() if now - at > self.ttl]
        for chat in expired:
            del self._turns[chat]
