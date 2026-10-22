"""baseline: empty starting point for the multi-user schema

Revision ID: 0001
Revises:
Create Date: 2026-10-22

No DDL on purpose: this revision exists so the chain, the env wiring, and
the test tooling are proven before B2 adds the first tables (users,
sessions). Every later revision must be reviewable DDL — no runtime
auto-create anywhere (docs/backend-design.md §A3).
"""
from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
