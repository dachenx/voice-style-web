import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.generation import GenerationRegistry, RetryInput, TaskError, TaskInput
from backend.app.storage import connect, migrate
from backend.app.tasks import enqueue, file_digest, get_task, retry_task
from backend.app.text_documents import TextError
from backend.tests.test_references import wav_bytes


def ready_project(directory: Path) -> tuple[Path, str, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "projects.sqlite3"
    migrate(database)
    project_id, reference_id, clip_id, confirmation_id = (str(uuid4()) for _ in range(4))
    content = {
        "source_language": "zh-CN",
        "target_language": "wuhan",
        "register": "自然口语",
        "terms": "金额128元不变",
        "conversion_warnings": ["人工核对专名"],
        "paragraphs": [
            dict(
                id=str(uuid4()),
                original_text="原稿128元",
                draft_text="草稿128块",
                pronunciation_note="名字保留",
            )
            for _ in range(2)
        ],
    }
    snapshot = {
        **content,
        "revision": 1,
        "id": confirmation_id,
        "created_at": "2026-10-16T00:00:00Z",
    }
    path = directory / "references" / reference_id / "clips" / f"{clip_id}.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(wav_bytes())
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO projects VALUES (?, 'task fixture', 'now', 'now')", (project_id,)
        )
        connection.execute(
            "INSERT INTO audio_references VALUES (?, ?, 'sample.wav', ?, ?, 1000, 'now')",
            (reference_id, project_id, path.stat().st_size, file_digest(path)),
        )
        connection.execute(
            "INSERT INTO reference_clips VALUES (?, ?, 0, 1000, 'now')", (clip_id, reference_id)
        )
        connection.execute(
            "INSERT INTO project_reference_selection VALUES (?, ?)", (project_id, clip_id)
        )
        connection.execute(
            "INSERT INTO text_documents VALUES (?, 1, ?)", (project_id, json.dumps(content))
        )
        connection.execute(
            "INSERT INTO text_confirmations VALUES (?, ?, 1, 'now', ?)",
            (confirmation_id, project_id, json.dumps(snapshot)),
        )
    payload = dict(
        request_id=str(uuid4()),
        expected_revision=1,
        expected_confirmation_id=confirmation_id,
        expected_clip_id=clip_id,
        adapter_id="simulated-tone",
        mode="preview",
        paragraph_id=content["paragraphs"][0]["id"],
        parameters={},
    )
    return database, project_id, payload


def test_snapshot_full_inputs_and_submission_idempotency(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, created = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    assert created and task["state"] == "queued"
    assert task["is_simulated"] and task["total_paragraphs"] == 1
    snapshot = task["snapshot"]
    assert len(snapshot["confirmation"]["paragraphs"]) == 2
    assert snapshot["confirmation"]["conversion_warnings"] == ["人工核对专名"]
    clip = snapshot["reference"]["clip"]
    path = tmp_path / "references" / clip["reference_id"] / "clips" / f"{clip['id']}.wav"
    assert snapshot["reference"]["clip_sha256"] == file_digest(path)
    assert snapshot["reference"]["sha256"] == file_digest(path)
    assert snapshot["adapter"]["version"] == "1"
    assert snapshot["paragraph_ids"] == [payload["paragraph_id"]]
    # New editing and reference selection do not mutate an accepted request or its snapshot.
    with connect(database) as connection:
        connection.execute("UPDATE text_documents SET revision=2")
        connection.execute("DELETE FROM project_reference_selection")
    repeated, created = enqueue(
        database, tmp_path, project, TaskInput(**payload), GenerationRegistry()
    )
    assert not created and repeated == task
    with pytest.raises(TaskError, match="请求 ID"):
        enqueue(
            database,
            tmp_path,
            project,
            TaskInput(**{**payload, "mode": "full", "paragraph_id": None}),
            GenerationRegistry(),
        )


def test_full_without_preview_and_atomic_concurrent_request(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    payload.update(mode="full", paragraph_id=None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                enqueue, database, tmp_path, project, TaskInput(**payload), GenerationRegistry()
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert sum(created for _, created in results) == 1
    assert results[0][0]["id"] == results[1][0]["id"]
    assert results[0][0]["total_paragraphs"] == 2
    assert len(results[0][0]["snapshot"]["paragraph_ids"]) == 2


@pytest.mark.parametrize(
    "case", ["revision", "confirmation", "reference", "unconfirmed", "paragraph", "adapter"]
)
def test_submission_guards(tmp_path: Path, case: str) -> None:
    database, project, payload = ready_project(tmp_path)
    if case == "revision":
        payload["expected_revision"] = 0
    elif case == "confirmation":
        payload["expected_confirmation_id"] = str(uuid4())
    elif case == "reference":
        payload["expected_clip_id"] = str(uuid4())
    elif case == "paragraph":
        payload["paragraph_id"] = str(uuid4())
    elif case == "adapter":
        payload["adapter_id"] = "unavailable"
    else:
        with connect(database) as connection:
            connection.execute("DELETE FROM text_confirmations")
    with pytest.raises((TaskError, TextError)):
        enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM generation_requests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case",
    [
        "parameters",
        "bool-revision",
        "float-revision",
        "full-paragraph",
        "preview-no-paragraph",
        "extra",
    ],
)
def test_task_input_validation(tmp_path: Path, case: str) -> None:
    _, _, payload = ready_project(tmp_path)
    if case == "parameters":
        payload["parameters"] = {"speed": 2}
    elif case == "bool-revision":
        payload["expected_revision"] = True
    elif case == "float-revision":
        payload["expected_revision"] = 1.0
    elif case == "full-paragraph":
        payload["mode"] = "full"
    elif case == "preview-no-paragraph":
        payload["paragraph_id"] = None
    else:
        payload["preview_task_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        TaskInput(**payload)


def test_retry_keeps_snapshot_and_only_one_direct_child(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with pytest.raises(TaskError):
        retry_task(database, project, task["id"], RetryInput(request_id=uuid4()))
    with connect(database) as connection:
        connection.execute(
            "UPDATE generation_tasks SET state='failed',phase='failed',"
            "error_code='fixture',error_message='failed' WHERE id=?",
            (task["id"],),
        )
        connection.execute("UPDATE text_documents SET revision=2")
    request = RetryInput(request_id=uuid4())
    retried, created = retry_task(database, project, task["id"], request)
    assert created and retried["id"] != task["id"]
    assert retried["snapshot"] == task["snapshot"]
    assert retried["retry_of"] == task["id"]
    assert retry_task(database, project, task["id"], request) == (retried, False)
    assert retry_task(database, project, task["id"], RetryInput(request_id=uuid4())) == (
        retried,
        False,
    )
    with connect(database) as connection:
        assert get_task(connection, project, task["id"])["state"] == "failed"
        assert connection.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 2


def test_submission_storage_failure_rolls_back(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    with connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON projects "
            "BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(sqlite3.Error):
        enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM generation_requests").fetchone()[0] == 0


def test_corrupt_reference_refused(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    path = next((tmp_path / "references").glob("*/clips/*.wav"))
    path.write_bytes(b"bad")
    with pytest.raises(OSError):
        enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
