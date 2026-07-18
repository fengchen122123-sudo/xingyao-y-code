from core.plan import PlanManager


def test_plan_update_rejects_unknown_status():
    plan = PlanManager()
    plan.replace([{"id": "1", "subject": "test", "status": "pending"}])

    assert plan.update("1", status="invalid") is False
    assert plan.snapshot()[0]["status"] == "pending"


def test_plan_tracks_in_progress_item_and_returns_copies():
    plan = PlanManager()
    plan.replace([
        {"id": "1", "subject": "first", "status": "completed"},
        {"id": "2", "subject": "second", "status": "in_progress"},
    ])

    items = plan.snapshot()
    items[0]["subject"] = "changed outside manager"

    assert plan.in_progress()["id"] == "2"
    assert plan.snapshot()[0]["subject"] == "first"
