"""The client's skill request can set lifecycle status and SKILL.md fetch auth."""

import pytest

from api.registry_client import SkillRegistrationRequest

pytestmark = pytest.mark.unit


def _request(**fields) -> SkillRegistrationRequest:
    return SkillRegistrationRequest(
        name="month-end-close", skill_md_url="https://example.com/SKILL.md", **fields
    )


def test_omits_status_and_auth_when_unset_so_existing_callers_are_unchanged():
    body = _request().model_dump(exclude_none=True)

    assert {"status", "auth_scheme", "auth_credential"}.isdisjoint(body)


def test_sends_status_and_auth_when_set():
    body = _request(status="active", auth_scheme="bearer", auth_credential="YOUR_TOKEN").model_dump(
        exclude_none=True
    )

    assert (body["status"], body["auth_scheme"], body["auth_credential"]) == (
        "active",
        "bearer",
        "YOUR_TOKEN",
    )


def test_keeps_the_credential_out_of_the_repr():
    request = _request(auth_scheme="bearer", auth_credential="YOUR_TOKEN")

    assert "YOUR_TOKEN" not in repr(request)
