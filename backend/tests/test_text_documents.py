import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.storage import connect, migrate
from backend.app.text_models import (
    ConversionRequest,
    ConversionResult,
    ConvertedParagraph,
    ConverterCapability,
)
from backend.tests.test_api import server


def paragraph(
    original: str = "今天去武汉，价格是 128 元。", draft: str = "今天克武汉，价格是 128 块。"
) -> dict[str, str]:
    return {
        "id": str(uuid4()),
        "original_text": original,
        "draft_text": draft,
        "pronunciation_note": "专名按原稿核对",
    }


def document(revision: int = 0, paragraphs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "expected_revision": revision,
        "source_language": "zh-CN",
        "target_language": "wuhan",
        "register": "自然口语",
        "terms": "数字和地点不得改变",
        "paragraphs": paragraphs if paragraphs is not None else [paragraph()],
    }


class FakeConverter:
    capability = ConverterCapability(
        id="fixture",
        label="仅测试合成适配器",
        source_languages=("zh-CN",),
        target_languages=("wuhan",),
    )

    def __init__(
        self, action: Callable[[ConversionRequest], ConversionResult] | None = None
    ) -> None:
        self.action = action or self.success
        self.requests: list[ConversionRequest] = []

    @staticmethod
    def success(request: ConversionRequest) -> ConversionResult:
        return ConversionResult(
            paragraphs=tuple(
                ConvertedParagraph(id=item.id, draft_text=f"测试草稿：{item.original_text}")
                for item in reversed(request.paragraphs)
            ),
            warnings=("合成测试结果，不代表武汉话质量",),
        )

    def convert(self, request: ConversionRequest) -> ConversionResult:
        self.requests.append(request)
        return self.action(request)


@contextmanager
def project_client(
    directory: Path, converter: FakeConverter | None = None
) -> Iterator[tuple[TestClient, str]]:
    with TestClient(
        create_app(directory, text_converters=[converter] if converter else [])
    ) as client:
        project = client.post("/api/projects", json={"name": "文案测试"}).json()
        yield client, f"/api/projects/{project['id']}/text"


def confirm(client: TestClient, base: str, revision: int) -> httpx.Response:
    return client.post(
        f"{base}/confirm", json={"expected_revision": revision, "acknowledged": True}
    )


