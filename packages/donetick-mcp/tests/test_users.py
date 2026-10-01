"""Integration tests for user and profile operations.

Run with ``DONETICK_INTEGRATION=1 pytest``.
"""

from __future__ import annotations

from conftest import requires_integration
from donetick_mcp import DonetickClient

pytestmark = requires_integration


class TestGetProfile:
    async def test_profile_is_valid(self, client: DonetickClient) -> None:
        profile = await client.get_profile()
        assert profile.id > 0
        assert profile.username == "testuser"
        assert profile.circle_id >= 0

    async def test_profile_has_a_display_name(self, client: DonetickClient) -> None:
        profile = await client.get_profile()
        assert profile.display_name == "Test User"


class TestGetUsers:
    async def test_returns_a_list(self, client: DonetickClient) -> None:
        assert isinstance(await client.get_users(), list)

    async def test_contains_the_test_user(self, client: DonetickClient) -> None:
        usernames = [user.get("username", "") for user in await client.get_users()]
        assert "testuser" in usernames


class TestCircleMembers:
    """`/circles/members` carries role and points that `/users/` omits."""

    async def test_returns_the_current_user(self, client: DonetickClient) -> None:
        members = await client.get_circle_members()
        assert [member.username for member in members] == ["testuser"]

    async def test_carries_role_and_points(self, client: DonetickClient) -> None:
        member = (await client.get_circle_members())[0]
        assert member.user_id > 0
        assert isinstance(member.role, str)
        assert isinstance(member.points, int)

    async def test_ids_match_the_legacy_endpoint(self, client: DonetickClient) -> None:
        profile = await client.get_profile()
        member = (await client.get_circle_members())[0]
        assert member.user_id == profile.id