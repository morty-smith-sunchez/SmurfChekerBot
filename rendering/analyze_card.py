from __future__ import annotations

import html as html_lib
import io
import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
BANNER_PATH = ROOT / "assets" / "welcome_banner.png"

CARD_W = 1080
MARGIN = 40
HEADER_H = 120
AVATAR_SIZE = 152
BODY_FONT_SIZE = 21
TITLE_FONT_SIZE = 34
SUB_FONT_SIZE = 24
LINE_H = 28
MAX_IMAGE_HEIGHT = 3600

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


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _make_rank_badge(rank_tier: int | None, leaderboard_rank: int | None) -> Image.Image:
    w, h = 320, 76
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
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=18, fill=(*fill, 235), outline=(255, 255, 255, 90), width=2)
    font_big = _load_font(26)
    font_small = _load_font(20)
    draw.text((16, 10), title, font=font_big, fill=(255, 255, 255, 255))
    if subtitle:
        draw.text((16, 44), subtitle, font=font_small, fill=(240, 240, 255, 240))
    return im


def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = ImageOps.fit(img.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


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
) -> bytes | None:
    if not BANNER_PATH.is_file():
        return None

    try:
        plain_lines = [_strip_html_line(x) for x in html_lines]
        body = "\n".join(plain_lines)
        wrapped: list[str] = []
        for para in body.split("\n"):
            if not para.strip():
                wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(para, width=46) or [""])

        font_title = _load_font(TITLE_FONT_SIZE)
        font_sub = _load_font(SUB_FONT_SIZE)
        font_body = _load_font(BODY_FONT_SIZE)

        # Header + avatar row + rank badge strip + body text
        sep_estimate = HEADER_H + 20 + AVATAR_SIZE + 120
        body_h = len(wrapped) * LINE_H + 100
        total_h = min(MAX_IMAGE_HEIGHT, max(900, sep_estimate + body_h))

        banner = Image.open(BANNER_PATH).convert("RGB")
        bg = banner.resize((CARD_W, total_h), resample=Image.Resampling.LANCZOS)
        bg = ImageEnhance.Brightness(bg).enhance(0.52)
        bg = ImageEnhance.Contrast(bg).enhance(1.05)

        draw = ImageDraw.Draw(bg, "RGBA")

        draw.rectangle((0, 0, CARD_W, HEADER_H), fill=(10, 12, 20, 200))
        draw.text((MARGIN, 28), "SmurfChekBot · отчёт", font=font_title, fill=(245, 248, 255))
        draw.text((MARGIN, 78), "смурфы и аккбаеры", font=font_sub, fill=(180, 190, 210))

        y0 = HEADER_H + 20
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

        tx = MARGIN + AVATAR_SIZE + 24
        nick = nickname or f"account {account_id}"
        draw.text((tx, y0 + 4), nick[:42], font=font_title, fill=(255, 255, 255))
        draw.text((tx, y0 + 50), f"account_id: {account_id}", font=font_sub, fill=(200, 210, 230))
        y_meta = y0 + 86
        if steamid64:
            draw.text((tx, y_meta), f"steamid64: {steamid64}", font=font_sub, fill=(190, 200, 220))
            y_meta += 30
        if steam_level is not None:
            draw.text((tx, y_meta), f"Steam level: {steam_level}", font=font_sub, fill=(190, 200, 220))
            y_meta += 30
        if steam_created:
            draw.text((tx, y_meta), f"Steam с: {steam_created}", font=font_sub, fill=(190, 200, 220))
            y_meta += 30

        badge = _make_rank_badge(rank_tier, leaderboard_rank)
        badge_y = max(y0 + AVATAR_SIZE + 10, y_meta + 8)
        bg.paste(badge, (MARGIN, badge_y), badge)

        sep_y = badge_y + badge.height + 24
        draw.line((MARGIN, sep_y, CARD_W - MARGIN, sep_y), fill=(120, 130, 160, 160), width=2)

        y = sep_y + 20
        for line in wrapped:
            if y > total_h - MARGIN:
                draw.text((MARGIN, total_h - 40), "… (обрезано по высоте)", font=font_body, fill=(200, 200, 210))
                break
            draw.text((MARGIN, y), line or " ", font=font_body, fill=(230, 235, 245))
            y += LINE_H

        buf = io.BytesIO()
        bg.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return None
