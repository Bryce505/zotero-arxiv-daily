"""Render one digest three ways.

Email HTML and web HTML are not the same medium.  Gmail strips @font-face and
ignores CSS variables, Outlook renders through Word (no flexbox, no grid), and
Gmail clips a message past ~102KB.  So the email body is a table-based,
inline-styled summary that leads with the papers worth reading first, while
the archived web page is free to use the full stylesheet.
"""

from dataclasses import dataclass, field
from datetime import date
from html import escape

from .extract import FieldSpec
from .protocol import Paper
from .weeknum import week_label, week_window

EMAIL_MAX_BYTES = 102_000
_TRUNCATION_NOTE = "完整周报见附件。"


@dataclass
class Digest:
    label: str
    start: date
    end: date
    clusters: list[tuple[str, list[Paper]]] = field(default_factory=list)
    backfill: list[Paper] = field(default_factory=list)
    top_picks: list[Paper] = field(default_factory=list)
    needs_manual: list[Paper] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(papers) for _, papers in self.clusters) + len(self.backfill)


def build_digest(papers: list[Paper], backfill: list[Paper], anchor: date, top_n: int = 3) -> Digest:
    """Group *papers* into the shape the renderers consume."""
    start, end = week_window(anchor)
    fresh = [p for p in papers if not p.is_backfill]

    grouped: dict[str, list[Paper]] = {}
    for paper in fresh:
        grouped.setdefault(paper.cluster or "未分类", []).append(paper)
    for bucket in grouped.values():
        bucket.sort(key=lambda p: -(p.score or 0.0))

    ordered = sorted(grouped.items(), key=lambda kv: (-max((p.score or 0.0) for p in kv[1]), kv[0]))

    return Digest(
        label=week_label(anchor),
        start=start,
        end=end,
        clusters=ordered,
        backfill=sorted(backfill, key=lambda p: -(p.cited_by_count or 0)),
        top_picks=sorted(fresh, key=lambda p: -(p.score or 0.0))[:top_n],
        needs_manual=[p for p in fresh if p.oa_status != "open"],
    )


def _byline(paper: Paper) -> str:
    bits = []
    if paper.journal:
        bits.append(paper.journal)
    if paper.pub_date:
        bits.append(paper.pub_date.isoformat())
    if paper.authors:
        bits.append(paper.authors[0] + (" et al." if len(paper.authors) > 1 else ""))
    return " · ".join(bits)


# --------------------------------------------------------------------------- markdown

def _markdown_entry(paper: Paper, fields: list[FieldSpec], extra: str = "") -> list[str]:
    link = paper.doi_url or paper.url
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    lines = [f"### {paper.title}", "", meta, "", f"DOI: <{link}>", ""]
    for spec in fields:
        value = (paper.extraction or {}).get(spec.key, "")
        if value:
            lines += [f"**{spec.label}：** {value}", ""]
    return lines


