import os
import unicodedata
from functools import lru_cache
from itertools import groupby
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


INK = "#15213d"
MUTED = "#56647e"
BLUE = "#25457e"
PANEL = "#f4f7fc"
LINE = "#e1e8f3"


@lru_cache(maxsize=24)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = (
        windows_fonts / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto") / ("NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    raise OSError("No CJK font found for the Kikoeru work cards")


@lru_cache(maxsize=12)
def _symbol_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ...]:
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = (
        windows_fonts / "seguisym.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Apple Symbols.ttf"),
    )
    fonts = []
    for candidate in candidates:
        if candidate.is_file():
            try:
                fonts.append(ImageFont.truetype(str(candidate), size))
            except OSError:
                continue
    return tuple(fonts)


@lru_cache(maxsize=32)
def _missing_glyph(font: ImageFont.FreeTypeFont) -> tuple[tuple[int, int], bytes]:
    mask = font.getmask(chr(0x10FFFF))
    return mask.size, bytes(mask)


@lru_cache(maxsize=256)
def _symbol_font(font: ImageFont.FreeTypeFont, char: str) -> ImageFont.FreeTypeFont:
    for candidate in (font, *_symbol_fonts(font.size)):
        mask = candidate.getmask(char)
        if (mask.size, bytes(mask)) != _missing_glyph(candidate):
            return candidate
    return font


def _text_runs(value: str, font: ImageFont.FreeTypeFont) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    return [
        ("".join(chars), run_font)
        for run_font, chars in groupby(
            value,
            key=lambda char: _symbol_font(font, char) if unicodedata.category(char).startswith("S") else font,
        )
    ]


def _text_width(value: str, font: ImageFont.FreeTypeFont) -> float:
    return sum(run_font.getlength(text) for text, run_font in _text_runs(value, font))


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
) -> None:
    runs = _text_runs(value, font)
    if len(runs) <= 1:
        draw.text(xy, value, font=runs[0][1] if runs else font, fill=fill, anchor="lt")
        return
    x, top = xy
    baseline = top - min(run_font.getbbox(text, anchor="ls")[1] for text, run_font in runs)
    for text, run_font in runs:
        draw.text((x, baseline), text, font=run_font, fill=fill, anchor="ls")
        x += run_font.getlength(text)


