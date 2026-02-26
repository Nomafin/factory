from factory.plane import parse_webhook_event


def test_parse_issue_create_webhook():
    payload = {
        "event": "issue",
        "action": "created",
        "data": {
            "id": "uuid-123",
            "name": "Fix login bug",
            "description_html": "<p>The timeout is too short</p>",
            "state": {"name": "Queued", "group": "unstarted"},
            "labels": [{"name": "coder"}, {"name": "repo:myapp"}],
        },
    }
    event = parse_webhook_event(payload)
    assert event.event_type == "issue"
    assert event.action == "create"
    assert event.issue_title == "Fix login bug"
    assert event.repo == "myapp"
    assert event.agent_type == "coder"
    assert event.state_name == "Queued"


def test_parse_issue_update_to_queued():
    payload = {
        "event": "issue",
        "action": "updated",
        "data": {
            "id": "uuid-123",
            "name": "Fix login bug",
            "description_html": "",
            "state": {"name": "Queued", "group": "unstarted"},
            "labels": [],
        },
    }
    event = parse_webhook_event(payload)
    assert event.action == "update"
    assert event.state_name == "Queued"


def test_parse_labels_for_repo_and_agent():
    payload = {
        "event": "issue",
        "action": "created",
        "data": {
            "id": "uuid-456",
            "name": "Review PR",
            "description_html": "",
            "state": {"name": "Queued", "group": "unstarted"},
            "labels": [{"name": "reviewer"}, {"name": "repo:frontend"}],
        },
    }
    event = parse_webhook_event(payload)
    assert event.repo == "frontend"
    assert event.agent_type == "reviewer"
