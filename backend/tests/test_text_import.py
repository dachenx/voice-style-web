import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.generation import TaskError
from backend.app.main import create_app
from backend.app.text_import import MAX_UPLOAD, extract


def docx(xml):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return output.getvalue()


def document(body):
    return (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + body
        + "</w:body></w:document>"
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_text_encodings_are_lossless(encoding):
    assert (
        extract("测试\r\n金额128元。\r结束".encode(encoding), "稿件.TXT")["text"]
        == "测试\n金额128元。\n结束"
    )


def test_word_order_tabs_breaks_and_unsupported_content():
    xml = document(
        "<w:p><w:r><w:t>第一句</w:t><w:tab/><w:t>金额128</w:t><w:br/><w:t>下一行</w:t></w:r><w:drawing><w:t>图片文字</w:t></w:drawing></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>最后</w:t></w:r></w:p>"
    )
    result = extract(docx(xml), "test.docx")
    assert result["text"] == "第一句\t金额128\n下一行\n最后"
    assert result["warnings"]


def test_word_nonbreaking_hyphen_preserves_number_meaning():
    xml = document("<w:p><w:r><w:t>5</w:t><w:noBreakHyphen/><w:t>10</w:t></w:r></w:p>")
    assert extract(docx(xml), "numbers.docx")["text"] == "5\u201110"


@pytest.mark.parametrize(
    "content,name",
    [
        (b"", "x.txt"),
        (b"\xff\x00", "x.txt"),
        (b"abc\x00", "x.txt"),
        (b"x" * 5001, "x.txt"),
        (b"x" * (MAX_UPLOAD + 1), "x.txt"),
        (b"not zip", "x.docx"),
        (b"legacy", "x.doc"),
        (docx("<broken>"), "x.docx"),
        (docx(document("<w:p/>")), "x.docx"),
        (
            docx(document("<w:p><w:r><w:t>" + "x" * (4 * 1024 * 1024) + "</w:t></w:r></w:p>")),
            "x.docx",
        ),
        (
            docx(
                '<!DOCTYPE doc [<!ENTITY x "secret">]>'
                + document("<w:p><w:r><w:t>&x;</w:t></w:r></w:p>")
            ),
            "x.docx",
        ),
        (
            docx(
                (
                    '<!DOCTYPE doc [<!ENTITY x "secret">]>'
                    + document("<w:p><w:r><w:t>&x;</w:t></w:r></w:p>")
                ).encode("utf-16")
            ),
            "x.docx",
        ),
    ],
)
def test_invalid_inputs_rejected_without_partial_text(content, name):
    with pytest.raises(TaskError):
        extract(content, name)


def test_preview_endpoint_has_no_document_side_effects(tmp_path):
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        before = client.get("/api/projects").json()
        response = client.post(
            "/api/text-import?filename=sample.txt",
            content=b"hello\r\nworld",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200 and response.json()["text"] == "hello\nworld"
        assert client.get("/api/projects").json() == before
        assert (
            client.post(
                "/api/text-import?filename=x.txt", content=b"x" * (MAX_UPLOAD + 1)
            ).status_code
            == 413
        )
        assert not list(tmp_path.rglob("sample.txt"))


def test_corrupt_deflate_is_a_controlled_client_error(tmp_path):
    content = bytearray(docx(document("<w:p><w:r><w:t>test</w:t></w:r></w:p>")))
    # First local header has a known name and no extra fields. BTYPE=3 is invalid.
    content[30 + len("word/document.xml")] = 0x07
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        response = client.post("/api/text-import?filename=corrupt.docx", content=bytes(content))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "import_invalid"
