"""Audio inspection and clean-up with ffmpeg. Every function degrades to "use the original file"."""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Prepared:
    path: Path          # file to send to the engines
    mime: str
    cleaned: bool       # True when ffmpeg produced a cleaned copy
    duration: float | None = None
    max_db: float | None = None
    note: str = ""


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


async def _run(*cmd: str, timeout: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "ffmpeg timed out"
    return proc.returncode or 0, err.decode(errors="replace")


async def inspect(path: Path) -> tuple[float | None, float | None, str]:
    """Return (duration seconds, peak level in dB, error). Decodes the whole file once."""
    code, err = await _run("ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
                           "-vn", "-af", "volumedetect", "-f", "null", "-")
    if code != 0:
        return None, None, err.strip().splitlines()[-1] if err.strip() else "ffmpeg failed"
    dur = None
    # The last "time=" progress value is the decoded length. The container header can lie.
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if times:
        h, m, s = times[-1]
        dur = int(h) * 3600 + int(m) * 60 + float(s)
    mx = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)
    max_db = None
    if mx:
        max_db = -120.0 if mx.group(1) == "-inf" else float(mx.group(1))
    return dur, max_db, ""


async def prepare(src: Path, mime: str, cleanup: bool) -> Prepared:
    """Measure the recording and, when asked, write a 16 kHz mono FLAC copy next to it.

    The filter chain is mild on purpose. Heavy noise suppression removes consonants and
    makes recognition worse. highpass drops handling rumble below speech; loudnorm brings
    a quiet ring microphone up to a level the engines were trained on.
    """
    if not have_ffmpeg():
        return Prepared(src, mime, False, note="ffmpeg is not installed; the original file was sent")
    dur, max_db, err = await inspect(src)
    if err:
        return Prepared(src, mime, False, note=f"could not read the audio: {err}")
    if not cleanup:
        return Prepared(src, mime, False, dur, max_db)
    out = src.with_suffix(".clean.flac")
    code, err = await _run(
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src), "-vn",
        "-af", "highpass=f=70,loudnorm=I=-18:TP=-2:LRA=11",
        "-ar", "16000", "-ac", "1", "-c:a", "flac", str(out))
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        return Prepared(src, mime, False, dur, max_db, note="clean-up failed; the original file was sent")
    return Prepared(out, "audio/flac", True, dur, max_db)
