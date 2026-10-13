"""Test helpers shared by core and slice tests (factories, a test notification template)."""

from __future__ import annotations

from app.core.notifications.registry import NotificationTemplate

TEST_TEMPLATE = NotificationTemplate(
    id="test.notice",
    owner="core",
    subject="Test notice for {{ sr_number }}",
    html="testing/templates/test.notice.html.j2",
    text="testing/templates/test.notice.txt.j2",
    description="Template used by the test-suite only.",
    name="Test notice",
    trigger="tests",
    recipients="contact",
    sample_context={
        "gender": "female",
        "first_name": "Mia",
        "last_name": "Turner",
        "sr_number": "SR0012345",
        "appointment": "2026-10-14T07:00:00Z",
        "unsafe": "<b>x</b>",
    },
)
