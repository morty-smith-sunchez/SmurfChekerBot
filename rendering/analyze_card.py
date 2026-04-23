from __future__ import annotations

import html as html_lib
import io
import os
import re
import textwrap
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
BANNER_PATH = ROOT / "assets" / "welcome_banner.png"
# Bundled OFL font (Latin + Cyrillic); used when system fonts are missing (Docker/Linux) or unreliable.
BUNDLED_SANS = ROOT / "assets" / "fonts" / "NotoSans-Regular.ttf"

# Full HD frame per page; background keeps aspect ratio of banner (cover crop).
PAGE_W = 1920
PAGE_H = 1080
MARGIN = 56
HEADER_H = 140
HEADER_THIN = 96
AVATAR_SIZE = 196
BODY_FONT_SIZE = 28
TITLE_FONT_SIZE = 46
SUB_FONT_SIZE = 30
LINE_H = 38
WRAP_CHARS = 88
MAX_PAGES = 8

_MEDAL_NAMES: dict[int, str] = {
    1: "Рекрут",
    2: "Страж",
    3: "Крестоносец",
    4: "Архонт",
    5: "Легенда",
    6: "Древний",
    7: "Божество",
    8: "Иммортал",
}

_MEDAL_COLORS: dict[int, tuple[int, int, int]] = {
    1: (110, 128, 150),
    2: (70, 130, 180),
    3: (46, 139, 87),
    4: (218, 165, 32),
    5: (186, 85, 211),
    6: (220, 80, 60),
    7: (255, 215, 0),
    8: (200, 60, 200),
}


def _strip_html_line(line: str) -> str:
    line = re.sub(r"<br\s*/?>", "\n", line, flags=re.I)
    line = re.sub(r"<[^>]+>", "", line)
    return html_lib.unescape(line).replace("\r", "")


def _filter_banner_duplicate_lines(lines: list[str]) -> list[str]:
    """Do not repeat banner slogans inside the body; they stay only in the drawn header."""
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append("")
            continue
        low = s.lower()
        if "смурфы и аккбаеры" in low:
            continue
        if "smurfchekbot" in low and len(s) < 56:
            continue
        out.append(line)
    return out


def _rank_label(rank_tier: int | None, leaderboard_rank: int | None) -> tuple[str, str]:
    if leaderboard_rank is not None and isinstance(leaderboard_rank, int) and leaderboard_rank > 0:
        return ("Иммортал", f"топ-{leaderboard_rank}")
    if rank_tier is None or rank_tier <= 0:
        return ("Без ранга", "")
    if rank_tier >= 80:
        return ("Иммортал", "")
    medal = rank_tier // 10
    stars = rank_tier % 10
    name = _MEDAL_NAMES.get(medal, f"Ранг {rank_tier}")
    if medal >= 8:
        return (name, "")
    if stars > 0:
        return (name, "★" * min(stars, 5))
    return (name, "")


def _font_paths_ordered() -> list[Path]:
    """Prefer bundled Noto, then OS fonts known to cover Cyrillic."""
    paths: list[Path] = [BUNDLED_SANS]
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        fonts_dir = Path(windir) / "Fonts"
        if fonts_dir.is_dir():
            for name in (
                "segoeui.ttf",
                "arial.ttf",
                "calibri.ttf",
                "verdana.ttf",
                "trebuc.ttf",
                "micross.ttf",
            ):
                paths.append(fonts_dir / name)
    for rel in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        paths.append(Path(rel))
    return paths


def _font_draws_cyrillic(font: ImageFont.FreeTypeFont) -> bool:
    """Reject bitmap / incomplete fonts: Pillow default cannot render Cyrillic."""
    try:
        bb = font.getbbox("Жы")
        return bb[2] > bb[0] and bb[3] > bb[1]
    except Exception:
        return False


