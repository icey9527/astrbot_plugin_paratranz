"""把项目进度画成一张卡片图，配色/字号对着 ParaTranz 官网（Bootstrap 那套）来。"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

# 官网 CSS：font-family:-apple-system,...,PingFang SC,...,Microsoft YaHei,sans-serif
# 服务器上只有 Noto Sans CJK，取 .ttc 里的 SC 那一份（index=2），别用默认的 JP。
FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_SC_INDEX = 2
FONT_FALLBACKS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# 官网 .progress / .progress-bar / bootstrap 调色板
C_BG = (255, 255, 255, 255)
C_TEXT = (33, 37, 41, 255)  # #212529
C_MUTED = (108, 117, 125, 255)  # #6c757d
C_LINE = (222, 226, 230, 255)  # #dee2e6
C_TRACK = (233, 236, 239, 255)  # #e9ecef
C_BLUE = (0, 123, 255, 255)  # #007bff stage1 已翻译
C_TEAL = (32, 201, 151, 255)  # #20c997 stage3 已检查
C_GREEN = (40, 167, 69, 255)  # #28a745 stage5 已审核
C_GRAY = (108, 117, 125, 255)  # #6c757d
C_WHITE = (255, 255, 255, 255)

SS = 1  # 不做超采样：PIL 直接按像素画字最锐利，缩放反而糊，还省一半时间

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    hit = _font_cache.get(key)
    if hit is not None:
        return hit
    font = None
    for path in (FONT_BOLD if bold else FONT_REG, *FONT_FALLBACKS):
        try:
            font = ImageFont.truetype(path, size, index=FONT_SC_INDEX)
            break
        except Exception:  # noqa: BLE001
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:  # noqa: BLE001
                continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _n(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pct(part: int, total: int) -> float:
    return (part / total * 100) if total else 0.0


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    ratio: float,
    color: tuple[int, int, int, int],
    label: str,
    label_font,
    radius: int | None = None,
) -> None:
    """官网那套：轨道 #e9ecef + 圆角 6，条内白字百分比。"""
    r = radius if radius is not None else max(1, h // 2)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=C_TRACK)
    fill_w = int(round(w * max(0.0, min(1.0, ratio))))
    if ratio >= 0.003:
        fill_w = max(fill_w, r * 2)
    else:
        fill_w = 0
    if fill_w <= 0:
        if label:
            draw.text(
                (x + max(6, r), y + h // 2 - 1),
                label,
                font=label_font,
                fill=C_MUTED,
                anchor="lm",
            )
        return
    draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=r, fill=color)
    if not label:
        return
    tw = _text_w(draw, label, label_font)
    if fill_w > tw + 12:
        draw.text(
            (x + fill_w // 2, y + h // 2 - 1),
            label,
            font=label_font,
            fill=C_WHITE,
            anchor="mm",
        )
    else:
        draw.text(
            (x + fill_w + 8, y + h // 2 - 1),
            label,
            font=label_font,
            fill=C_MUTED,
            anchor="lm",
        )


def render_project_card(
    project: dict,
    summary: dict,
    files_count: int,
    out_path: str,
    logo_path: str | None = None,
    subtitle: str = "",
) -> str:
    total = _n(summary.get("total"))
    translated = _n(summary.get("translated"))
    checked = _n(summary.get("checked"))
    reviewed = _n(summary.get("reviewed")) + _n(summary.get("locked"))
    disputed = _n(summary.get("disputed"))
    words = _n(summary.get("words")) or _n((project.get("stats") or {}).get("words"))
    name = project.get("name") or f"项目 {project.get('id')}"
    pid = project.get("id")

    W = 720
    pad = 34
    inner = W - pad * 2

    def s(v: float) -> int:
        return int(round(v * SS))

    img = Image.new("RGBA", (s(W), s(560)), C_BG)
    draw = ImageDraw.Draw(img)

    y = s(26)
    if logo_path and os.path.exists(logo_path):
        try:
            with Image.open(logo_path) as raw:
                logo = raw.convert("RGBA")
            lh = s(34)
            lw = max(1, int(logo.width * lh / logo.height))
            img.alpha_composite(logo.resize((lw, lh), Image.LANCZOS), (s(pad), y))
            y += lh + s(18)
        except Exception:  # noqa: BLE001
            pass

    draw.text(
        (s(pad), y),
        f"#{pid} {name}",
        font=_font(s(25), bold=True),
        fill=C_TEXT,
        anchor="la",
    )
    y += s(38)

    meta = [f"文件 {files_count}", f"有疑问 {disputed}"]
    if subtitle:
        meta.append(subtitle)
    draw.text(
        (s(pad), y), "　·　".join(meta), font=_font(s(14)), fill=C_MUTED, anchor="la"
    )
    y += s(28)

    draw.line((s(pad), y, s(W - pad), y), fill=C_LINE, width=max(1, s(1)))
    y += s(20)

    label_font = _font(s(15))
    value_font = _font(s(15), bold=True)
    bar_font = _font(s(11.5), bold=True)
    bar_h = s(16)
    bar_r = s(6)  # 官网 border-radius:6px，不是全圆头

    def plain_row(label: str, value: str) -> None:
        nonlocal y
        draw.text((s(pad), y + s(3)), label, font=label_font, fill=C_MUTED, anchor="la")
        draw.text(
            (s(W - pad), y + s(2)), value, font=value_font, fill=C_TEXT, anchor="ra"
        )
        y += s(31)

    def bar_row(label: str, value: int, color) -> None:
        nonlocal y
        ratio = (value / total) if total else 0.0
        draw.text((s(pad), y + s(4)), label, font=label_font, fill=C_MUTED, anchor="la")
        draw.text(
            (s(W - pad), y + s(3)),
            f"{value:,}",
            font=value_font,
            fill=C_TEXT,
            anchor="ra",
        )
        bar_x = s(pad + 118)
        bar_w = s(inner - 118 - 92)
        _bar(
            draw,
            bar_x,
            y + s(2),
            bar_w,
            bar_h,
            ratio,
            color,
            f"{ratio * 100:.2f}%",
            bar_font,
            bar_r,
        )
        y += s(31)

    if words:
        plain_row("总字数", f"{words:,}")
    plain_row("总条数", f"{total:,}")
    y += s(6)
    mode = review_mode(project)
    bar_row("已翻译条数", translated, C_BLUE)
    if mode == 1:  # 一次校对：翻译 + 审核（checked 与 reviewed 是同一件事）
        bar_row("已审核条数", reviewed or checked, C_GREEN)
    elif mode >= 2:  # 二次校对：翻译 → 检查 → 审核
        bar_row("已检查条数", checked, C_TEAL)
        bar_row("已审核条数", reviewed, C_GREEN)

    y += s(10)
    draw.line((s(pad), y, s(W - pad), y), fill=C_LINE, width=max(1, s(1)))
    y += s(14)
    draw.text(
        (s(W // 2), y),
        f"paratranz.cn/projects/{pid}",
        font=_font(s(13)),
        fill=C_MUTED,
        anchor="ma",
    )
    y += s(28)

    img = img.crop((0, 0, s(W), y))
    img = img.resize((W, max(1, int(round(y / SS)))), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path


def _clip(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """按像素宽度截断，末尾加省略号。"""
    text = (text or "").replace("\n", " ").replace("\t", " ").strip()
    if not text or _text_w(draw, text, font) <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(draw, text[:mid] + ell, font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


def _new_canvas(w: int, h: int):
    img = Image.new("RGBA", (w * SS, h * SS), C_BG)
    return img, ImageDraw.Draw(img)


def _save(img: Image.Image, out_path: str, width: int) -> str:
    h = img.height // SS
    img = img.crop((0, 0, width * SS, h * SS))
    if SS > 1:
        # 正好整数倍降采样，reduce 比 resize(LANCZOS) 快一个数量级
        img = img.reduce(SS)
    elif img.width != width:
        img = img.resize((width, h), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # compress_level=1：编码快很多，文件大一点无所谓，发完就删
    img.convert("RGB").save(out_path, "PNG", compress_level=1)
    return out_path


def _header(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    width: int,
    pad: int,
    title: str,
    subtitle: str,
    logo_path: str | None,
    bg: tuple[int, int, int, int] = C_BG,
    line_color: tuple[int, int, int, int] = C_LINE,
) -> int:
    def s(v: float) -> int:
        return int(round(v * SS))

    y = s(26)
    if logo_path and os.path.exists(logo_path):
        try:
            with Image.open(logo_path) as raw:
                logo = raw.convert("RGBA")
            lh = s(32)
            lw = max(1, int(logo.width * lh / logo.height))
            img.alpha_composite(logo.resize((lw, lh), Image.LANCZOS), (s(pad), y))
            y += lh + s(16)
        except Exception:  # noqa: BLE001
            pass
    draw.text(
        (s(pad), y), title, font=_font(s(22), bold=True), fill=C_TEXT, anchor="la"
    )
    y += s(34)
    if subtitle:
        draw.text((s(pad), y), subtitle, font=_font(s(13)), fill=C_MUTED, anchor="la")
        y += s(24)
    draw.line((s(pad), y, s(width - pad), y), fill=line_color, width=max(1, s(1)))
    return y + s(16)


def render_file_card(
    project: dict,
    summary: dict,
    files: list[dict],
    out_path: str,
    logo_path: str | None = None,
    limit: int = 10,
) -> str:
    """文件进度排行：每行一条进度条，对着官网文件列表的感觉来。"""
    width, pad = 720, 32
    shown = files[:limit]
    row_h = 30
    total = _n(summary.get("total"))
    translated = _n(summary.get("translated"))
    pct = _pct(translated, total)

    def s(v: float) -> int:
        return int(round(v * SS))

    height = 190 + row_h * (len(shown) + 1) + (30 if len(files) > limit else 0)
    img, draw = _new_canvas(width, height)
    y = _header(
        img,
        draw,
        width,
        pad,
        "文件进度",
        f"#{project.get('id')} {project.get('name')}　共 {_n(project.get('filesCount')) or len(files)} 个文件　按词条数排序",
        logo_path,
    )

    # 总进度条
    draw.text((s(pad), y), "总进度", font=_font(s(14)), fill=C_MUTED, anchor="la")
    draw.text(
        (s(width - pad), y - s(1)),
        f"{translated:,}/{total:,}（{pct:.2f}%）",
        font=_font(s(14), bold=True),
        fill=C_TEXT,
        anchor="ra",
    )
    _bar(
        draw,
        s(pad),
        y + s(20),
        s(width - pad * 2),
        s(14),
        (translated / total) if total else 0,
        C_BLUE,
        "",
        _font(s(11), bold=True),
        s(6),
    )
    y += s(46)

    # 表头
    name_font = _font(s(13))
    head_font = _font(s(12), bold=True)
    draw.text((s(pad), y), "文件", font=head_font, fill=C_MUTED, anchor="la")
    draw.text((s(pad + 328), y), "进度", font=head_font, fill=C_MUTED, anchor="la")
    draw.text((s(width - pad), y), "条数", font=head_font, fill=C_MUTED, anchor="ra")
    y += s(22)
    draw.line((s(pad), y, s(width - pad), y), fill=C_LINE, width=max(1, s(1)))
    y += s(6)

    value_font = _font(s(13))
    for f in shown:
        ft = _n(f.get("total"))
        done = _n(f.get("translated"))
        ratio = (done / ft) if ft else 0.0
        name = _clip(draw, str(f.get("name") or ""), name_font, s(316))
        draw.text((s(pad), y + s(6)), name, font=name_font, fill=C_TEXT, anchor="la")
        _bar(
            draw,
            s(pad + 328),
            y + s(6),
            s(212),
            s(13),
            ratio,
            C_BLUE if ratio >= 1 else C_GREEN,
            f"{ratio * 100:.0f}%",
            _font(s(10.5), bold=True),
            s(6),
        )
        draw.text(
            (s(width - pad), y + s(4)),
            f"{done:,}/{ft:,}",
            font=value_font,
            fill=C_MUTED,
            anchor="ra",
        )
        y += s(row_h)

    if len(files) > limit:
        draw.text(
            (s(width // 2), y + s(6)),
            f"…共 {len(files)} 个文件，只显示前 {limit} 个（/pt 文件 数量 可调）",
            font=_font(s(12)),
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(26)
    return _save(img.crop((0, 0, width * SS, y + s(20))), out_path, width)


def render_table_card(
    title: str,
    subtitle: str,
    rows: list[tuple[str, str]],
    out_path: str,
    logo_path: str | None = None,
    columns: tuple[str, str] = ("", ""),
    footer: str = "",
) -> str:
    """通用两列表格卡片（术语、成员、词条、搜索结果都用它）。"""
    width, pad = 720, 32
    row_h = 32
    shown = rows

    def s(v: float) -> int:
        return int(round(v * SS))

    height = 190 + row_h * (len(shown) + 1) + (28 if footer else 0)
    img, draw = _new_canvas(width, height)
    y = _header(img, draw, width, pad, title, subtitle, logo_path)

    cell_font = _font(s(13))
    head_font = _font(s(12), bold=True)
    col1_x = s(pad)
    col2_x = s(pad + 344)
    col_w1 = s(320)
    col_w2 = s(width - pad * 2 - 344)

    if columns[0] or columns[1]:
        draw.text((col1_x, y), columns[0], font=head_font, fill=C_MUTED, anchor="la")
        draw.text((col2_x, y), columns[1], font=head_font, fill=C_MUTED, anchor="la")
        y += s(22)
        draw.line((s(pad), y, s(width - pad), y), fill=C_LINE, width=max(1, s(1)))
        y += s(6)

    for left, right in shown:
        draw.text(
            (col1_x, y + s(7)),
            _clip(draw, left, cell_font, col_w1),
            font=cell_font,
            fill=C_TEXT,
            anchor="la",
        )
        draw.text(
            (col2_x, y + s(7)),
            _clip(draw, right, cell_font, col_w2),
            font=cell_font,
            fill=C_MUTED,
            anchor="la",
        )
        y += s(row_h)

    if footer:
        draw.text(
            (s(width // 2), y + s(8)),
            footer,
            font=_font(s(12)),
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(24)
    return _save(img.crop((0, 0, width * SS, y + s(20))), out_path, width)


# 官网 .stage-badge[data-stage] 的配色，直接照抄
STAGE_COLORS = {
    0: (204, 204, 204, 255),  # 未翻译
    1: (0, 123, 255, 255),  # 已翻译
    2: (255, 193, 7, 255),  # 有疑问
    3: (32, 201, 151, 255),  # 已检查
    5: (40, 167, 69, 255),  # 已审核
    9: (33, 37, 41, 255),  # 锁定
    -1: (73, 80, 87, 255),  # 隐藏
}
STAGE_NAMES_R = {
    0: "未翻译",
    1: "已翻译",
    2: "有疑问",
    3: "已检查",
    5: "已审核",
    9: "已锁定",
    -1: "已隐藏",
}

C_ORIGINAL = (128, 136, 146, 255)  # 原文灰：不可改


def render_strings_card(
    title: str,
    subtitle: str,
    items: list[dict],
    out_path: str,
    logo_path: str | None = None,
    footer: str = "",
) -> str:
    """通用词条卡片：一条一张白卡。

    卡片头是状态徽标（按官网 stage 配色）+ 词条 ID + 文件名；
    卡内两行：原文（灰色，表示只读不改）、译文（正文色，可改）。
    """
    width, pad = 720, 24
    bg = (245, 246, 248, 255)
    card_bg = (255, 255, 255, 255)
    body_bg = (247, 248, 250, 255)

    def s(v: float) -> int:
        return int(round(v * SS))

    badge_font = _font(s(11), bold=True)
    meta_font = _font(s(11.5))
    id_font = _font(s(12), bold=True)
    orig_font = _font(s(13))
    trans_font = _font(s(13.5))
    row_pad = s(12)
    inner = s(width - pad * 2) - row_pad * 2 - s(2)

    measure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    layouts = []
    total_h = 190
    for it in items:
        orig_lines = _wrap_text(
            measure, (it.get("original") or "").replace("\\n", " / "), orig_font, inner
        )[:3]
        trans_lines = _wrap_text(
            measure,
            (it.get("translation") or "（无译文）").replace("\\n", " / "),
            trans_font,
            inner,
        )[:3]
        card_h = (
            row_pad * 2
            + s(20)  # 徽标行
            + s(8)
            + int(orig_font.size * 1.5) * len(orig_lines)
            + int(trans_font.size * 1.5) * len(trans_lines)
            + s(6)
        )
        layouts.append((it, orig_lines, trans_lines, card_h))
        total_h += int(card_h + s(10))
    if footer:
        total_h += s(24)
    total_h += s(100)

    img = Image.new("RGBA", (s(width), total_h), bg)
    draw = ImageDraw.Draw(img)
    y = _header(
        img,
        draw,
        width,
        pad + 8,
        title,
        subtitle,
        logo_path,
        bg=bg,
        line_color=(226, 229, 234, 255),
    )

    for it, orig_lines, trans_lines, card_h in layouts:
        draw.rounded_rectangle(
            (s(pad), y, s(width - pad), y + card_h), radius=s(10), fill=card_bg
        )
        cx = s(pad) + row_pad
        ty = y + row_pad

        # 状态徽标
        stage = it.get("stage")
        try:
            stage = int(stage)
        except (TypeError, ValueError):
            stage = 1 if it.get("stage_name") == "已翻译" else 0
        color = STAGE_COLORS.get(stage, (108, 117, 125, 255))
        label = STAGE_NAMES_R.get(stage, str(it.get("stage_name") or "词条"))
        bw = _text_w(draw, label, badge_font) + s(14)
        draw.rounded_rectangle((cx, ty, cx + bw, ty + s(18)), radius=s(6), fill=color)
        draw.text(
            (cx + bw // 2, ty + s(9) - 1),
            label,
            font=badge_font,
            fill=C_WHITE,
            anchor="mm",
        )
        draw.text(
            (cx + bw + s(8), ty + s(2)),
            f"#{it.get('id')}",
            font=id_font,
            fill=C_TEXT,
            anchor="la",
        )
        fname = it.get("file_name") or ""
        if fname:
            used = draw.textlength(f"#{it.get('id')}", font=id_font)
            draw.text(
                (cx + bw + s(8) + used + s(8), ty + s(2)),
                _clip(draw, fname, meta_font, s(width - pad * 2 - int(bw) - s(80))),
                font=meta_font,
                fill=(150, 158, 168, 255),
                anchor="la",
            )
        ty += s(20) + s(6)

        # 原文（灰）
        body_h = (
            int(orig_font.size * 1.5) * len(orig_lines)
            + int(trans_font.size * 1.5) * len(trans_lines)
            + s(8)
        )
        draw.rounded_rectangle(
            (cx, ty, s(width - pad) - row_pad, ty + body_h),
            radius=s(7),
            fill=body_bg,
        )
        ly = ty + s(4)
        for runs in orig_lines:
            draw.text(
                (cx + s(8), ly),
                "".join(seg[0] for seg in runs),
                font=orig_font,
                fill=C_ORIGINAL,
                anchor="la",
            )
            ly += int(orig_font.size * 1.5)
        for runs in trans_lines:
            draw.text(
                (cx + s(8), ly),
                "".join(seg[0] for seg in runs),
                font=trans_font,
                fill=C_TEXT,
                anchor="la",
            )
            ly += int(trans_font.size * 1.5)
        y += card_h + s(10)

    if footer:
        draw.text(
            (s(width // 2), y - s(2)),
            footer,
            font=meta_font,
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(20)
    return _save(img.crop((0, 0, s(width), min(y, img.height))), out_path, width)


FA_SOLID = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "assets", "fa-solid-900.ttf"
)
FA_TROPHY = "\uf091"
FA_WIFI = "\uf1eb"
FA_CLOCK = "\uf017"

C_GOLD = (245, 166, 35, 255)
C_SILVER = (168, 176, 186, 255)
C_BRONZE = (201, 123, 60, 255)
C_ORANGE = (240, 163, 46, 255)
C_PP = (35, 178, 110, 255)
C_ONLINE = (24, 176, 240, 255)


def _fa(size: int):
    try:
        return ImageFont.truetype(FA_SOLID, size)
    except Exception:  # noqa: BLE001
        return _font(size)


def _pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    fill,
    font,
    icon: str | None = None,
) -> int:
    """画一个小圆角徽标，返回右边界。"""
    h = int(font.size * 1.55)
    tw = _text_w(draw, text, font)
    iw = int(font.size * 1.1) if icon else 0
    w = tw + iw + int(font.size * 1.3)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=int(h / 3.2), fill=fill)
    tx = x + int(font.size * 0.65)
    if icon:
        draw.text(
            (tx, y + h // 2 - 1), icon, font=_fa(font.size), fill=C_WHITE, anchor="lm"
        )
        tx += iw
    draw.text((tx, y + h // 2 - 1), text, font=font, fill=C_WHITE, anchor="lm")
    return x + w


def _avatar(path: str | None, size: int, label: str = "") -> Image.Image:
    """头像：方形裁切 + 15% 圆角，跟官网 .avatar.square 一致。"""
    img = None
    if path and os.path.exists(path):
        try:
            with Image.open(path) as raw:
                img = raw.convert("RGB")
        except Exception:  # noqa: BLE001
            img = None
    if img is None:
        img = Image.new("RGB", (size, size), (233, 236, 239))
        d = ImageDraw.Draw(img)
        ch = (label or "?")[:1].upper()
        d.text(
            (size // 2, size // 2),
            ch,
            font=_font(int(size * 0.42), bold=True),
            fill=C_MUTED,
            anchor="mm",
        )
    side = min(img.width, img.height)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize(
        (size, size), Image.LANCZOS
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 0.15), fill=255
    )
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _ago(iso: str) -> str:
    """把 lastVisit 变成「x 前在线」。"""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone

        text = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return ""
    if delta < 300:
        return "当前在线"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前在线"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前在线"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前在线"
    return f"{int(delta // 86400 // 30)} 个月前在线"


def render_member_card(
    project: dict,
    members: list[dict],
    out_path: str,
    logo_path: str | None = None,
    avatars: dict | None = None,
    limit: int = 10,
    total_members: int | None = None,
) -> str:
    """排行榜：奖牌/名次 + 头像 + 昵称 + PP 徽标 + 在线状态 + 译/编/审 + 加入日期。

    宽度按内容自动算（不留右边空白），高度按人数一行一行往下排。
    """
    shown = members[:limit]
    total_count = int(total_members or len(members))
    avatars = avatars or {}
    pad = 30
    rank_w = 44
    av_size = 58
    gap = 16
    row_h = 104
    min_w, max_w = 360, 900

    def s(v: float) -> int:
        return int(round(v * SS))

    # 量宽度和画图都用 SS 倍尺寸的字体，最后再换算回 1x
    name_font = _font(s(17), bold=True)
    nick_font = _font(s(15))
    stat_font = _font(s(13))
    date_font = _font(s(12))
    badge_font = _font(s(11.5), bold=True)
    rank_font = _font(s(19), bold=True)
    head_title_font = _font(s(22), bold=True)
    head_sub_font = _font(s(13))

    measure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    logo_img = None
    if logo_path and os.path.exists(logo_path):
        try:
            with Image.open(logo_path) as raw:
                logo_img = raw.convert("RGBA")
        except Exception:  # noqa: BLE001
            logo_img = None

    def pill_w(text: str, icon: str | None = None) -> int:
        tw = _text_w(measure, text, badge_font)
        iw = int(badge_font.size * 1.1) if icon else 0
        return tw + iw + int(badge_font.size * 1.3)

    # 第一遍：量内容，决定画布宽度
    text_w = _text_w(
        measure, f"#{project.get('id')} {project.get('name')}", head_sub_font
    )
    text_w = max(text_w, _text_w(measure, "排行榜", head_title_font))
    if logo_img is not None:
        text_w = max(text_w, int(logo_img.width * 32 / logo_img.height))
    for m in shown:
        user = m.get("user") or {}
        username = str(user.get("username") or m.get("uid") or "")
        nickname = str(user.get("nickname") or "").strip()
        w_name = _text_w(measure, username, name_font)
        if nickname and nickname != username:
            w_name += _text_w(measure, f"（{nickname}）", nick_font) + 6
        points = m.get("totalPoints")
        ptxt = f"PP {float(points):.2f}" if points else "PP 0"
        ago = (
            "当前在线"
            if user.get("isOnline")
            else _ago(str(user.get("lastVisit") or ""))
        )
        w_badge = pill_w(ptxt) + (
            8 + pill_w(ago, FA_WIFI if user.get("isOnline") else FA_CLOCK) if ago else 0
        )
        w_stat = _text_w(
            measure,
            f"翻译 {_n(m.get('translated')):,}　编辑 {_n(m.get('edited')):,}　审核 {_n(m.get('reviewed')):,}",
            stat_font,
        )
        w_date = _text_w(
            measure,
            f"{str(m.get('createdAt') or '')[:10].replace('-', '/')} 加入",
            date_font,
        )
        text_w = max(text_w, w_name, w_badge, w_stat, w_date)

    width_px = s(pad) * 2 + s(rank_w) + s(av_size) + s(gap) + int(text_w) + s(10)
    width = max(min_w, min(max_w, int(round(width_px / SS)) + 2))
    height = 190 + row_h * len(shown) + (26 if total_count > len(shown) else 0)
    img, draw = _new_canvas(width, height)
    y = _header(
        img,
        draw,
        width,
        pad,
        "排行榜",
        f"#{project.get('id')} {project.get('name')}　共 {total_count} 人　按贡献点数排序",
        logo_path,
    )

    av_x = s(pad + rank_w)
    text_x = av_x + s(av_size) + s(gap)
    room = s(width - pad) - text_x

    for idx, m in enumerate(shown, start=1):
        user = m.get("user") or {}
        username = str(user.get("username") or m.get("uid") or "")
        nickname = str(user.get("nickname") or "").strip()
        top = y
        if idx <= 3:
            color = (C_GOLD, C_SILVER, C_BRONZE)[idx - 1]
            draw.text(
                (s(pad) + s(12), top + s(av_size) // 2),
                FA_TROPHY,
                font=_fa(s(26)),
                fill=color,
                anchor="mm",
            )
        else:
            draw.text(
                (s(pad) + s(12), top + s(av_size) // 2),
                str(idx),
                font=rank_font,
                fill=C_ORANGE,
                anchor="mm",
            )

        av = _avatar(
            avatars.get(user.get("id") or m.get("uid")),
            s(av_size),
            username or nickname,
        )
        img.alpha_composite(av, (av_x, top))

        label = username
        draw.text((text_x, top - s(2)), label, font=name_font, fill=C_TEXT, anchor="la")
        if nickname and nickname != username:
            used = draw.textlength(label, font=name_font)
            left = int(room - used - s(6))
            if left > s(24):
                draw.text(
                    (text_x + used + s(6), top + s(2)),
                    _clip(draw, f"（{nickname}）", nick_font, left),
                    font=nick_font,
                    fill=C_MUTED,
                    anchor="la",
                )

        by = top + s(26)
        points = m.get("totalPoints")
        ptxt = f"PP {float(points):.2f}" if points else "PP 0"
        bx = _pill(draw, text_x, by, ptxt, C_PP, badge_font)
        ago = (
            "当前在线"
            if user.get("isOnline")
            else _ago(str(user.get("lastVisit") or ""))
        )
        if ago:
            _pill(
                draw,
                bx + s(8),
                by,
                ago,
                C_ONLINE if user.get("isOnline") else (154, 165, 177, 255),
                badge_font,
                FA_WIFI if user.get("isOnline") else FA_CLOCK,
            )

        draw.text(
            (text_x, top + s(50)),
            f"翻译 {_n(m.get('translated')):,}　编辑 {_n(m.get('edited')):,}　审核 {_n(m.get('reviewed')):,}",
            font=stat_font,
            fill=C_MUTED,
            anchor="la",
        )
        joined = str(m.get("createdAt") or "")[:10].replace("-", "/")
        if joined:
            draw.text(
                (text_x, top + s(72)),
                f"{joined} 加入",
                font=date_font,
                fill=(150, 158, 168, 255),
                anchor="la",
            )
        y += s(row_h)
        draw.line(
            (s(pad), y - s(6), s(width - pad), y - s(6)),
            fill=(243, 245, 247, 255),
            width=max(1, s(1)),
        )

    if total_count > len(shown):
        draw.text(
            (s(width // 2), y + s(2)),
            f"…共 {total_count} 人，只显示前 {len(shown)} 名（/pt 成员 数量 可调）",
            font=_font(s(12)),
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(22)
    return _save(img.crop((0, 0, width * SS, y + s(16))), out_path, width)


def _diff_segments(old: str, new: str) -> list[tuple[str, str]]:
    """字符级差分，返回 [(文本, 类型)]，类型 eq / del / ins。"""
    import difflib

    sm = difflib.SequenceMatcher(None, old or "", new or "", autojunk=False)
    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append((old[i1:i2], "eq"))
        elif tag == "delete":
            out.append((old[i1:i2], "del"))
        elif tag == "insert":
            out.append((new[j1:j2], "ins"))
        else:  # replace
            if i2 > i1:
                out.append((old[i1:i2], "del"))
            if j2 > j1:
                out.append((new[j1:j2], "ins"))
    return out


_char_w_cache: dict[tuple[int, str], float] = {}


def _char_w(font, ch: str) -> float:
    """按字符缓存宽度，比每格都调一次 textlength 快得多。"""
    key = (id(font), ch)
    val = _char_w_cache.get(key)
    if val is None:
        try:
            val = font.getlength(ch)
        except Exception:  # noqa: BLE001
            val = float(font.size)
        _char_w_cache[key] = val
    return val


def _layout_runs(draw, segs: list[tuple[str, str]], font, max_w: int):
    """把差分片段按像素宽度折行，返回 [[(文本, 类型, 宽度)]]。"""
    lines: list[list[list]] = []
    cur: list[list] = []
    cur_w = 0.0
    for text, kind in segs:
        for ch in text:
            w = _char_w(font, ch)
            if cur and cur_w + w > max_w:
                lines.append(cur)
                cur, cur_w = [], 0.0
            if cur and cur[-1][1] == kind:
                cur[-1][0] += ch
                cur[-1][2] += w
            else:
                cur.append([ch, kind, w])
            cur_w += w
    if cur:
        lines.append(cur)
    return lines


def render_diff_card(
    title: str,
    subtitle: str,
    items: list[dict],
    out_path: str,
    logo_path: str | None = None,
    footer: str = "",
    author: str = "",
) -> str:
    """改动汇报图：仿官网「历史记录」页——灰底、一条一张白卡、卡里一行行内红绿差分。

    - 卡片头：编辑者 + 编辑了词条 <ID>
    - 卡片体：浅灰圆角块，里面是整段文字的行内差分（删除红底删除线，新增绿底）
    """
    width, pad = 720, 24
    bg = (245, 246, 248, 255)
    card_bg = (255, 255, 255, 255)
    body_bg = (247, 248, 250, 255)
    c_del_bg = (255, 221, 225, 255)
    c_del_fg = (176, 0, 32, 255)
    c_ins_bg = (205, 255, 216, 255)
    c_ins_fg = (17, 99, 41, 255)

    def s(v: float) -> int:
        return int(round(v * SS))

    def pretty(text: str) -> str:
        return (text or "").strip()

    body_font = _font(s(13.5))
    head_font = _font(s(12.5))
    meta_font = _font(s(12))
    line_h = int(body_font.size * 1.46)
    inner = s(width - pad * 2)
    card_pad = s(14)
    head_h = s(26)
    body_pad = s(10)
    gap = s(12)

    measure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    layouts: list[tuple[dict, list[list[list]]]] = []
    total_h = 190
    for it in items:
        segs = _diff_segments(pretty(it.get("old")), pretty(it.get("new")))
        lines = _layout_runs(measure, segs, body_font, inner - s(2 * 14) - s(2 * 10))
        layouts.append((it, lines))
        total_h += int(card_pad * 2 + head_h + body_pad * 2 + line_h * len(lines) + gap)
    if footer:
        total_h += s(24)
    total_h += s(120)  # 留点余量，最后按实际用掉的高度裁

    img = Image.new("RGBA", (s(width), total_h), bg)
    draw = ImageDraw.Draw(img)
    y = _header(
        img,
        draw,
        width,
        pad + 8,
        title,
        subtitle,
        logo_path,
        bg=bg,
        line_color=(226, 229, 234, 255),
    )

    for it, lines in layouts:
        sid = it.get("id")
        body_h = body_pad * 2 + line_h * len(lines)
        card_h = card_pad * 2 + head_h + body_h
        draw.rounded_rectangle(
            (s(pad), y, s(width - pad), y + card_h), radius=s(10), fill=card_bg
        )

        # 卡片头：小圆点头像 + 谁编辑了哪条 + 右侧编号
        av = s(20)
        av_x = s(pad) + card_pad
        av_y = y + card_pad + s(2)
        draw.ellipse((av_x, av_y, av_x + av, av_y + av), fill=(232, 240, 254, 255))
        draw.text(
            (av_x + av // 2, av_y + av // 2),
            "\uf304",  # fa-pen
            font=_fa(s(10)),
            fill=(26, 115, 232, 255),
            anchor="mm",
        )
        who = author or "提交"
        tx = av_x + av + s(10)
        draw.text((tx, av_y + s(1)), who, font=head_font, fill=C_TEXT, anchor="la")
        used = draw.textlength(who, font=head_font)
        draw.text(
            (tx + used + s(4), av_y + s(1)),
            f"编辑了词条 {sid}",
            font=head_font,
            fill=(110, 118, 129, 255),
            anchor="la",
        )

        # 卡片体：灰底圆角块 + 行内差分
        by = y + card_pad + head_h
        draw.rounded_rectangle(
            (av_x, by, s(width - pad) - card_pad, by + body_h),
            radius=s(8),
            fill=body_bg,
        )
        ty = by + body_pad
        for runs in lines:
            x = av_x + body_pad + s(2)
            for text, kind, w in runs:
                if kind != "eq":
                    fill = c_del_bg if kind == "del" else c_ins_bg
                    draw.rounded_rectangle(
                        (x - s(1), ty - s(1), x + w + s(1), ty + line_h - s(3)),
                        radius=s(2),
                        fill=fill,
                    )
                fg = C_TEXT
                if kind == "del":
                    fg = c_del_fg
                elif kind == "ins":
                    fg = c_ins_fg
                draw.text((x, ty), text, font=body_font, fill=fg, anchor="la")
                if kind == "del":
                    mid = ty + int(body_font.size * 0.62)
                    draw.line((x, mid, x + w, mid), fill=c_del_fg, width=max(1, s(1)))
                x += w
            ty += line_h
        y += card_h + gap

    if footer:
        draw.text(
            (s(width // 2), y - s(2)),
            footer,
            font=meta_font,
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(20)
    return _save(img.crop((0, 0, s(width), min(y, img.height))), out_path, width)


def review_mode(project: dict) -> int:
    """项目校对模式：0 无需校对，1 一次校对，2 二次校对。"""
    raw = project.get("reviewMode")
    if raw is None:
        raw = (project.get("extra") or {}).get("reviewMode")
    try:
        return max(0, min(int(raw), 2))
    except (TypeError, ValueError):
        return 2


def _fmt_day(iso: str) -> str:
    """2026-08-25T03:37:12.168Z -> 2026/08/25"""
    text = str(iso or "")[:10]
    return text.replace("-", "/")


def _fmt_time(iso: str) -> str:
    """2026-10-23T20:48:00 -> 10-23 20:48"""
    text = str(iso or "")
    if len(text) >= 16:
        return f"{text[5:7]}-{text[8:10]} {text[11:16]}"
    return text[:10]


def render_task_card(
    project: dict,
    tasks: list[dict],
    out_path: str,
    logo_path: str | None = None,
    footer: str = "",
) -> str:
    """任务进度图：仿官网任务页——⭐ + 标题 + 创建时间 + 三个百分比徽标 + 文件/词条/字数。"""
    width, pad = 720, 24
    bg = (245, 246, 248, 255)
    card_bg = (255, 255, 255, 255)
    c_run = (224, 49, 49, 255)
    c_done = (32, 150, 90, 255)
    c_trans = (0, 123, 255, 255)  # stage1 已翻译
    c_check = (32, 201, 151, 255)  # stage3 已检查
    c_review = (40, 167, 69, 255)  # stage5 已审核

    def s(v: float) -> int:
        return int(round(v * SS))

    title_font = _font(s(14.5), bold=True)
    meta_font = _font(s(12))
    badge_font = _font(s(11.5), bold=True)
    pad_card = s(14)
    inner = s(width - pad * 2) - pad_card * 2
    line_h = int(title_font.size * 1.45)

    measure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    layouts = []
    total_h = 190
    for t in tasks:
        title_lines = _wrap_text(measure, str(t.get("title") or ""), title_font, inner)
        card_h = pad_card * 2 + line_h * len(title_lines) + s(24) + s(20)
        layouts.append((t, title_lines, card_h))
        total_h += int(card_h + s(10))
    if footer:
        total_h += s(24)
    total_h += s(120)

    img = Image.new("RGBA", (s(width), total_h), bg)
    draw = ImageDraw.Draw(img)
    y = _header(
        img,
        draw,
        width,
        pad + 8,
        "任务进度",
        f"#{project.get('id')} {project.get('name')}　共 {len(tasks)} 个任务",
        logo_path,
        bg=bg,
        line_color=(226, 229, 234, 255),
    )

    for t, title_lines, card_h in layouts:
        draw.rounded_rectangle(
            (s(pad), y, s(width - pad), y + card_h), radius=s(10), fill=card_bg
        )
        cx = s(pad) + pad_card
        ty = y + pad_card
        # ⭐ + 标题（最多两行，超出省略）
        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            last = "".join(seg[0] for seg in title_lines[1])
            title_lines[1] = [(last[:-1] + "…", "eq", title_lines[1][-1][2])]
        draw.text(
            (cx, ty), "\uf005", font=_fa(s(12)), fill=(250, 190, 40, 255), anchor="la"
        )
        ttx = cx + s(20)
        for i, runs in enumerate(title_lines):
            text = "".join(seg[0] for seg in runs)
            draw.text(
                (ttx, ty + i * line_h), text, font=title_font, fill=C_TEXT, anchor="la"
            )
        created = _fmt_day(t.get("createdAt"))
        draw.text(
            (s(width - pad) - pad_card, ty + s(2)),
            f"创建于 {created}",
            font=meta_font,
            fill=(130, 138, 148, 255),
            anchor="ra",
        )
        ty += line_h * len(title_lines) + s(6)

        # 状态 + 三个百分比徽标
        files = t.get("taskFiles") or []
        total = sum(_n(f.get("total")) for f in files) or 1
        translated = sum(_n(f.get("translated")) for f in files)
        checked = sum(_n(f.get("checked")) for f in files)
        reviewed = sum(_n(f.get("reviewed")) for f in files)
        words = sum(_n(f.get("words")) for f in files)
        status = str(t.get("status"))
        status_txt = "进行中" if status in ("0", "None", "") else "已完成"
        bx = cx
        bx = _pill(
            draw,
            bx,
            ty,
            status_txt,
            c_run if status in ("0", "None", "") else c_done,
            badge_font,
        )
        bx = _pill(
            draw, bx + s(6), ty, f"{translated / total * 100:.2f}%", c_trans, badge_font
        )
        mode = review_mode(project)
        if mode == 1:
            _pill(
                draw,
                bx + s(6),
                ty,
                f"{(reviewed or checked) / total * 100:.2f}%",
                c_check,
                badge_font,
            )
        elif mode >= 2:
            bx = _pill(
                draw,
                bx + s(6),
                ty,
                f"{checked / total * 100:.2f}%",
                c_check,
                badge_font,
            )
            _pill(
                draw,
                bx + s(6),
                ty,
                f"{reviewed / total * 100:.2f}%",
                c_review,
                badge_font,
            )
        ty += s(24)

        stats = f"共 {len(files)} 文件 / {total} 词条 / {words} 字"
        draw.text(
            (s(width - pad) - pad_card, ty + s(2)),
            stats,
            font=meta_font,
            fill=(130, 138, 148, 255),
            anchor="ra",
        )
        y += card_h + s(10)

    if footer:
        draw.text(
            (s(width // 2), y - s(2)),
            footer,
            font=meta_font,
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(20)
    return _save(img.crop((0, 0, s(width), min(y, img.height))), out_path, width)


def _wrap_text(draw, text: str, font, max_w: int) -> list[list[list]]:
    """按像素宽度把纯文本折行，返回 _layout_runs 同构的结构。"""
    if not text:
        return [[["", "eq", 0]]]
    return _layout_runs(draw, [(text, "eq")], font, max_w)


def render_records_card(
    records: list[dict],
    out_path: str,
    logo_path: str | None = None,
    page: int = 1,
    pages: int = 1,
    total: int = 0,
    title: str = "提交记录",
) -> str:
    """提交记录列表：一行一条（时间、条数、发起人、确认人、哈希），底部分页信息。"""
    width, pad = 720, 24
    bg = (245, 246, 248, 255)
    card_bg = (255, 255, 255, 255)
    row_h = 56

    def s(v: float) -> int:
        return int(round(v * SS))

    head_font = _font(s(12), bold=True)
    cell_font = _font(s(13))
    hash_font = _font(s(13), bold=True)
    meta_font = _font(s(12))

    height = 190 + row_h * max(1, len(records)) + 40
    img = Image.new("RGBA", (s(width), s(height + 80)), bg)
    draw = ImageDraw.Draw(img)
    y = _header(
        img,
        draw,
        width,
        pad + 8,
        title,
        f"共 {total} 条　第 {page}/{pages} 页",
        logo_path,
        bg=bg,
        line_color=(226, 229, 234, 255),
    )

    if records:
        draw.rounded_rectangle(
            (s(pad), y, s(width - pad), y + s(row_h * len(records))),
            radius=s(10),
            fill=card_bg,
        )
        cx = s(pad) + s(14)
        right = s(width - pad) - s(14)
        for i, rec in enumerate(records):
            top = y + i * s(row_h)
            if i:
                draw.line(
                    (cx, top, right, top), fill=(238, 240, 243, 255), width=max(1, s(1))
                )
            label = {
                "pending": "待确认",
                "committed": "已写入",
                "reverted": "已撤回",
                "cancelled": "已取消",
                "expired": "已过期",
                "failed": "失败",
                "revert_failed": "撤回失败",
            }.get(str(rec.get("status")), str(rec.get("status")))
            when = _fmt_time(rec.get("approved_at") or rec.get("at") or "")
            count = len(rec.get("items") or [])
            who = f"{rec.get('by_name') or rec.get('by')} → {rec.get('approver_name') or rec.get('approver') or '未确认'}"
            draw.text((cx, top + s(10)), when, font=cell_font, fill=C_TEXT, anchor="la")
            draw.text(
                (cx, top + s(31)),
                label,
                font=meta_font,
                fill=(120, 128, 138, 255),
                anchor="la",
            )
            draw.text(
                (cx + s(92), top + s(10)),
                f"{count} 条",
                font=head_font,
                fill=C_TEXT,
                anchor="la",
            )
            draw.text(
                (cx + s(92), top + s(31)),
                _clip(draw, who, meta_font, s(300)),
                font=meta_font,
                fill=(120, 128, 138, 255),
                anchor="la",
            )
            draw.text(
                (right, top + s(20)),
                f"#{rec.get('hash')}",
                font=hash_font,
                fill=(26, 115, 232, 255),
                anchor="ra",
            )
    else:
        draw.text(
            (s(width // 2), y + s(30)),
            "还没有提交记录",
            font=cell_font,
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(72)

    y += s(row_h) * max(1, len(records)) + s(20)
    return _save(img.crop((0, 0, s(width), min(y, img.height))), out_path, width)


def render_multi_project_card(
    items: list[dict],
    out_path: str,
    logo_path: str | None = None,
    footer: str = "",
) -> str:
    """多项目总览：一个群绑了多个项目时，一屏把每个项目的进度都列出来。

    items: [{"project": dict, "summary": dict, "files_count": int}]
    """
    width, pad = 720, 24
    bg = (245, 246, 248, 255)
    card_bg = (255, 255, 255, 255)

    def s(v: float) -> int:
        return int(round(v * SS))

    title_font = _font(s(14), bold=True)
    meta_font = _font(s(12))
    label_font = _font(s(12))
    val_font = _font(s(12), bold=True)
    pad_card = s(14)
    inner = s(width - pad * 2) - pad_card * 2
    line_h = int(title_font.size * 1.45)
    row_h = s(20)

    measure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    layouts = []
    total_h = 190
    for it in items:
        proj = it.get("project") or {}
        title_lines = _wrap_text(
            measure, f"#{proj.get('id')} {proj.get('name') or ''}", title_font, inner
        )[:2]
        mode = review_mode(proj)
        rows = 1 + (0 if mode == 0 else (1 if mode == 1 else 2))
        card_h = pad_card * 2 + line_h * len(title_lines) + s(8) + row_h * rows + s(16)
        layouts.append((it, title_lines, mode, rows, card_h))
        total_h += int(card_h + s(10))
    if footer:
        total_h += s(24)
    total_h += s(100)

    img = Image.new("RGBA", (s(width), total_h), bg)
    draw = ImageDraw.Draw(img)
    y = _header(
        img,
        draw,
        width,
        pad + 8,
        "本群绑定项目",
        f"共 {len(items)} 个，下面按项目分别列进度",
        logo_path,
        bg=bg,
        line_color=(226, 229, 234, 255),
    )

    for it, title_lines, mode, _rows, card_h in layouts:
        proj = it.get("project") or {}
        summ = it.get("summary") or {}
        total = _n(summ.get("total")) or 1
        draw.rounded_rectangle(
            (s(pad), y, s(width - pad), y + card_h), radius=s(10), fill=card_bg
        )
        cx = s(pad) + pad_card
        right = s(width - pad) - pad_card
        ty = y + pad_card
        for i, runs in enumerate(title_lines):
            draw.text(
                (cx, ty + i * line_h),
                "".join(seg[0] for seg in runs),
                font=title_font,
                fill=C_TEXT,
                anchor="la",
            )
        ty += line_h * len(title_lines) + s(4)

        def bar_row(label: str, value: int, color) -> None:
            nonlocal ty
            draw.text(
                (cx, ty + s(3)), label, font=label_font, fill=C_MUTED, anchor="la"
            )
            draw.text(
                (right, ty + s(2)),
                f"{value:,}（{value / total * 100:.2f}%）",
                font=val_font,
                fill=C_TEXT,
                anchor="ra",
            )
            bar_x = cx + s(96)
            bar_w = right - s(150) - bar_x
            _bar(
                draw,
                bar_x,
                ty + s(4),
                max(s(40), bar_w),
                s(13),
                value / total,
                color,
                "",
                _font(s(10), bold=True),
                s(6),
            )
            ty += row_h

        bar_row("已翻译", _n(summ.get("translated")), C_BLUE)
        if mode == 1:
            bar_row(
                "已审核", _n(summ.get("reviewed")) or _n(summ.get("checked")), C_GREEN
            )
        elif mode >= 2:
            bar_row("已检查", _n(summ.get("checked")), C_TEAL)
            bar_row("已审核", _n(summ.get("reviewed")), C_GREEN)

        info = (
            f"文件 {_n(it.get('files_count'))}　词条 {_n(summ.get('total')):,}"
            f"　字数 {_n(summ.get('words')):,}　有疑问 {_n(summ.get('disputed'))}"
        )
        draw.text(
            (cx, ty + s(2)),
            _clip(draw, info, meta_font, inner),
            font=meta_font,
            fill=(130, 138, 148, 255),
            anchor="la",
        )
        y += card_h + s(10)

    if footer:
        draw.text(
            (s(width // 2), y - s(2)),
            footer,
            font=meta_font,
            fill=C_MUTED,
            anchor="ma",
        )
        y += s(20)
    return _save(img.crop((0, 0, s(width), min(y, img.height))), out_path, width)
