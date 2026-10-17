"""Bounded audio decoding and non-destructive PCM excerpts."""

import subprocess
import wave
from pathlib import Path
from typing import cast

import imageio_ffmpeg  # type: ignore[import-untyped]

MAX_BYTES = 50 * 1024 * 1024
SAMPLE_RATE = 24000
FORMATS = {".wav": "wav", ".mp3": "mp3", ".m4a": "mov", ".flac": "flac", ".ogg": "ogg"}


class AudioError(Exception):
    def __init__(self, message: str, status: int = 422, code: str = "invalid_audio") -> None:
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


def ffmpeg_executable() -> str:
    try:
        return cast(str, imageio_ffmpeg.get_ffmpeg_exe())
    except (RuntimeError, OSError) as exc:
        raise AudioError(
            "音频解码组件不可用，请检查依赖安装后重启服务", 503, "audio_dependency_unavailable"
        ) from exc


def decode(
    source: Path, destination: Path, suffix: str, *, min_frames: int = SAMPLE_RATE // 10
) -> int:
    demuxer = FORMATS[suffix]
    command = [
        ffmpeg_executable(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-max_alloc",
        "67108864",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        demuxer,
        "-f",
        demuxer,
    ]
    if demuxer == "mov":
        command += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    command += [
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-t",
        "600.1",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-threads",
        "1",
        "-f",
        "wav",
        "-y",
        str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=45, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("音频解码超时，请缩短音频或换一个文件") from exc
    except OSError as exc:
        raise AudioError(
            "音频解码组件无法启动，请检查依赖安装和执行权限后重启服务",
            503,
            "audio_dependency_unavailable",
        ) from exc
    if result.returncode:
        raise AudioError("无法解码音频，请确认文件完整且格式为 WAV、MP3、M4A、FLAC 或 OGG")
    try:
        with wave.open(str(destination), "rb") as audio:
            frames = audio.getnframes()
            if (
                audio.getframerate() != SAMPLE_RATE
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
            ):
                raise AudioError("音频试听副本格式无效")
    except (wave.Error, EOFError) as exc:
        raise AudioError("音频解码结果无效") from exc
    if not min_frames <= frames <= SAMPLE_RATE * 600:
        raise AudioError("音频时长不符合当前输入要求（最长 600 秒）")
    # Floor to milliseconds so a legal end_ms never exceeds the decoded samples.
    return frames * 1000 // SAMPLE_RATE


def excerpt(source: Path, destination: Path, start_ms: int, end_ms: int) -> None:
    try:
        with wave.open(str(source), "rb") as audio:
            audio.setpos(start_ms * SAMPLE_RATE // 1000)
            frames = audio.readframes((end_ms - start_ms) * SAMPLE_RATE // 1000)
        if len(frames) != (end_ms - start_ms) * SAMPLE_RATE // 1000 * 2:
            raise OSError("试听文件不完整")
    except (wave.Error, EOFError) as exc:
        raise OSError("试听文件无法读取") from exc
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames)
