from __future__ import annotations

import html as html_lib
import io
import os
import re
import textwrap
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from pilmoji import Pilmoji
except ImportError:
    Pilmoji = None  # type: ignore[misc, assignment]

ROOT = Path(__file__).resolve().parent.parent
_RENDERING_DIR = Path(__file__).resolve().parent
WELCOME_BANNER_PATH = ROOT / "assets" / "welcome_banner.png"
REPORT_BACKGROUND_PATH = ROOT / "assets" / "report_background.png"
PACKAGE_SANS = _RENDERING_DIR / "fonts" / "NotoSans-Regular.ttf"
BUNDLED_SANS = ROOT / "assets" / "fonts" / "NotoSans-Regular.ttf"

PAGE_W = 1920
PAGE_H = 1080
MARGIN = 56
# No opaque header bar: logo + profile start below a thin top margin.
LOGO_Y = 18
AVATAR_TOP = 78
AVATAR_SIZE = 196
BRAND_FONT_SIZE = 44
BODY_FONT_MAX = 28
BODY_FONT_MIN = 20
LINE_H_MAX = 38
LINE_H_MIN = 28
WRAP_MIN = 92
WRAP_MAX = 120
MAX_PAGES = 2

_MEDAL_NAMES_EN: dict[int, str] = {
    1: "Herald",
    2: "Guardian",
    3: "Crusader",
    4: "Archon",
    5: "Legend",
    6: "Ancient",
    7: "Divine",
    8: "Immortal",
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


def _rank_badge_content(rank_tier: int | None, leaderboard_rank: int | None) -> tuple[str, int, str]:
    """English medal title, star count (0–5, drawn as dots — not Unicode ★), optional subtitle line."""
    if leaderboard_rank is not None and isinstance(leaderboard_rank, int) and leaderboard_rank > 0:
        return ("Immortal", 0, f"Leaderboard #{leaderboard_rank}")
    if rank_tier is None or rank_tier <= 0:
        return ("Unranked", 0, "")
    if rank_tier >= 80:
        return ("Immortal", 0, "")
    medal = rank_tier // 10
    stars = rank_tier % 10
    name = _MEDAL_NAMES_EN.get(medal, f"Rank {rank_tier}")
    if medal >= 8:
        return (name, 0, "")
    n = min(int(stars), 5) if stars > 0 else 0
    return (name, n, "")


def format_rank_summary_en(rank_tier: int | None, leaderboard_rank: int | None) -> str:
    """Human-readable rank for HTML (English, no Unicode star glyphs)."""
    title, n, sub = _rank_badge_content(rank_tier, leaderboard_rank)
    parts: list[str] = [title]
    if n == 1:
        parts.append("1 star")
    elif n > 1:
        parts.append(f"{n} stars")
    if sub:
        parts.append(sub)
    return " — ".join(parts)


def _font_paths_ordered() -> list[Path]:
    paths: list[Path] = [PACKAGE_SANS, BUNDLED_SANS]
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


def _font_draws_cyrillic(font: ImageFont.ImageFont) -> bool:
    try:
        bb = font.getbbox("Жы")
        return bb[2] > bb[0] and bb[3] > bb[1]
    except Exception:
        return False


def _truetype(path: str, size: int) -> ImageFont.FreeTypeFont:
    layout = getattr(ImageFont, "Layout", None)
    basic = getattr(layout, "BASIC", None) if layout is not None else None
    kwargs: dict = {"size": size}
    if basic is not None:
        kwargs["layout_engine"] = basic
    return ImageFont.truetype(path, **kwargs)


@lru_cache(maxsize=32)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _font_paths_ordered():
        if not path.is_file():
            continue
        try:
            ft = _truetype(str(path), size)
        except OSError:
            continue
        if _font_draws_cyrillic(ft):
            return ft
    for path in _font_paths_ordered():
        if not path.is_file():
            continue
        try:
            return _truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_stroke_kw() -> dict:
    return {"stroke_width": 2, "stroke_fill": (14, 16, 26)}


def _pj_text(
    pj: object,
    xy: tuple[int, int],
    text: str,
    fill: tuple[int, int, int],
    font: ImageFont.ImageFont,
    *,
    use_pilmoji: bool,
    emoji_scale: float = 1.08,
) -> None:
    stroke = _text_stroke_kw()
    if use_pilmoji:
        pj.text(xy, text, fill, font, emoji_scale_factor=emoji_scale, **stroke)  # type: ignore[attr-defined]
    else:
        pj.text(xy, text, font=font, fill=fill, **stroke)  # type: ignore[attr-defined]


def _make_rank_badge(rank_tier: int | None, leaderboard_rank: int | None) -> Image.Image:
    w, h = 380, 88
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    title, n_stars, subtitle = _rank_badge_content(rank_tier, leaderboard_rank)
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
    draw.text((18, 10), title, font=font_big, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0))
    if n_stars > 0:
        cy, r = 57, 6
        for i in range(n_stars):
            cx = 24 + i * 20
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 220, 100), outline=(60, 45, 20), width=1)
    elif subtitle:
        draw.text(
            (18, 48),
            subtitle,
            font=font_small,
            fill=(248, 248, 255, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0),
        )
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
    """Cover-crop only; do not alter colors — background must look like the source file."""
    bw, bh = banner.size
    scale = max(pw / bw, ph / bh)
    nw = max(int(bw * scale) + 1, pw)
    nh = max(int(bh * scale) + 1, ph)
    scaled = banner.resize((nw, nh), resample=Image.Resampling.LANCZOS)
    left = max(0, (nw - pw) // 2)
    top0 = max(0, (nh - ph) // 2)
    top = min(max(0, top0 + y_shift), nh - ph)
    return scaled.crop((left, top, left + pw, top + ph)).convert("RGB")


def _wrap_plain_text(plain_lines: list[str], width: int) -> list[str]:
    wrapped: list[str] = []
    for para in "\n".join(plain_lines).split("\n"):
        if not para.strip():
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(para, width=width) or [""])
    return wrapped if wrapped else [""]


def _split_two_chunks(wrapped: list[str], first_page_lines: int, second_page_lines: int) -> list[list[str]]:
    if first_page_lines <= 0:
        first_page_lines = 1
    if second_page_lines <= 0:
        second_page_lines = 1
    a = wrapped[:first_page_lines]
    b = wrapped[first_page_lines : first_page_lines + second_page_lines]
    chunks: list[list[str]] = [a]
    if any(x.strip() for x in b):
        chunks.append(b)
    return chunks[:MAX_PAGES]


def _estimate_page1_body_start(
    *,
    steamid64: int | None,
    steam_level: int | None,
    steam_created: str | None,
    y0: int,
    badge_h: int,
) -> int:
    y_meta = y0 + 108
    if steamid64:
        y_meta += 34
    if steam_level is not None:
        y_meta += 34
    if steam_created:
        y_meta += 34
    badge_y_est = max(y0 + AVATAR_SIZE + 12, y_meta + 8)
    sep_y_est = badge_y_est + badge_h + 24
    return sep_y_est + 28


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
    bg_file = REPORT_BACKGROUND_PATH if REPORT_BACKGROUND_PATH.is_file() else WELCOME_BANNER_PATH
    if not bg_file.is_file():
        return []

    try:
        plain = _filter_banner_duplicate_lines([_strip_html_line(x) for x in html_lines])
        banner = Image.open(bg_file).convert("RGB")

        chosen: tuple[int, int, int] | None = None
        wrapped: list[str] = []
        for body_sz in range(BODY_FONT_MAX, BODY_FONT_MIN - 1, -1):
            line_h = max(LINE_H_MIN, min(LINE_H_MAX, body_sz + 10))
            wrap_w = WRAP_MIN + (BODY_FONT_MAX - body_sz) * 4
            wrap_w = min(WRAP_MAX, max(WRAP_MIN, wrap_w))
            w = _wrap_plain_text(plain, wrap_w)
            font_body = _load_font(body_sz)
            nick_sz = max(34, min(42, body_sz + 10))
            meta_sz = max(22, body_sz - 2)
            first_body_y = _estimate_page1_body_start(
                steamid64=steamid64,
                steam_level=steam_level,
                steam_created=steam_created,
                y0=AVATAR_TOP,
                badge_h=88,
            )
            avail1 = max(1, (PAGE_H - first_body_y - MARGIN) // line_h)
            body_top_p2 = AVATAR_TOP
            avail2 = max(1, (PAGE_H - body_top_p2 - MARGIN) // line_h)
            cap = avail1 + avail2
            if len(w) <= cap:
                chosen = (body_sz, line_h, wrap_w)
                wrapped = w
                break
        if chosen is None:
            body_sz, line_h, wrap_w = BODY_FONT_MIN, LINE_H_MIN, WRAP_MAX
            wrapped = _wrap_plain_text(plain, wrap_w)
            font_body = _load_font(body_sz)
            nick_sz = max(34, min(42, body_sz + 10))
            meta_sz = max(22, body_sz - 2)
            first_body_y = _estimate_page1_body_start(
                steamid64=steamid64,
                steam_level=steam_level,
                steam_created=steam_created,
                y0=AVATAR_TOP,
                badge_h=88,
            )
            avail1 = max(1, (PAGE_H - first_body_y - MARGIN) // line_h)
            avail2 = max(1, (PAGE_H - AVATAR_TOP - MARGIN) // line_h)
            cap = avail1 + avail2
            if len(wrapped) > cap:
                wrapped = wrapped[: max(0, cap - 1)]
                wrapped.append("… (часть отчёта не поместилась на 2 страницы)")
        else:
            body_sz, line_h, wrap_w = chosen
            font_body = _load_font(body_sz)
            nick_sz = max(34, min(42, body_sz + 10))
            meta_sz = max(22, body_sz - 2)

        first_body_y = _estimate_page1_body_start(
            steamid64=steamid64,
            steam_level=steam_level,
            steam_created=steam_created,
            y0=AVATAR_TOP,
            badge_h=88,
        )
        avail1 = max(1, (PAGE_H - first_body_y - MARGIN) // line_h)
        avail2 = max(1, (PAGE_H - AVATAR_TOP - MARGIN) // line_h)
        chunks = _split_two_chunks(wrapped, avail1, avail2)
        if not chunks:
            chunks = [wrapped]

        font_brand = _load_font(BRAND_FONT_SIZE)
        font_nick = _load_font(nick_sz)
        font_meta = _load_font(meta_sz)

        pages: list[bytes] = []

        use_pj = Pilmoji is not None

        for page_i, chunk in enumerate(chunks):
            bg = _cover_background(banner, PAGE_W, PAGE_H, y_shift=0)
            draw = ImageDraw.Draw(bg)
            y0 = AVATAR_TOP

            if page_i == 0:
                if avatar_png:
                    try:
                        av = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
                        av = _circle_avatar(av, AVATAR_SIZE)
                        bg.paste(av, (MARGIN, y0), av)
                    except Exception:
                        draw.ellipse(
                            (MARGIN, y0, MARGIN + AVATAR_SIZE, y0 + AVATAR_SIZE),
                            outline=(200, 200, 220),
                            width=3,
                        )
                else:
                    draw.ellipse(
                        (MARGIN, y0, MARGIN + AVATAR_SIZE, y0 + AVATAR_SIZE),
                        outline=(160, 170, 190),
                        width=3,
                    )

                tx = MARGIN + AVATAR_SIZE + 28
                ym = y0 + 108
                if steamid64:
                    ym += 34
                if steam_level is not None:
                    ym += 34
                if steam_created:
                    ym += 34
                badge = _make_rank_badge(rank_tier, leaderboard_rank)
                badge_y = max(y0 + AVATAR_SIZE + 12, ym + 8)
                bg.paste(badge, (MARGIN, badge_y), badge)
                sep_y_local = badge_y + badge.height + 24
                draw.line((MARGIN, sep_y_local, PAGE_W - MARGIN, sep_y_local), fill=(140, 150, 175), width=2)
                y_text_body = sep_y_local + 28
            else:
                y_text_body = AVATAR_TOP + 8

            def _draw_all_text(pj: object) -> None:
                _pj_text(pj, (MARGIN, LOGO_Y), "SmurfChekBot", (248, 250, 255), font_brand, use_pilmoji=use_pj)
                if page_i == 0:
                    tx = MARGIN + AVATAR_SIZE + 28
                    nick = nickname or f"account {account_id}"
                    _pj_text(pj, (tx, y0 + 6), nick[:48], (255, 255, 255), font_nick, use_pilmoji=use_pj)
                    _pj_text(pj, (tx, y0 + 62), f"account_id: {account_id}", (210, 218, 235), font_meta, use_pilmoji=use_pj)
                    ym = y0 + 108
                    if steamid64:
                        _pj_text(pj, (tx, ym), f"steamid64: {steamid64}", (200, 210, 228), font_meta, use_pilmoji=use_pj)
                        ym += 34
                    if steam_level is not None:
                        _pj_text(pj, (tx, ym), f"Steam level: {steam_level}", (200, 210, 228), font_meta, use_pilmoji=use_pj)
                        ym += 34
                    if steam_created:
                        _pj_text(pj, (tx, ym), f"Steam с: {steam_created}", (200, 210, 228), font_meta, use_pilmoji=use_pj)
                yt = y_text_body
                for line in chunk:
                    if yt > PAGE_H - MARGIN - line_h:
                        break
                    _pj_text(pj, (MARGIN, yt), line or " ", (232, 236, 246), font_body, use_pilmoji=use_pj)
                    yt += line_h

            if use_pj:
                with Pilmoji(bg) as pj:
                    _draw_all_text(pj)
            else:
                _draw_all_text(draw)

            buf = io.BytesIO()
            bg.save(buf, format="PNG", optimize=True)
            pages.append(buf.getvalue())

        return pages
    except Exception:
        return []
