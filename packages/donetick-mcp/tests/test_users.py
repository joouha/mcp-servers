"""Integration tests for user-related operations."""

from __future__ import annotations

from donetick_mcp import DonetickClient


class TestGetProfile:
    """Tests for retrieving the current user's profile."""

    def test_profile_returns_valid_user(self, client: DonetickClient) -> None:
        profile = client.get_profile()
        assert profile.id > 0
        assert profile.username == "testuser"
        assert profile.circle_id >= 0

    def test_profile_has_display_name(self, client: DonetickClient) -> None:
        profile = client.get_profile()
        assert profile.display_name == "Test User"


class TestGetUsers:
    """Tests for listing circle users."""

    def test_users_returns_list(self, client: DonetickClient) -> None:
        users = client.get_users()
        assert isinstance(users, list)
        assert len(users) >= 1

    def test_users_contains_test_user(self, client: DonetickClient) -> None:
        users = client.get_users()
        usernames = [u.get("username", "") for u in users]
        assert "testuser" in usernames
