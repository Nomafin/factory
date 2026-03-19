from factory.db import Database
from factory.models import TaskCreate, TaskStatus


async def test_create_and_get_task():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(
        title="Fix login bug",
        description="The login timeout is too short",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="issue-123",
    ))

    assert task.id is not None
    assert task.title == "Fix login bug"
    assert task.status == TaskStatus.QUEUED

    fetched = await db.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "Fix login bug"

    await db.close()


async def test_list_tasks():
    db = Database(":memory:")
    await db.initialize()

    await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.create_task(TaskCreate(title="Task 2", repo="myapp", agent_type="coder"))

    tasks = await db.list_tasks()
    assert len(tasks) == 2

    await db.close()


async def test_update_task_status():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    updated = await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.started_at is not None

    await db.close()


async def test_list_tasks_by_status():
    db = Database(":memory:")
    await db.initialize()

    await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    t2 = await db.create_task(TaskCreate(title="Task 2", repo="myapp", agent_type="coder"))
    await db.update_task_status(t2.id, TaskStatus.IN_PROGRESS)

    queued = await db.list_tasks(status=TaskStatus.QUEUED)
    assert len(queued) == 1
    assert queued[0].title == "Task 1"

    in_progress = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    assert len(in_progress) == 1
    assert in_progress[0].title == "Task 2"

    await db.close()


async def test_get_last_output():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.add_log(task.id, "First output")
    await db.add_log(task.id, "Second output")

    last = await db.get_last_output(task.id)
    assert last == "Second output"

    await db.close()


async def test_get_last_output_empty():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    last = await db.get_last_output(task.id)
    assert last is None

    await db.close()


async def test_get_last_output_truncated():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.add_log(task.id, "x" * 1000)

    last = await db.get_last_output(task.id)
    assert len(last) == 500

    await db.close()


async def test_get_logs_with_since():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.add_log(task.id, "First output")
    logs_before = await db.get_logs(task.id)
    since = logs_before[0]["timestamp"]

    await db.add_log(task.id, "Second output")

    logs = await db.get_logs(task.id, since=since)
    assert len(logs) == 1
    assert logs[0]["message"] == "Second output"

    await db.close()


async def test_get_logs_with_limit():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    for i in range(10):
        await db.add_log(task.id, f"Output {i}")

    logs = await db.get_logs(task.id, limit=3)
    assert len(logs) == 3
    assert logs[0]["message"] == "Output 0"

    await db.close()
