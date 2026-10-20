"""Bounded, in-memory text extraction; never persists or executes uploaded files."""

import io
import zipfile
import zlib
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from .generation import TaskError
from .text_models import safe_text

MAX_UPLOAD = 2 * 1024 * 1024
MAX_XML = 4 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class NoDTD(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("DTD is not supported")


def word_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > 32 * 1024 * 1024:
            raise ValueError("archive too large")
        matches = [item for item in entries if item.filename == "word/document.xml"]
        if len(matches) != 1 or matches[0].file_size > MAX_XML or matches[0].flag_bits & 1:
            raise ValueError("invalid document entry")
        with archive.open(matches[0]) as source:
            xml = source.read(MAX_XML + 1)
        if len(xml) > MAX_XML:
            raise ValueError("document too large")
    root = ET.fromstring(xml, parser=ET.XMLParser(target=NoDTD()))
    body = root.find(W + "body")
    if root.tag != W + "document" or body is None:
        raise ValueError("missing document body")

    def paragraph_text(element: ET.Element) -> str:
        if element.tag in (W + "drawing", W + "pict", W + "del", W + "txbxContent"):
            return ""
        if element.tag == W + "t":
            return element.text or ""
        if element.tag in (W + "br", W + "cr"):
            return "\n"
        if element.tag == W + "tab":
            return "\t"
        if element.tag == W + "noBreakHyphen":
            return "\u2011"
        return "".join(paragraph_text(child) for child in element)

    return "\n".join(paragraph_text(p) for p in body if p.tag == W + "p")


def extract(content: bytes, filename: str) -> dict[str, object]:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in ("txt", "docx"):
        raise TaskError(422, "import_format", "支持 TXT 和 Word .docx；旧 .doc 请先另存为 .docx。")
    if not content or len(content) > MAX_UPLOAD:
        raise TaskError(422, "import_size", "文件不能为空，且不能超过 2 MiB。")
    try:
        if suffix == "txt":
            codec = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            text = content.decode(codec)
        else:
            text = word_text(content)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        safe_text(text)
    except (
        ValueError,
        ET.ParseError,
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
        RecursionError,
        NotImplementedError,
        zlib.error,
        EOFError,
    ) as exc:
        raise TaskError(
            422,
            "import_invalid",
            "无法读取文件。TXT 请另存为 UTF-8，Word 请另存为未加密的 .docx；"
            "不支持损坏或过度压缩的文件。",
        ) from exc
    if not text.strip():
        raise TaskError(
            422, "import_empty", "未找到可导入的正文文字；图片、表格、页眉和文本框不作为正文提取。"
        )
    if len(text) > 5000:
        raise TaskError(
            422,
            "import_length",
            "本次导入替换一个段落，最多 5000 字符；请拆成较小文档后导入，不会截断内容。",
        )
    return {
        "text": text,
        "characters": len(text),
        "warnings": ["只提取正文段落；不含表格、页眉页脚、批注、文本框或图片文字，格式不保留。"]
        if suffix == "docx"
        else [],
    }


def import_router() -> APIRouter:
    routes = APIRouter()

    @routes.post("/api/text-import")
    async def preview(request: Request, filename: str = "") -> dict[str, object]:
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_UPLOAD:
                raise TaskError(413, "import_size", "文件不能超过 2 MiB，请拆小后导入。")
            content.extend(chunk)
        return await run_in_threadpool(extract, bytes(content), filename)

    return routes
