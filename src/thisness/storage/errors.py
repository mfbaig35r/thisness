"""Errors raised by stores. A rejected commit writes nothing."""

from __future__ import annotations


class ThisnessError(Exception):
    pass


class NotFound(ThisnessError):
    pass


class InvalidOperation(ThisnessError):
    """An operation is inconsistent with current identity state."""

    def __init__(self, op_index: int, message: str) -> None:
        super().__init__(f"op {op_index}: {message}")
        self.op_index = op_index


class UnknownDecision(ThisnessError):
    """A change cites a decision that was never recorded."""

    def __init__(self, decision_ids: list[str]) -> None:
        super().__init__(f"unrecorded decisions: {decision_ids}")
        self.decision_ids = decision_ids


class StaleVersion(ThisnessError):
    """An operator change was built against an entity version that has since moved."""

    def __init__(self, entity_id: str, expected: int, actual: int) -> None:
        super().__init__(f"{entity_id}: expected version {expected}, found {actual}")
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual


class CommitConflict(ThisnessError):
    """Something a run read changed after its base (design 01 §8). Rebase and retry."""

    def __init__(self, base_seq: int, entity_ids: list[str], blocking_keys: list[str]) -> None:
        super().__init__(
            f"changed since seq {base_seq}: entities {entity_ids}, blocking keys {blocking_keys}"
        )
        self.base_seq = base_seq
        self.entity_ids = entity_ids
        self.blocking_keys = blocking_keys