def render_markdown(digest: Digest, fields: list[FieldSpec]) -> str:
    lines = [
        f"# CMC 文献周报 {digest.label}",
        "",
        f"**覆盖期：** {digest.start.isoformat()} ~ {digest.end.isoformat()}　**共 {digest.total} 篇**",
        "",
    ]

    if digest.top_picks:
        lines += ["## 本周优先读", ""]
        for paper in digest.top_picks:
            lines.append(f"- [{paper.title}]({paper.doi_url or paper.url})　{_byline(paper)}")
        lines.append("")

    for name, papers in digest.clusters:
        lines += [f"## {name}（{len(papers)} 篇）", ""]
        for paper in papers:
            lines += _markdown_entry(paper, fields)

    if digest.backfill:
        lines += ["## 经典补位", "", "> 本周新文献不足，以下为依据文献库检索到的高引经典文献。", ""]
        for paper in digest.backfill:
            lines += _markdown_entry(paper, fields, extra=f"被引 {paper.cited_by_count or 0}")

    if digest.needs_manual:
        lines += ["## 需人工取全文", ""]
        for paper in digest.needs_manual:
            lines.append(f"- [{paper.title}]({paper.doi_url or paper.url})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- web html

_WEB_CSS = """
:root{--bg:#FBFAF7;--fg:#1F2421;--muted:#5C6660;--rule:#E2E0D8;--accent:#0E5E5A;--card:#FFFFFF}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#14181A;--fg:#E8E6E0;--muted:#9AA5A0;--rule:#2A3033;--accent:#5FB8B2;--card:#1B2124}}
:root[data-theme="dark"]{--bg:#14181A;--fg:#E8E6E0;--muted:#9AA5A0;--rule:#2A3033;--accent:#5FB8B2;--card:#1B2124}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
 font-family:"Noto Sans SC","Helvetica Neue",Arial,sans-serif;line-height:1.7}
main{max-width:52rem;margin:0 auto}
h1{font-size:1.9rem;margin:0 0 .3rem}
h2{font-size:1.25rem;margin:2.5rem 0 .75rem;padding-bottom:.35rem;border-bottom:2px solid var(--accent)}
h3{font-size:1.05rem;margin:1.6rem 0 .3rem}
.meta{color:var(--muted);font-size:.86rem}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:1rem 1.15rem;margin:.9rem 0}
.field{margin:.5rem 0}
.field b{color:var(--accent)}
a{color:var(--accent)}
.pill{display:inline-block;margin-left:.5rem;font-size:.75rem;padding:.1rem .5rem;border:1px solid var(--rule);border-radius:999px;color:var(--muted)}
""".strip()


def _html_card(paper: Paper, fields: list[FieldSpec], extra: str = "") -> str:
    link = escape(paper.doi_url or paper.url)
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    rows = [
        f'<div class="card"><h3><a href="{link}">{escape(paper.title)}</a></h3>',
        f'<p class="meta">{escape(meta)}</p>',
    ]
    for spec in fields:
        value = (paper.extraction or {}).get(spec.key, "")
        if value:
            rows.append(f'<div class="field"><b>{escape(spec.label)}</b>：{escape(value)}</div>')
    rows.append("</div>")
    return "".join(rows)


def render_web_html(digest: Digest, fields: list[FieldSpec]) -> str:
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>CMC 文献周报 {escape(digest.label)}</title>",
        f"<style>{_WEB_CSS}</style></head><body><main>",
        f"<h1>CMC 文献周报 {escape(digest.label)}</h1>",
        f'<p class="meta">覆盖期 {digest.start.isoformat()} ~ {digest.end.isoformat()}　共 {digest.total} 篇</p>',
    ]

    if digest.top_picks:
        parts.append("<h2>本周优先读</h2><ol>")
        for paper in digest.top_picks:
            link = escape(paper.doi_url or paper.url)
            parts.append(
                f'<li><a href="{link}">{escape(paper.title)}</a> '
                f'<span class="meta">{escape(_byline(paper))}</span></li>'
            )
        parts.append("</ol>")

    for name, papers in digest.clusters:
        parts.append(f'<h2>{escape(name)}<span class="pill">{len(papers)} 篇</span></h2>')
        parts.extend(_html_card(p, fields) for p in papers)

    if digest.backfill:
        parts.append(
            '<h2>经典补位</h2><p class="meta">本周新文献不足，以下为依据文献库检索到的高引经典文献。</p>'
        )
        parts.extend(_html_card(p, fields, extra=f"被引 {p.cited_by_count or 0}") for p in digest.backfill)

    if digest.needs_manual:
        parts.append("<h2>需人工取全文</h2><ul>")
        for paper in digest.needs_manual:
            parts.append(f'<li><a href="{escape(paper.doi_url or paper.url)}">{escape(paper.title)}</a></li>')
        parts.append("</ul>")

    parts.append("</main></body></html>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- email html

_EMAIL_WRAP_OPEN = (
    '<div style="margin:0;padding:16px;background:#FBFAF7;'
    "font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#1F2421\">"
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
    'style="max-width:640px;margin:0 auto;background:#FFFFFF;border:1px solid #E2E0D8;border-radius:8px">'
    '<tr><td style="padding:20px 22px">'
)
_EMAIL_WRAP_CLOSE = "</td></tr></table></div>"


def _email_heading(text: str) -> str:
    return (
        f'<p style="margin:18px 0 6px;font-size:15px;font-weight:700;'
        f'color:#0E5E5A;border-bottom:2px solid #0E5E5A;padding-bottom:4px">{text}</p>'
    )


def _email_pick(paper: Paper, fields: list[FieldSpec]) -> str:
    link = escape(paper.doi_url or paper.url)
    extraction = paper.extraction or {}
    first_field = next((extraction[f.key] for f in fields if extraction.get(f.key)), "")
    body = (
        f'<p style="margin:2px 0 0;font-size:13px;color:#3C4642">{escape(first_field)}</p>'
        if first_field
        else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:8px 0;border-left:3px solid #0E5E5A"><tr><td style="padding:2px 0 2px 10px">'
        f'<a href="{link}" style="color:#0E5E5A;font-weight:600;font-size:14px;text-decoration:none">'
        f"{escape(paper.title)}</a>"
        f'<p style="margin:2px 0 0;color:#5C6660;font-size:12px">{escape(_byline(paper))}</p>'
        f"{body}</td></tr></table>"
    )


def _email_list(papers: list[Paper]) -> str:
    rows = []
    for paper in papers:
        link = escape(paper.doi_url or paper.url)
        rows.append(
            '<tr><td style="padding:4px 0;border-bottom:1px solid #F0EEE8">'
            f'<a href="{link}" style="color:#0E5E5A;font-size:13px;text-decoration:none">'
            f"{escape(paper.title)}</a>"
            f'<span style="color:#5C6660;font-size:11px"> · {escape(_byline(paper))}</span>'
            "</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        + "".join(rows)
        + "</table>"
    )


def render_email_html(digest: Digest, fields: list[FieldSpec], max_bytes: int = EMAIL_MAX_BYTES) -> str:
    """Render the email body, trimming sections until it fits *max_bytes*.

    Table layout and inline styles only: Gmail drops @font-face and CSS
    variables, and Outlook has no flexbox or grid.
    """
    header = (
        f'<h1 style="margin:0 0 4px;font-size:20px">CMC 文献周报 {escape(digest.label)}</h1>'
        f'<p style="margin:0 0 16px;color:#5C6660;font-size:13px">'
        f"覆盖期 {digest.start.isoformat()} ~ {digest.end.isoformat()}　共 {digest.total} 篇</p>"
    )

    blocks: list[str] = []
    if digest.top_picks:
        blocks.append(_email_heading("本周优先读"))
        blocks.extend(_email_pick(p, fields) for p in digest.top_picks)

    for name, papers in digest.clusters:
        blocks.append(_email_heading(f"{escape(name)}（{len(papers)} 篇）"))
        blocks.append(_email_list(papers))

    if digest.backfill:
        blocks.append(_email_heading("经典补位"))
        blocks.append(_email_list(digest.backfill))

    footer = f'<p style="margin:18px 0 0;color:#5C6660;font-size:12px">{_TRUNCATION_NOTE}</p>'

    def assemble(kept: list[str], note: str = "") -> str:
        return _EMAIL_WRAP_OPEN + header + "".join(kept) + note + footer + _EMAIL_WRAP_CLOSE

    html = assemble(blocks)
    if len(html.encode("utf-8")) <= max_bytes:
        return html

    note = (
        '<p style="margin:14px 0 0;color:#94372C;font-size:12px">'
        f"内容较多，正文已截断。{_TRUNCATION_NOTE}</p>"
    )
    kept: list[str] = []
    for block in blocks:
        if len(assemble(kept + [block], note).encode("utf-8")) > max_bytes:
            break
        kept.append(block)
    return assemble(kept, note)
