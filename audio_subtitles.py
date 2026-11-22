import html
import re
import struct
from pathlib import Path


_CUE_TIME = r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})"
_CUE_HEADER = re.compile(rf"^{_CUE_TIME}\s+-->\s+{_CUE_TIME}(?:[ \t].*)?$")
_TAG_DESCRIPTION = "KikoFlu subtitles"


def _timestamp_ms(value: str) -> int:
    whole, fraction = re.split(r"[.,]", value)
    seconds = 0
    for part in whole.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000 + int(fraction)


def _write_wave_cues(path: Path, cues: list[tuple[int, int, str]], sample_rate: int) -> None:
    """Write RIFF caption ranges expressed as sample offsets.

    Args:
        path: RIFF WAVE file to update.
        cues: Start and end times in milliseconds, with caption text.
        sample_rate: Number of audio samples per second.

    Raises:
        ValueError: The file is not a supported RIFF WAVE or would exceed its limit.
    """

    def chunk(name: bytes, payload: bytes) -> bytes:
        padding = b"\0" if len(payload) % 2 else b""
        return name + struct.pack("<I", len(payload)) + payload + padding

    points = []
    labels = []
    for identifier, (start, end, caption) in enumerate(cues, 1):
        start_sample = round(start * sample_rate / 1000)
        end_sample = round(end * sample_rate / 1000)
        points.append(struct.pack("<II4sIII", identifier, start_sample, b"data", 0, 0, start_sample))
        header = struct.pack("<II4s4H", identifier, end_sample - start_sample, b"capt", 0, 0, 0, 65001)
        labels.append(chunk(b"ltxt", header + caption.encode("utf-8") + b"\0"))
    cue_chunk = chunk(b"cue ", struct.pack("<I", len(points)) + b"".join(points))
    captions_chunk = chunk(b"LIST", b"adtl" + b"".join(labels))
    with path.open("r+b") as stream:
        magic, size, form = struct.unpack("<4sI4s", stream.read(12))
        if (magic, form) != (b"RIFF", b"WAVE") or stream.seek(0, 2) != size + 8:
            raise ValueError("Invalid RIFF WAVE container")
        stream.seek(12)
        while stream.tell() < size + 8:
            name, length = struct.unpack("<4sI", stream.read(8))
            next_chunk = stream.tell() + length + length % 2
            if next_chunk > size + 8:
                raise ValueError("Invalid RIFF chunk length")
            if name == b"cue " or (name == b"LIST" and length >= 4 and stream.read(4) == b"adtl"):
                # Preserve existing cue identifiers. The ID3 tag still contains the captions.
                return
            stream.seek(next_chunk)
        # Conventional RIFF cannot grow beyond its 32-bit size field.
        new_size = size + len(cue_chunk) + len(captions_chunk)
        if new_size > 0xFFFFFFFF:
            raise ValueError("Subtitle chunks exceed the RIFF size limit")
        stream.write(cue_chunk)
        stream.write(captions_chunk)
        stream.seek(4)
        stream.write(struct.pack("<I", new_size))


def embed_timed_subtitles(path: Path, content: bytes, language: str) -> bool:
    """Embed timed captions in an MP3 or WAV file without recoding audio.

    Args:
        path: Local MP3 or WAV file to update.
        content: UTF-8 WebVTT or SRT data.
        language: Three-character ID3 language code.

    Returns:
        Whether the file received any timed captions.
    """
    from mutagen.id3 import ID3, ID3NoHeaderError, SYLT
    from mutagen.wave import WAVE

    text = content.decode("utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.splitlines()
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        for index, line in enumerate(lines[:2]):
            if match := _CUE_HEADER.fullmatch(line.strip()):
                start, end = (_timestamp_ms(part) for part in match.groups())
                caption = html.unescape(re.sub(r"<[^>]+>", "", "\n".join(lines[index + 1 :])))
                caption = caption.replace("\0", "").strip()
                if caption and start < end <= 0xFFFFFFFF:
                    cues.append((start, end, caption))
                break
    if not cues:
        return False

    cues.sort()
    changes: dict[int, tuple[list[int], list[int]]] = {}
    for index, (start, end, _) in enumerate(cues):
        changes.setdefault(start, ([], []))[0].append(index)
        changes.setdefault(end, ([], []))[1].append(index)
    active: set[int] = set()
    entries = []
    for time, (starts, ends) in sorted(changes.items()):
        active.difference_update(ends)
        active.update(starts)
        caption = "\n".join(cues[index][2] for index in sorted(active))
        if not entries or entries[-1][0] != caption:
            entries.append((caption, time))

    if path.suffix.lower() == ".wav":
        wave = WAVE(path)
        _write_wave_cues(path, cues, wave.info.sample_rate)
        if wave.tags is None:
            wave.add_tags()
        tags = wave.tags
    else:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
    tags.add(SYLT(encoding=1, lang=language, format=2, type=1, desc=_TAG_DESCRIPTION, text=entries))
    tags.save(path, v2_version=3)
    return True
