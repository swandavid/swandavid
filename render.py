"""
Builds profile.svg from the stats today.py collects.

The card is generated rather than patched. The previous version kept a
hand-written SVG and swapped text by element id, which meant every value had a
hardcoded dot-count for alignment and any number that outgrew its budget
silently broke the layout. Laying it out from the data avoids that entirely.
"""

W, H = 1000, 545
PAD = 28

# Two palettes, swapped by prefers-color-scheme at view time. Everything below
# refers to these by CSS variable so the card reads correctly in either theme.
DARK = {
    'bg': '#0d1117', 'panel': '#161b22', 'border': '#30363d',
    'text': '#e6edf3', 'muted': '#8b949e', 'accent': '#56d364',
    'value': '#a5d6ff', 'add': '#3fb950', 'del': '#f85149', 'grid': '#21262d',
}
LIGHT = {
    'bg': '#ffffff', 'panel': '#f6f8fa', 'border': '#d0d7de',
    'text': '#1f2328', 'muted': '#59636e', 'accent': '#1a7f37',
    'value': '#0550ae', 'add': '#1a7f37', 'del': '#cf222e', 'grid': '#eaeef2',
}

FONT = "ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def esc(text):
    """
    Escapes the five XML metacharacters. Values come from the GitHub API, so a
    repository or language name containing '&' must not corrupt the document.
    """
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&apos;'))


def commas(number):
    return '{:,}'.format(number)


def theme_css():
    """
    Emits both palettes. Defaults to light on bare :root so a viewer with no
    preference still gets a fully specified card, then overrides for dark.
    """
    light = '\n'.join(f'    --{k}: {v};' for k, v in LIGHT.items())
    dark = '\n'.join(f'      --{k}: {v};' for k, v in DARK.items())
    return f''':root {{
{light}
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
{dark}
    }}
  }}'''


def panel(x, y, w, h, title=None):
    """
    A titled rounded container. The title sits on the panel's top edge.
    """
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
           f'fill="var(--panel)" stroke="var(--border)" stroke-width="1"/>']
    if title:
        out.append(f'<text x="{x + 16}" y="{y + 24}" class="label">{esc(title)}</text>')
    return out


def stat_tile(x, y, w, h, label, value, accent='var(--value)'):
    """
    One headline number with its caption underneath.
    """
    return [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="var(--panel)" stroke="var(--border)" stroke-width="1"/>',
        f'<text x="{x + w / 2:.0f}" y="{y + 38}" class="stat" fill="{accent}" '
        f'text-anchor="middle">{esc(value)}</text>',
        f'<text x="{x + w / 2:.0f}" y="{y + 58}" class="label" '
        f'text-anchor="middle">{esc(label)}</text>',
    ]


LEGEND_COLS = 3
LEGEND_ROW_H = 22


def language_segments(languages, shown=5):
    """
    The bar's segments: the largest `shown` languages plus an "Other" bucket, so
    the bar stays readable instead of dissolving into slivers.

    `shown` is 5 rather than 6 so the legend fills exactly two rows of three. A
    seventh segment wrapped to a third row that the panel had no height for,
    which is what made the section look cramped at the bottom.
    """
    top = languages[:shown]
    rest = sum(lang['pct'] for lang in languages[shown:])
    if rest > 0.5:
        return list(top) + [{'name': 'Other', 'pct': rest, 'color': '#6e7681'}]
    return list(top)