def test_initial_document_and_default_converter_unavailable(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        assert client.get(base).json() == {
            "revision": 0,
            "source_language": "zh-CN",
            "target_language": "wuhan",
            "register": "自然口语",
            "terms": "",
            "paragraphs": [],
            "conversion_warnings": [],
            "latest_confirmation": None,
            "confirmation_current": False,
        }
        capabilities = client.get("/api/text-converters").json()
        assert capabilities["converters"] == []
        assert "人工" in capabilities["message"]
        assert confirm(client, base, 0).status_code == 422
        denied = client.get(f"{base}/confirmed-input?expected_revision=0")
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "confirmation_required"
        assert client.put(base, json=document()).status_code == 200
        before = client.get(base).json()
        response = client.post(
            f"{base}/convert", json={"expected_revision": 1, "converter_id": "missing"}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "converter_unavailable"
        assert client.get(base).json() == before


def test_snapshots_history_ids_order_and_invalidation(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        first, second = paragraph(), paragraph("第二段原稿", "第二段草稿")
        saved = client.put(base, json=document(paragraphs=[first, second])).json()
        assert saved["revision"] == 1
        assert saved["paragraphs"] == [first, second]
        assert saved["confirmation_current"] is False
        confirmed = confirm(client, base, 1).json()
        snapshot = confirmed["latest_confirmation"]
        assert confirmed["confirmation_current"] is True
        assert snapshot["paragraphs"] == [first, second]
        assert snapshot["revision"] == 1
        assert snapshot["register"] == "自然口语"
        assert snapshot["terms"] == "数字和地点不得改变"
        assert snapshot["conversion_warnings"] == []
        assert set(snapshot) == {
            "id",
            "revision",
            "created_at",
            "source_language",
            "target_language",
            "register",
            "terms",
            "paragraphs",
            "conversion_warnings",
        }
        assert confirm(client, base, 1).json()["latest_confirmation"] == snapshot
        assert len(client.get(f"{base}/confirmations").json()["confirmations"]) == 1
        assert client.get(f"{base}/confirmed-input?expected_revision=1").json() == snapshot
        second["draft_text"] = "改过的第二段"
        second["pronunciation_note"] = "保留段落ID"
        edited = client.put(base, json=document(1, [second, first])).json()
        assert edited["revision"] == 2
        assert edited["paragraphs"] == [second, first]
        assert edited["latest_confirmation"] == snapshot
        assert edited["confirmation_current"] is False
        assert client.get(f"{base}/confirmed-input?expected_revision=2").status_code == 409
        assert (
            client.get(f"{base}/confirmed-input?expected_revision=1").json()["error"]["code"]
            == "revision_conflict"
        )
        assert client.get(f"{base}/confirmations/{snapshot['id']}").json() == snapshot
        current = confirm(client, base, 2).json()["latest_confirmation"]
        assert current["id"] != snapshot["id"]
        history = client.get(f"{base}/confirmations").json()["confirmations"]
        assert [item["revision"] for item in history] == [2, 1]
        # Deletion is persisted; surviving paragraph keeps its UUID.
        deleted = client.put(base, json=document(2, [first])).json()
        assert deleted["paragraphs"] == [first]
        assert deleted["confirmation_current"] is False
        # Even no-op saves are revisions and invalidate prior confirmation.
        confirm(client, base, 3)
        noop = client.put(base, json=document(3, [first])).json()
        assert noop["revision"] == 4
        assert noop["confirmation_current"] is False
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(base).json() == noop
        assert client.get(f"{base}/confirmations/{snapshot['id']}").json() == snapshot


@pytest.mark.parametrize("revision", [True, False, 0.0, "0", -1, None])
def test_strict_revisions(tmp_path: Path, revision: object) -> None:
    with project_client(tmp_path) as (client, base):
        payload = document()
        payload["expected_revision"] = revision
        assert client.put(base, json=payload).status_code == 422
        assert (
            client.post(
                f"{base}/confirm", json={"expected_revision": revision, "acknowledged": True}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{base}/convert", json={"expected_revision": revision, "converter_id": "fixture"}
            ).status_code
            == 422
        )
        assert client.get(base).json()["revision"] == 0


@pytest.mark.parametrize("value", ["true", "1.0", "-1", "NaN", "01", "1e0"])
def test_confirmed_input_strict_query(tmp_path: Path, value: str) -> None:
    with project_client(tmp_path) as (client, base):
        assert client.get(f"{base}/confirmed-input?expected_revision={value}").status_code == 422
        assert client.get(f"{base}/confirmed-input").status_code == 422


@pytest.mark.parametrize("acknowledged", [False, 1, "true", None])
def test_confirmation_explicit_acknowledgement(tmp_path: Path, acknowledged: object) -> None:
    with project_client(tmp_path) as (client, base):
        client.put(base, json=document())
        response = client.post(
            f"{base}/confirm", json={"expected_revision": 1, "acknowledged": acknowledged}
        )
        assert response.status_code == 422
        assert client.get(base).json()["latest_confirmation"] is None


@pytest.mark.parametrize(
    "paragraphs", [[], [paragraph("", "draft")], [paragraph("original", "  \n\t")]]
)
def test_incomplete_text_savable_not_confirmable(
    tmp_path: Path, paragraphs: list[dict[str, str]]
) -> None:
    with project_client(tmp_path) as (client, base):
        assert client.put(base, json=document(paragraphs=paragraphs)).status_code == 200
        assert confirm(client, base, 1).status_code == 422
        assert client.get(f"{base}/confirmed-input?expected_revision=1").status_code == 409


def test_concurrency_conflicts_and_cross_project(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        client.put(base, json=document())
        snapshot = confirm(client, base, 1).json()["latest_confirmation"]
        other = client.post("/api/projects", json={"name": "另一个"}).json()
        other_base = f"/api/projects/{other['id']}/text"
        assert client.get(f"{other_base}/confirmations/{snapshot['id']}").status_code == 404
        assert client.get(other_base).json()["revision"] == 0
        for response in (
            client.put(base, json=document(0)),
            confirm(client, base, 0),
            client.post(
                f"{base}/convert", json={"expected_revision": 0, "converter_id": "missing"}
            ),
        ):
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "revision_conflict"
        missing = f"/api/projects/{uuid4()}/text"
        assert client.get(missing).status_code == 404
        assert client.put(missing, json=document()).status_code == 404
        assert confirm(client, missing, 0).status_code == 404
        assert client.get(f"{missing}/confirmations").status_code == 404
        assert client.get(f"{missing}/confirmed-input?expected_revision=0").status_code == 404


@pytest.mark.parametrize(
    "case",
    [
        "too-many",
        "original",
        "draft",
        "note",
        "total-original",
        "total-draft",
        "terms",
        "register",
        "duplicate",
        "bad-id",
        "language",
        "control",
    ],
)
def test_input_limits_and_ids(tmp_path: Path, case: str) -> None:
    payload = document()
    if case == "too-many":
        payload["paragraphs"] = [paragraph() for _ in range(201)]
    elif case in ("original", "draft", "note"):
        field = {"original": "original_text", "draft": "draft_text", "note": "pronunciation_note"}[
            case
        ]
        payload["paragraphs"][0][field] = "字" * (501 if case == "note" else 5001)
    elif case.startswith("total-"):
        field = "original_text" if case == "total-original" else "draft_text"
        payload["paragraphs"] = [paragraph() for _ in range(5)]
        for item in payload["paragraphs"]:
            item[field] = "字" * 4001
    elif case in ("terms", "register"):
        payload[case] = "字" * (2001 if case == "terms" else 81)
    elif case == "duplicate":
        payload["paragraphs"] *= 2
    elif case == "bad-id":
        payload["paragraphs"][0]["id"] = "not-uuid"
    elif case == "language":
        payload["target_language"] = "en"
    else:
        payload["paragraphs"][0]["draft_text"] = "bad\x00text"
    with project_client(tmp_path) as (client, base):
        assert client.put(base, json=payload).status_code == 422
        assert client.get(base).json()["revision"] == 0


def test_inclusive_limits_and_allowed_newline_tab(tmp_path: Path) -> None:
    paragraphs = [paragraph("字" * 5000, "字" * 5000) for _ in range(4)]
    paragraphs[0]["pronunciation_note"] = "字" * 500
    payload = document(paragraphs=paragraphs)
    payload.update(register="字" * 80, terms="字" * 2000)
    with project_client(tmp_path) as (client, base):
        assert client.put(base, json=payload).status_code == 200
        payload = document(1, [paragraph("原稿\n\t继续", "草稿\n\t继续")])
        assert client.put(base, json=payload).status_code == 200
        payload = document(2, [paragraph("", "") for _ in range(200)])
        assert client.put(base, json=payload).status_code == 200


def test_converter_preserves_originals_ids_order_notes_and_snapshots(tmp_path: Path) -> None:
    adapter = FakeConverter()
    with project_client(tmp_path, adapter) as (client, base):
        paragraphs = [paragraph(), paragraph("第二段", "旧草稿")]
        client.put(base, json=document(paragraphs=paragraphs))
        previous = confirm(client, base, 1).json()["latest_confirmation"]
        assert client.get("/api/text-converters").json()["converters"][0]["id"] == "fixture"
        response = client.post(
            f"{base}/convert", json={"expected_revision": 1, "converter_id": "fixture"}
        )
        assert response.status_code == 200, response.text
        converted = response.json()
        assert converted["revision"] == 2
        assert converted["confirmation_current"] is False
        assert converted["latest_confirmation"] == previous
        for source, output in zip(paragraphs, converted["paragraphs"], strict=True):
            assert output == {**source, "draft_text": f"测试草稿：{source['original_text']}"}
        request = adapter.requests[0]
        assert request.speech_register == "自然口语" and request.terms == "数字和地点不得改变"
        assert request.source_language == "zh-CN" and request.target_language == "wuhan"
        assert [str(item.id) for item in request.paragraphs] == [item["id"] for item in paragraphs]
        snapshot = confirm(client, base, 2).json()["latest_confirmation"]
        assert snapshot["conversion_warnings"] == list(FakeConverter.success(request).warnings)
        assert snapshot["paragraphs"] == converted["paragraphs"]
        saved = client.put(base, json=document(2, converted["paragraphs"])).json()
        assert saved["conversion_warnings"] == []
        assert saved["latest_confirmation"] == snapshot


@pytest.mark.parametrize(
    "case", ["exception", "missing", "extra", "duplicate", "long", "control", "total", "warning"]
)
def test_invalid_converter_results_preserve_document(tmp_path: Path, case: str) -> None:
    def action(request: ConversionRequest) -> ConversionResult:
        if case == "exception":
            raise RuntimeError("private credential path")
        result = FakeConverter.success(request).model_dump(mode="json")
        if case == "missing":
            result["paragraphs"] = result["paragraphs"][:-1]
        elif case == "extra":
            result["paragraphs"].append({"id": str(uuid4()), "draft_text": "额外"})
        elif case == "duplicate":
            result["paragraphs"].append(result["paragraphs"][0])
        elif case in ("long", "control"):
            result["paragraphs"][0]["draft_text"] = "字" * 5001 if case == "long" else "bad\x00"
        elif case == "total":
            for item in result["paragraphs"]:
                item["draft_text"] = "字" * 4001
        elif case == "warning":
            result["warnings"] = ["bad\x00"]
        return result  # type: ignore[return-value]

    with project_client(tmp_path, FakeConverter(action)) as (client, base):
        client.put(base, json=document(paragraphs=[paragraph() for _ in range(5)]))
        confirm(client, base, 1)
        before = client.get(base).json()
        response = client.post(
            f"{base}/convert", json={"expected_revision": 1, "converter_id": "fixture"}
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == (
            "converter_failed" if case == "exception" else "converter_invalid_output"
        )
        assert "private credential path" not in response.text
        assert client.get(base).json() == before


def test_late_converter_cannot_overwrite_new_confirmed_revision(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()

    def action(request: ConversionRequest) -> ConversionResult:
        started.set()
        assert release.wait(timeout=5)
        return FakeConverter.success(request)

    with (
        project_client(tmp_path, FakeConverter(action)) as (client, base),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        client.put(base, json=document())
        future = executor.submit(
            client.post, f"{base}/convert", json={"expected_revision": 1, "converter_id": "fixture"}
        )
        try:
            assert started.wait(timeout=5)
            # This write succeeds while the external call is blocked: no write transaction is held.
            changed = client.put(base, json=document(1, [paragraph("新稿", "新草稿")]))
            assert changed.status_code == 200
            confirmed = confirm(client, base, 2).json()
        finally:
            release.set()
        response = future.result(timeout=5)
        assert response.status_code == 409
        assert client.get(base).json() == confirmed


def test_storage_failure_rolls_back_document_and_snapshot(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        client.put(base, json=document())
        before = client.get(base).json()
        with connect(tmp_path / "projects.sqlite3") as connection:
            connection.execute(
                "CREATE TRIGGER fail_project_update BEFORE UPDATE ON projects "
                "BEGIN SELECT RAISE(ABORT, 'private failure'); END"
            )
        for response in (client.put(base, json=document(1)), confirm(client, base, 1)):
            assert response.status_code == 503
            assert "private failure" not in response.text
        assert client.get(base).json() == before
        assert client.get(f"{base}/confirmations").json() == {"confirmations": []}


def test_m02_migration_upgrade_and_idempotence(tmp_path: Path) -> None:
    migrations = tmp_path / "old"
    migrations.mkdir()
    source = Path(__file__).parents[1] / "app/migrations"
    for name in ("001_projects.sql", "002_references.sql"):
        (migrations / name).write_bytes((source / name).read_bytes())
    database = tmp_path / "projects.sqlite3"
    migrate(database, migrations)
    with connect(database) as connection:
        connection.execute("INSERT INTO projects VALUES ('old', 'old project', 'now', 'now')")
        connection.execute(
            "INSERT INTO audio_references VALUES "
            "('ref', 'old', 'sample.wav', 44, 'hash', 100, 'now')"
        )
    migrate(database)
    migrate(database)
    with connect(database) as connection:
        assert (
            connection.execute("SELECT filename FROM audio_references").fetchone()[0]
            == "sample.wav"
        )
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5


def test_actual_process_restart_preserves_text_and_history(tmp_path: Path) -> None:
    with server(tmp_path) as url:
        project = httpx.post(f"{url}/api/projects", json={"name": "文案重启"}).json()
        base = f"{url}/api/projects/{project['id']}/text"
        saved = httpx.put(base, json=document())
        assert saved.status_code == 200
        confirmed = httpx.post(
            f"{base}/confirm", json={"expected_revision": 1, "acknowledged": True}
        ).json()
        snapshot = confirmed["latest_confirmation"]
        assert httpx.put(base, json=document(1, [paragraph("新原稿", "新草稿")])).status_code == 200
        edited = httpx.get(base).json()
    with server(tmp_path) as url:
        base = f"{url}/api/projects/{project['id']}/text"
        assert httpx.get(base).json() == edited
        assert httpx.get(f"{base}/confirmations/{snapshot['id']}").json() == snapshot
        assert httpx.get(f"{base}/confirmed-input?expected_revision=2").status_code == 409
        current = httpx.post(
            f"{base}/confirm", json={"expected_revision": 2, "acknowledged": True}
        ).json()
        assert (
            httpx.get(f"{base}/confirmed-input?expected_revision=2").json()
            == current["latest_confirmation"]
        )
