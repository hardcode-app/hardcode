"""
A stand-in for the machine's name lookups, for the tests of what the CLI
does with names that resolve to an address on your network.
"""

from __future__ import annotations

from typing import Optional


class FakeResolver:
    """
    Name lookups from a table: what each name resolves to (nothing for a
    name it doesn't have), and which names were asked, in order.
    """

    def __init__(self, answers: Optional[dict[str, list[str]]] = None):
        self.answers = dict(answers or {})
        self.asked: list[str] = []

    def __call__(self, name: str) -> list[str]:
        self.asked.append(name)
        return list(self.answers.get(name, []))
