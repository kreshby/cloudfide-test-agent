from datetime import date, datetime, timezone
from unittest.mock import Mock, call

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.features.todos.models import TodoRecord
from app.features.todos.repository import TodoRepository
from app.features.todos.schemas import TodoCreate, TodoUpdate


def test_list_maps_models_and_orders_tasks_newest_first(model_factory, todo_values, todo):
    session = Mock(spec=Session)
    session.scalars.return_value = [model_factory(todo_values)]
    assert TodoRepository(session).list() == [todo]
    statement = session.scalars.call_args.args[0]
    expected = select(TodoRecord).order_by(TodoRecord.created_at.desc(), TodoRecord.id.desc())
    assert statement.compare(expected)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "ORDER BY todos.created_at DESC, todos.id DESC" in compiled


def test_get_returns_none_for_missing_task():
    session = Mock(spec=Session)
    session.get.return_value = None
    assert TodoRepository(session).get(404) is None
    session.get.assert_called_once_with(TodoRecord, 404)


def test_create_adds_commits_refreshes_and_returns_mapped_todo(todo):
    session = Mock(spec=Session)
    payload = TodoCreate(**todo.model_dump(exclude={"id", "created_at", "updated_at"}))

    def refresh(record):
        record.id = todo.id

    session.refresh.side_effect = refresh
    before = datetime.now(timezone.utc)
    saved = TodoRepository(session).create(payload)
    after = datetime.now(timezone.utc)

    record = session.add.call_args.args[0]
    assert isinstance(record, TodoRecord)
    assert session.mock_calls == [call.add(record), call.commit(), call.refresh(record)]
    assert saved == todo.model_copy(update={
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    })
    assert before <= saved.created_at <= after
    assert saved.updated_at == saved.created_at


def test_update_applies_supplied_fields_commits_refreshes_and_returns_mapped_todo(
    model_factory, todo_values, todo
):
    session = Mock(spec=Session)
    record = model_factory({**todo_values, "due_date": date(2026, 10, 15)})
    session.get.return_value = record
    payload = TodoUpdate(title="Updated task", completed=True, due_date=None)
    before = datetime.now(timezone.utc)
    saved = TodoRepository(session).update(todo.id, payload)
    after = datetime.now(timezone.utc)

    assert session.mock_calls == [
        call.get(TodoRecord, todo.id),
        call.commit(),
        call.refresh(record),
    ]
    assert saved == todo.model_copy(update={
        "title": "Updated task",
        "completed": True,
        "due_date": None,
        "updated_at": record.updated_at,
    })
    assert before <= saved.updated_at <= after


def test_delete_deletes_existing_record_commits_and_returns_true(model_factory, todo_values):
    session = Mock(spec=Session)
    record = model_factory(todo_values)
    session.get.return_value = record

    assert TodoRepository(session).delete(todo_values["id"]) is True
    assert session.mock_calls == [
        call.get(TodoRecord, todo_values["id"]),
        call.delete(record),
        call.commit(),
    ]