def language_panel_height(segments):
    """
    Height the languages panel needs for its bar plus however many legend rows
    the segments actually wrap to. Derived rather than hardcoded so adding a
    language can never crowd the panel's bottom edge again.
    """
    rows = -(-len(segments) // LEGEND_COLS) # ceiling division
    return 86 + (rows - 1) * LEGEND_ROW_H


def language_bar(x, y, w, segments):
    """
    A stacked bar of language share, with a legend beneath.
    """
    out, cursor = [], float(x)
    bar_h = 12
    out.append(f'<clipPath id="barclip"><rect x="{x}" y="{y}" width="{w}" '
               f'height="{bar_h}" rx="6"/></clipPath>')
    out.append(f'<g clip-path="url(#barclip)">')
    for seg in segments:
        seg_w = w * seg['pct'] / 100.0
        out.append(f'<rect x="{cursor:.2f}" y="{y}" width="{seg_w:.2f}" '
                   f'height="{bar_h}" fill="{esc(seg["color"])}"/>')
        cursor += seg_w
    out.append('</g>')

    # Legend laid out in fixed columns so long language names never collide.
    col_w = w / LEGEND_COLS
    for i, seg in enumerate(segments):
        col, row = i % LEGEND_COLS, i // LEGEND_COLS
        lx = x + col * col_w
        ly = y + 34 + row * LEGEND_ROW_H
        out.append(f'<circle cx="{lx + 5:.0f}" cy="{ly - 4}" r="5" fill="{esc(seg["color"])}"/>')
        out.append(f'<text x="{lx + 16:.0f}" y="{ly}" class="legend">'
                   f'{esc(seg["name"])} <tspan class="muted">{seg["pct"]:.1f}%</tspan></text>')
    return out


def activity_chart(x, y, w, h, weeks):
    """
    Weekly contribution counts for the last year as a bar chart.

    A commit total says how much; this says when, which is the part a single
    number can never carry.
    """
    if not weeks:
        return []
    peak = max(weeks) or 1
    gap = 2
    bar_w = max(2.0, (w - gap * (len(weeks) - 1)) / len(weeks))
    out = [f'<line x1="{x}" y1="{y + h}" x2="{x + w}" y2="{y + h}" '
           f'stroke="var(--grid)" stroke-width="1"/>']
    for i, count in enumerate(weeks):
        bx = x + i * (bar_w + gap)
        # Fade the older half so the eye lands on recent activity.
        opacity = 0.45 + 0.55 * (i / max(1, len(weeks) - 1))
        if count == 0:
            # A quiet week has to read as quiet. Given a year where most weeks
            # are near zero, a shared minimum bar height would make one commit
            # and none look the same, so nothing gets its own flat tick.
            out.append(f'<rect x="{bx:.2f}" y="{y + h - 1.5:.2f}" width="{bar_w:.2f}" '
                       f'height="1.5" fill="var(--muted)" opacity="{opacity * 0.5:.2f}"/>')
            continue
        bar_h = max(4.0, h * count / peak)
        out.append(f'<rect x="{bx:.2f}" y="{y + h - bar_h:.2f}" width="{bar_w:.2f}" '
                   f'height="{bar_h:.2f}" rx="1" fill="var(--add)" opacity="{opacity:.2f}"/>')
    return out


def render(data):
    """
    Assembles the whole card. `data` is the dict today.py builds.
    """
    out = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{W}px" height="{H}px" viewBox="0 0 {W} {H}" font-family="{FONT}" '
        f'role="img" aria-label="{esc(data["name"])} GitHub profile statistics">')

    out.append(f'''<style>
  {theme_css()}
  text {{ font-family: {FONT}; }}
  .name {{ font-family: {SANS}; font-size: 26px; font-weight: 700; fill: var(--text); }}
  .handle {{ font-size: 14px; fill: var(--accent); }}
  .role {{ font-family: {SANS}; font-size: 13px; fill: var(--muted); }}
  .key {{ font-size: 12.5px; fill: var(--accent); }}
  .val {{ font-size: 12.5px; fill: var(--value); }}
  .label {{ font-size: 11px; fill: var(--muted); letter-spacing: 0.6px; text-transform: uppercase; }}
  .stat {{ font-family: {SANS}; font-size: 27px; font-weight: 700; }}
  .legend {{ font-size: 12px; fill: var(--text); }}
  .muted {{ fill: var(--muted); }}
  .big {{ font-family: {SANS}; font-size: 30px; font-weight: 700; fill: var(--text); }}
</style>''')

    out.append(f'<rect width="{W}" height="{H}" rx="16" fill="var(--bg)" '
               f'stroke="var(--border)" stroke-width="1"/>')

    # ---- left column: portrait and identity --------------------------------
    col_w = 300
    lx = PAD

    if data.get('avatar'):
        size = 132
        ax, ay = lx + (col_w - size) / 2, PAD + 14
        out.append(f'<clipPath id="avatarclip"><circle cx="{ax + size / 2:.0f}" '
                   f'cy="{ay + size / 2:.0f}" r="{size / 2:.0f}"/></clipPath>')
        out.append(f'<image x="{ax:.0f}" y="{ay:.0f}" width="{size}" height="{size}" '
                   f'clip-path="url(#avatarclip)" preserveAspectRatio="xMidYMid slice" '
                   f'xlink:href="{data["avatar"]}"/>')
        out.append(f'<circle cx="{ax + size / 2:.0f}" cy="{ay + size / 2:.0f}" '
                   f'r="{size / 2:.0f}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>')
        cursor_y = ay + size + 38
    else:
        cursor_y = PAD + 60

    cx = lx + col_w / 2
    out.append(f'<text x="{cx:.0f}" y="{cursor_y}" class="name" text-anchor="middle">'
               f'{esc(data["name"])}</text>')
    out.append(f'<text x="{cx:.0f}" y="{cursor_y + 22}" class="handle" text-anchor="middle">'
               f'@{esc(data["login"])}</text>')
    out.append(f'<text x="{cx:.0f}" y="{cursor_y + 46}" class="role" text-anchor="middle">'
               f'{esc(data["role"])}</text>')
    out.append(f'<text x="{cx:.0f}" y="{cursor_y + 64}" class="role" text-anchor="middle">'
               f'{esc(data["company"])}</text>')

    out.append(f'<line x1="{lx + 30}" y1="{cursor_y + 88}" x2="{lx + col_w - 30}" '
               f'y2="{cursor_y + 88}" stroke="var(--border)"/>')

    # Spread the rows so this column bottoms out level with the right-hand one
    # instead of stopping short and leaving a dead band under the card.
    info_y = cursor_y + 116
    ROW_H = 25
    for key, value in data['info']:
        out.append(f'<text x="{lx + 14}" y="{info_y}" class="key">{esc(key)}</text>')
        out.append(f'<text x="{lx + col_w - 14}" y="{info_y}" class="val" '
                   f'text-anchor="end">{esc(value)}</text>')
        info_y += ROW_H

    # ---- divider -----------------------------------------------------------
    div_x = PAD + col_w + 12
    out.append(f'<line x1="{div_x}" y1="{PAD}" x2="{div_x}" y2="{H - PAD}" '
               f'stroke="var(--border)"/>')

    # ---- right column ------------------------------------------------------
    rx = div_x + 28
    rw = W - rx - PAD

    # stat tiles
    tile_h, gap = 74, 12
    tile_w = (rw - gap * 3) / 4
    tiles = [
        ('Repos', commas(data['repos'])),
        ('Contributed', commas(data['contrib'])),
        ('Commits', commas(data['commits'])),
        ('Followers', commas(data['followers'])),
    ]
    for i, (label, value) in enumerate(tiles):
        out += stat_tile(rx + i * (tile_w + gap), PAD, tile_w, tile_h, label, value)

    # lines of code
    loc_y = PAD + tile_h + gap
    loc_h = 78
    out += panel(rx, loc_y, rw, loc_h, 'Lines of code written')
    out.append(f'<text x="{rx + 16}" y="{loc_y + 62}" class="big">'
               f'{commas(data["loc_net"])}</text>')
    out.append(f'<text x="{rx + rw - 16}" y="{loc_y + 44}" text-anchor="end" '
               f'font-size="14px" fill="var(--add)">+{commas(data["loc_add"])}</text>')
    out.append(f'<text x="{rx + rw - 16}" y="{loc_y + 66}" text-anchor="end" '
               f'font-size="14px" fill="var(--del)">-{commas(data["loc_del"])}</text>')

    # languages
    lang_y = loc_y + loc_h + gap
    segments = language_segments(data['languages'])
    lang_h = language_panel_height(segments)
    out += panel(rx, lang_y, rw, lang_h, 'Languages by bytes')
    out += language_bar(rx + 16, lang_y + 38, rw - 32, segments)

    # activity
    act_y = lang_y + lang_h + gap
    act_h = H - act_y - PAD
    out += panel(rx, act_y, rw, act_h,
                 f'{commas(data["contributions"])} contributions in the last year')
    out += activity_chart(rx + 16, act_y + 42, rw - 32, act_h - 70, data['weeks'])

    out.append('</svg>')
    return '\n'.join(out)


def write(filename, data):
    with open(filename, 'w') as f:
        f.write(render(data))
