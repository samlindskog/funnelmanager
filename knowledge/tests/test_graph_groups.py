"""Per-user session-graph namespacing: distinct usernames must never collide."""

from app.graph import user_group_id


def test_distinct_usernames_get_distinct_groups_despite_slug_collision():
    # These all slug-normalize to 'sam-doe'; the digest must keep them apart.
    groups = {user_group_id(u) for u in ("sam.doe", "sam_doe", "Sam Doe", "sam!doe")}
    assert len(groups) == 4


def test_group_id_is_stable_per_username():
    assert user_group_id("sam") == user_group_id("sam")


def test_group_id_handles_empty_and_symbol_only_usernames():
    a = user_group_id("")
    b = user_group_id("!!!")
    assert a != b
    assert a.startswith("user-") and b.startswith("user-")


def test_case_variants_are_distinct_users():
    assert user_group_id("Sam") != user_group_id("sam")