def _wrap(value: str, font: ImageFont.FreeTypeFont, width: int, limit: int | None = None) -> list[str]:
    lines = []
    line = ""
    for token in " ".join(value.split()):
        # Character wrapping also works for titles without spaces in CJK scripts.
        if _text_width(line + token, font) <= width:
            line += token
        else:
            lines.append(line)
            line = token.lstrip()
    if line:
        lines.append(line.rstrip())
    if limit and len(lines) > limit:
        lines = lines[:limit]
        while lines[-1] and _text_width(lines[-1] + "…", font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def render_work_card(
    item: dict,
    rows: list[dict] | None,
    cover: Path | None,
    index: int | None,
    destination: Path,
) -> None:
    """Draw one work and its numbered downloadable tracks as a JPEG.

    Args:
        item: Work title and metadata.
        rows: Numbered tracks, or None if the backend could not list them.
        cover: Local artwork, if available.
        index: Candidate number, or None for an individually selected work.
        destination: Path to write.
    """
    width = 1400
    left, right, end = 65, 470, 1335
    title_font = _font(44, True)
    subtitle_font = _font(24)
    track_font = _font(22, True)
    title_lines = _wrap(item.get("work_title") or item["title"], title_font, end - left - 8)
    info = "  ·  ".join(
        value
        for value in (
            item.get("kikoeru_rj"),
            item.get("circle"),
            item.get("duration"),
            item.get("kikoeru_lang"),
            item.get("release"),
        )
        if value
    )
    tags = item.get("tags") or []
    info_lines = _wrap(info, subtitle_font, end - left - 48, 2)
    if tags:
        info_lines.extend(_wrap("标签  " + " · ".join(tags), subtitle_font, end - left - 48))

    info_y = 123 + len(title_lines) * 56
    info_height = 42 + max(1, len(info_lines)) * 34
    body_y = info_y + info_height + 22
    track_count = len(rows or [])
    columns = 1 if track_count <= 12 else 2
    rows_per_column = (track_count + columns - 1) // columns
    track_width = (end - right - 48 - 18 * (columns - 1)) // columns
    track_top = body_y + 71
    track_height = 62
    body_height = max(376, 91 + track_height * rows_per_column)
    body_end = body_y + body_height
    height = body_end + 137

    image = Image.new("RGB", (width, height), "#eef2f8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((30, 30, width - 30, height - 30), 27, fill="#ffffff")
    badge = f"第 {index} 部" if index is not None else "KikoFlu"
    draw.rounded_rectangle((left, 61, left + 153, 107), 18, fill="#e6edff")
    draw.text((left + 76, 84), badge, font=_font(22, True), fill=BLUE, anchor="mm")
    for row_index, line in enumerate(title_lines):
        _draw_text(draw, (left, 124 + row_index * 56), line, title_font, INK)

    draw.rounded_rectangle((left, info_y, end, info_y + info_height), 18, fill=PANEL, outline=LINE, width=2)
    for row_index, line in enumerate(info_lines or ["KikoFlu 作品"]):
        _draw_text(draw, (left + 24, info_y + 23 + row_index * 34), line, subtitle_font, MUTED)

    cover_box = (left, body_y, right - 24, body_y + 376)
    draw.rounded_rectangle(cover_box, 18, fill="#e6edfa")
    if cover is not None:
        try:
            with Image.open(cover) as original:
                art = ImageOps.exif_transpose(original).convert("RGB")
                art.thumbnail((cover_box[2] - left - 20, 356), Image.Resampling.LANCZOS)
                image.paste(art, (left + (cover_box[2] - left - art.width) // 2, body_y + (376 - art.height) // 2))
        except (OSError, ValueError, Image.DecompressionBombError):
            cover = None
    if cover is None:
        draw.text(((left + cover_box[2]) // 2, body_y + 187), "封面暂不可用", font=subtitle_font, fill=BLUE, anchor="mm")

    draw.rounded_rectangle((right, body_y, end, body_end), 18, fill=PANEL, outline=LINE, width=2)
    count_text = f"音轨列表  ·  {track_count} 条" if rows else "音轨列表"
    heading_font = _font(27, True)
    draw.text((right + 24, body_y + 23), count_text, font=heading_font, fill=INK, anchor="lt")
    if any(variant["se"] == "无SE" for row in rows or [] for variant in row["variants"]):
        badge_x = round(right + 42 + draw.textlength(count_text, font=heading_font))
        draw.rounded_rectangle((badge_x, body_y + 17, badge_x + 115, body_y + 53), 11, fill="#e6edff")
        draw.text((badge_x + 57, body_y + 35), "含无音效", font=_font(19, True), fill=BLUE, anchor="mm")
    if not rows:
        draw.text((right + 24, track_top), "音轨暂不可用，回复作品编号可重试。", font=subtitle_font, fill=MUTED, anchor="lt")
    for number, row in enumerate(rows or [], 1):
        column, position = divmod(number - 1, rows_per_column)
        x = right + 24 + column * (track_width + 18)
        y = track_top + position * track_height
        draw.rounded_rectangle((x, y, x + 47, y + 33), 8, fill="#e6edff")
        draw.text((x + 23, y + 17), f"{number:02}", font=_font(18, True), fill=BLUE, anchor="mm")
        title_x = x + 59
        labels = _wrap(row["title"], track_font, track_width - 59, 2)
        for line_index, label in enumerate(labels):
            _draw_text(draw, (title_x, y + line_index * 24), label, track_font, INK)
        draw.line((x, y + track_height - 7, x + track_width, y + track_height - 7), fill=LINE, width=1)

    example = f"第{index}部第1号" if index is not None else "第1号"
    footer = f"回复「{example}」下载音轨，可指定 MP4、MP3 或 WAV。"
    draw.text((left, body_end + 26), footer, font=subtitle_font, fill=BLUE, anchor="lt")
    image.save(destination, "JPEG", quality=90, optimize=True)
