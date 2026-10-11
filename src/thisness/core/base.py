from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """Base for every core record: immutable after construction, unknown fields rejected.

    Sequence fields are tuples so a record cannot be changed through a shared list.
    """

    # protected_namespaces=(): fields such as Score.model_ref are domain names, and
    # pydantic < 2.10 warns about any field starting with "model_".
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())