@lru_cache(maxsize=24)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _font_paths_ordered():
        if not path.is_file():
            continue
        try:
            ft = ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
        if isinstance(ft, ImageFont.FreeTypeFont) and _font_draws_cyrillic(ft):
            return ft
    for path in _font_paths_ordered():
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _make_rank_badge(rank_tier: int | None, leaderboard_rank: int | None) -> Image.Image:
    w, h = 380, 88
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    title, subtitle = _rank_label(rank_tier, leaderboard_rank)
    medal = 0
    if rank_tier and rank_tier > 0 and rank_tier < 80:
        medal = rank_tier // 10
    elif rank_tier and rank_tier >= 80:
        medal = 8
    if leaderboard_rank is not None and isinstance(leaderboard_rank, int) and leaderboard_rank > 0:
        medal = 8
    fill = _MEDAL_COLORS.get(medal, (90, 96, 110))
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=22, fill=(*fill, 235), outline=(255, 255, 255, 90), width=2)
    font_big = _load_font(30)
    font_small = _load_font(22)
    draw.text((18, 12), title, font=font_big, fill=(255, 255, 255, 255))
    if subtitle:
        draw.text((18, 52), subtitle, font=font_small, fill=(240, 240, 255, 240))
    return im


def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = ImageOps.fit(img.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _cover_background(banner: Image.Image, pw: int, ph: int, y_shift: int) -> Image.Image:
    """Scale+crop banner to fill (pw, ph) keeping aspect ratio (cover)."""
    bw, bh = banner.size
    scale = max(pw / bw, ph / bh)
    nw = max(int(bw * scale) + 1, pw)
    nh = max(int(bh * scale) + 1, ph)
    scaled = banner.resize((nw, nh), resample=Image.Resampling.LANCZOS)
    left = max(0, (nw - pw) // 2)
    top0 = max(0, (nh - ph) // 2)
    top = min(max(0, top0 + y_shift), nh - ph)
    crop = scaled.crop((left, top, left + pw, top + ph))
    crop = ImageEnhance.Brightness(crop).enhance(0.50)
    crop = ImageEnhance.Contrast(crop).enhance(1.06)
    return crop.convert("RGB")


def _wrap_plain_text(plain_lines: list[str]) -> list[str]:
    wrapped: list[str] = []
    for para in "\n".join(plain_lines).split("\n"):
        if not para.strip():
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(para, width=WRAP_CHARS) or [""])
    return wrapped if wrapped else [""]


def _split_chunks(
    wrapped: list[str],
    *,
    first_page_lines: int,
    other_page_lines: int,
) -> list[list[str]]:
    if first_page_lines <= 0:
        first_page_lines = 1
    if other_page_lines <= 0:
        other_page_lines = 1
    chunks: list[list[str]] = []
    idx = 0
    chunks.append(wrapped[idx : idx + first_page_lines])
    idx += first_page_lines
    while idx < len(wrapped):
        chunks.append(wrapped[idx : idx + other_page_lines])
        idx += other_page_lines
    while len(chunks) > 1 and not any(x.strip() for x in chunks[-1]):
        chunks.pop()
    return chunks[:MAX_PAGES]


def render_analyze_card(
    *,
    html_lines: list[str],
    nickname: str | None,
    account_id: int,
    steamid64: int | None,
    steam_level: int | None,
    steam_created: str | None,
    rank_tier: int | None,
    leaderboard_rank: int | None,
    avatar_png: bytes | None,
) -> list[bytes]:
    if not BANNER_PATH.is_file():
        return []

    try:
        plain = _filter_banner_duplicate_lines([_strip_html_line(x) for x in html_lines])
        wrapped = _wrap_plain_text(plain)
        banner = Image.open(BANNER_PATH).convert("RGB")

        font_title = _load_font(TITLE_FONT_SIZE)
        font_sub = _load_font(SUB_FONT_SIZE)
        font_body = _load_font(BODY_FONT_SIZE)

        y0 = HEADER_H + 24
        y_meta = y0 + 108
        if steamid64:
            y_meta += 34
        if steam_level is not None:
            y_meta += 34
        if steam_created:
            y_meta += 34
        badge_h = 88
        badge_y_est = max(y0 + AVATAR_SIZE + 12, y_meta + 8)
        sep_y_est = badge_y_est + badge_h + 24
        first_body_y = sep_y_est + 28
        avail1 = (PAGE_H - first_body_y - MARGIN) // LINE_H
        avail_rest = (PAGE_H - HEADER_THIN - MARGIN - 32) // LINE_H
        cap_lines = avail1 + (MAX_PAGES - 1) * avail_rest
        if len(wrapped) > cap_lines:
            wrapped = wrapped[: max(0, cap_lines - 1)]
            wrapped.append("… (далее не поместилось на изображении)")
        chunks = _split_chunks(wrapped, first_page_lines=avail1, other_page_lines=avail_rest)
        if not chunks:
            chunks = [wrapped]

        pages: list[bytes] = []
        total_pages = len(chunks)

        for page_i, chunk in enumerate(chunks):
            y_shift = page_i * 72
            bg = _cover_background(banner, PAGE_W, PAGE_H, y_shift=y_shift)
            draw = ImageDraw.Draw(bg, "RGBA")

            if page_i == 0:
                draw.rectangle((0, 0, PAGE_W, HEADER_H), fill=(10, 12, 22, 228))
                draw.text((MARGIN, 32), "SmurfChekBot · отчёт", font=font_title, fill=(245, 248, 255))
                draw.text((MARGIN, 92), "смурфы и аккбаеры", font=font_sub, fill=(180, 190, 210))

                if avatar_png:
                    try:
                        av = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
                        av = _circle_avatar(av, AVATAR_SIZE)
                        bg.paste(av, (MARGIN, y0), av)
                    except Exception:
                        draw.ellipse(
                            (MARGIN, y0, MARGIN + AVATAR_SIZE, y0 + AVATAR_SIZE),
                            outline=(200, 200, 220, 200),
                            width=3,
                        )
                else:
                    draw.ellipse(
                        (MARGIN, y0, MARGIN + AVATAR_SIZE, y0 + AVATAR_SIZE),
                        outline=(160, 170, 190, 200),
                        width=3,
                    )

                tx = MARGIN + AVATAR_SIZE + 28
                nick = nickname or f"account {account_id}"
                draw.text((tx, y0 + 6), nick[:48], font=font_title, fill=(255, 255, 255))
                draw.text((tx, y0 + 62), f"account_id: {account_id}", font=font_sub, fill=(200, 210, 230))
                ym = y0 + 108
                if steamid64:
                    draw.text((tx, ym), f"steamid64: {steamid64}", font=font_sub, fill=(190, 200, 220))
                    ym += 34
                if steam_level is not None:
                    draw.text((tx, ym), f"Steam level: {steam_level}", font=font_sub, fill=(190, 200, 220))
                    ym += 34
                if steam_created:
                    draw.text((tx, ym), f"Steam с: {steam_created}", font=font_sub, fill=(190, 200, 220))
                    ym += 34

                badge = _make_rank_badge(rank_tier, leaderboard_rank)
                badge_y = max(y0 + AVATAR_SIZE + 12, ym + 8)
                bg.paste(badge, (MARGIN, badge_y), badge)
                sep_y_local = badge_y + badge.height + 24
                draw.line((MARGIN, sep_y_local, PAGE_W - MARGIN, sep_y_local), fill=(120, 130, 160, 160), width=2)
                y_text = sep_y_local + 28
            else:
                draw.rectangle((0, 0, PAGE_W, HEADER_THIN), fill=(10, 12, 22, 228))
                cap = f"SmurfChekBot · стр. {page_i + 1}/{total_pages}"
                draw.text((MARGIN, 28), cap, font=font_title, fill=(245, 248, 255))
                y_text = HEADER_THIN + 28

            for line in chunk:
                if y_text > PAGE_H - MARGIN - LINE_H:
                    break
                draw.text((MARGIN, y_text), line or " ", font=font_body, fill=(230, 235, 245))
                y_text += LINE_H

            buf = io.BytesIO()
            bg.convert("RGB").save(buf, format="PNG", optimize=True)
            pages.append(buf.getvalue())

        return pages
    except Exception:
        return []
