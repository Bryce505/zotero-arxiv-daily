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

from .extract import FieldSpec, ListItem
from .protocol import Paper
from .search.focus import FocusResult
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
    # The operator's own topic, when one is configured. Rendered as its own
    # section rather than folded into the library's themes: it is a different
    # question, answered by a different relevance rubric.
    focus: FocusResult | None = None

    @property
    def focus_papers(self) -> list[Paper]:
        return list(self.focus.papers) if self.focus else []

    @property
    def total(self) -> int:
        return (
            sum(len(papers) for _, papers in self.clusters)
            + len(self.backfill)
            + len(self.focus_papers)
        )


def _rank_key(paper: Paper) -> float:
    """Best-first sort key: the composite score when the paper was scored,
    falling back to embedding similarity for any paper that reached here
    without a ScoreBreakdown (should not happen once every survivor is
    gated, but this must never crash)."""
    if paper.scoring is not None:
        return paper.scoring.rank_score
    return paper.score or 0.0


def build_digest(
    papers: list[Paper],
    backfill: list[Paper],
    anchor: date,
    top_n: int = 3,
    focus: FocusResult | None = None,
) -> Digest:
    """Group *papers* into the shape the renderers consume."""
    start, end = week_window(anchor)
    fresh = [p for p in papers if not p.is_backfill]

    grouped: dict[str, list[Paper]] = {}
    for paper in fresh:
        grouped.setdefault(paper.cluster or "未分类", []).append(paper)
    for bucket in grouped.values():
        bucket.sort(key=lambda p: -_rank_key(p))

    ordered = sorted(grouped.items(), key=lambda kv: (-max(_rank_key(p) for p in kv[1]), kv[0]))
    focus_papers = list(focus.papers) if focus else []

    return Digest(
        label=week_label(anchor),
        start=start,
        end=end,
        clusters=ordered,
        backfill=sorted(backfill, key=lambda p: -(p.cited_by_count or 0)),
        # Focus papers are deliberately not eligible: their relevance was
        # measured against the operator's topic, not against biologics CMC,
        # so ranking them next to cluster papers would make one badge number
        # mean two different things in the same report.
        top_picks=sorted(fresh, key=lambda p: -_rank_key(p))[:top_n],
        # Backfill and focus included: a thin week is mostly backfill, which is
        # exactly when the manual-retrieval list matters.
        needs_manual=[
            p for p in fresh + list(backfill) + focus_papers if p.oa_status != "open"
        ],
        focus=focus,
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


def _badges(paper: Paper) -> str:
    """Relevance plus the two reasons this paper outranked its neighbours."""
    scoring = paper.scoring
    if scoring is None:
        return ""
    bits = [f"相关度 {scoring.relevance}"]
    if scoring.journal_hit:
        bits.append("核心期刊")
    if scoring.industry_hit:
        bits.append(f"企业研究（{scoring.industry_hit}）")
    return " · ".join(bits)


def _reason(paper: Paper) -> str:
    return (paper.triage.reason if paper.triage else "") or ""


def _as_items(value) -> list[ListItem]:
    """Tolerate an extraction written before fields had types."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, ListItem)]
    return []


# --------------------------------------------------------------------------- markdown

def _markdown_field(spec: FieldSpec, value) -> list[str]:
    if spec.kind == "list":
        items = _as_items(value)
        if not items:
            return []
        lines = [f"**{spec.label}：**", ""]
        for n, item in enumerate(items, 1):
            lines.append(f"{n}. **{item.point}** — {item.detail}" if item.point else f"{n}. {item.detail}")
        return lines + [""]
    return [f"**{spec.label}：** {value}", ""] if value else []


def _markdown_entry(paper: Paper, fields: list[FieldSpec], extra: str = "") -> list[str]:
    link = paper.doi_url or paper.url
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    lines = [f"### {paper.title}", "", meta, ""]
    badges = _badges(paper)
    if badges:
        lines += [badges, ""]
    lines += [f"DOI: <{link}>", ""]
    reason = _reason(paper)
    if reason:
        lines += [f"**推荐理由：** {reason}", ""]
    for spec in fields:
        lines += _markdown_field(spec, (paper.extraction or {}).get(spec.key, ""))
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

    if digest.focus and digest.focus.papers:
        lines += [f"## 特定主题：{digest.focus.topic}（{len(digest.focus.papers)} 篇）", ""]
        if digest.focus.summary:
            lines += [f"> {digest.focus.summary}", ""]
        for paper in digest.focus.papers:
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
.points{margin:.35rem 0 0;padding-left:1.4rem}.points li{margin:.25rem 0}
.layout{display:grid;grid-template-columns:15rem 1fr;max-width:74rem;margin:0 auto;align-items:start}
main{padding:2rem 1.5rem}
nav.toc{position:sticky;top:0;height:100vh;overflow-y:auto;padding:2rem 1rem 2rem 1.5rem;
 border-right:1px solid var(--rule);font-size:.82rem}
.toc-title{font-weight:700;font-size:.95rem;margin-bottom:.9rem;color:var(--fg)}
.toc-section{color:var(--muted);font-weight:600;margin:1.1rem 0 .3rem;font-size:.75rem;letter-spacing:.02em}
nav.toc ul{list-style:none;margin:0;padding:0}
nav.toc li{margin:.15rem 0}
nav.toc a{display:flex;gap:.4rem;color:var(--fg);text-decoration:none;padding:.25rem .4rem;
 border-radius:6px;line-height:1.35}
nav.toc a:hover{background:var(--card);color:var(--accent)}
.num{color:var(--accent);font-weight:700;flex-shrink:0}
h3 .num,li .num{margin-right:.35rem}
@media (max-width:860px){
 .layout{display:block}
 nav.toc{position:static;height:auto;border-right:none;border-bottom:1px solid var(--rule);padding:1.2rem 1rem}
 main{padding:1.5rem 1rem}
}
""".strip()

_TOC_TITLE_MAX_LEN = 34


def _number_papers(digest: Digest) -> dict[int, int]:
    """Assign each unique paper a running number in reading order.

    Reading order is top picks first (the order a person meets them), then
    each cluster top to bottom, then backfill.  A top pick is the same
    ``Paper`` object as its cluster-card counterpart, so keying on ``id()``
    and skipping anything already seen makes both mentions carry one number
    rather than two.
    """
    numbers: dict[int, int] = {}
    n = 1
    for paper in (
        list(digest.top_picks)
        + [p for _, papers in digest.clusters for p in papers]
        + digest.focus_papers
        + list(digest.backfill)
    ):
        if id(paper) not in numbers:
            numbers[id(paper)] = n
            n += 1
    return numbers


def _num_badge(paper: Paper, numbers: dict[int, int]) -> str:
    n = numbers.get(id(paper))
    return f'<span class="num">#{n}</span> ' if n else ""


def _toc_html(digest: Digest, numbers: dict[int, int]) -> str:
    """A sticky sidebar of every section, each entry jumping to its card."""
    sections: list[tuple[str, list[Paper]]] = []
    if digest.top_picks:
        sections.append(("本周优先读", list(digest.top_picks)))
    sections.extend(digest.clusters)
    if digest.focus and digest.focus.papers:
        sections.append((f"特定主题：{digest.focus.topic}", list(digest.focus.papers)))
    if digest.backfill:
        sections.append(("经典补位", list(digest.backfill)))
    if not any(papers for _, papers in sections):
        return ""

    parts = ['<nav class="toc"><div class="toc-title">目录</div>']
    for name, papers in sections:
        if not papers:
            continue
        parts.append(f'<div class="toc-section">{escape(name)}</div><ul>')
        for paper in papers:
            n = numbers.get(id(paper))
            if n is None:
                continue
            title = paper.title
            short = title if len(title) <= _TOC_TITLE_MAX_LEN else title[:_TOC_TITLE_MAX_LEN] + "…"
            parts.append(f'<li><a href="#p-{n}"><span class="num">#{n}</span>{escape(short)}</a></li>')
        parts.append("</ul>")
    parts.append("</nav>")
    return "".join(parts)


def _html_card(paper: Paper, fields: list[FieldSpec], numbers: dict[int, int], extra: str = "") -> str:
    link = escape(paper.doi_url or paper.url)
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    n = numbers.get(id(paper))
    anchor = f' id="p-{n}"' if n else ""
    badge = _num_badge(paper, numbers)
    rows = [
        f'<div class="card"{anchor}><h3>{badge}<a href="{link}">{escape(paper.title)}</a></h3>',
        f'<p class="meta">{escape(meta)}</p>',
    ]
    badges = _badges(paper)
    if badges:
        rows.append(f'<p class="meta">{escape(badges)}</p>')
    reason = _reason(paper)
    if reason:
        rows.append(f'<div class="field"><b>推荐理由</b>：{escape(reason)}</div>')
    for spec in fields:
        value = (paper.extraction or {}).get(spec.key, "")
        if spec.kind == "list":
            items = _as_items(value)
            if not items:
                continue
            points = "".join(
                f"<li><strong>{escape(i.point)}</strong> — {escape(i.detail)}</li>"
                if i.point
                else f"<li>{escape(i.detail)}</li>"
                for i in items
            )
            rows.append(f'<div class="field"><b>{escape(spec.label)}</b><ol class="points">{points}</ol></div>')
        elif value:
            rows.append(f'<div class="field"><b>{escape(spec.label)}</b>：{escape(value)}</div>')
    rows.append("</div>")
    return "".join(rows)


def render_web_html(digest: Digest, fields: list[FieldSpec]) -> str:
    numbers = _number_papers(digest)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>CMC 文献周报 {escape(digest.label)}</title>",
        f"<style>{_WEB_CSS}</style></head><body><div class=\"layout\">",
        _toc_html(digest, numbers),
        "<main>",
        f"<h1>CMC 文献周报 {escape(digest.label)}</h1>",
        f'<p class="meta">覆盖期 {digest.start.isoformat()} ~ {digest.end.isoformat()}　共 {digest.total} 篇</p>',
    ]

    if digest.top_picks:
        parts.append("<h2>本周优先读</h2><ol>")
        for paper in digest.top_picks:
            link = escape(paper.doi_url or paper.url)
            badge = _num_badge(paper, numbers)
            parts.append(
                f'<li>{badge}<a href="{link}">{escape(paper.title)}</a> '
                f'<span class="meta">{escape(_byline(paper))}</span></li>'
            )
        parts.append("</ol>")

    for name, papers in digest.clusters:
        parts.append(f'<h2>{escape(name)}<span class="pill">{len(papers)} 篇</span></h2>')
        parts.extend(_html_card(p, fields, numbers) for p in papers)

    if digest.focus and digest.focus.papers:
        parts.append(
            f'<h2>特定主题：{escape(digest.focus.topic)}'
            f'<span class="pill">{len(digest.focus.papers)} 篇</span></h2>'
        )
        if digest.focus.summary:
            parts.append(f'<p class="meta">{escape(digest.focus.summary)}</p>')
        parts.extend(_html_card(p, fields, numbers) for p in digest.focus.papers)

    if digest.backfill:
        parts.append(
            '<h2>经典补位</h2><p class="meta">本周新文献不足，以下为依据文献库检索到的高引经典文献。</p>'
        )
        parts.extend(
            _html_card(p, fields, numbers, extra=f"被引 {p.cited_by_count or 0}") for p in digest.backfill
        )

    if digest.needs_manual:
        parts.append("<h2>需人工取全文</h2><ul>")
        for paper in digest.needs_manual:
            badge = _num_badge(paper, numbers)
            parts.append(f'<li>{badge}<a href="{escape(paper.doi_url or paper.url)}">{escape(paper.title)}</a></li>')
        parts.append("</ul>")

    parts.append("</main></div></body></html>")
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
    # The teaser is the recommendation reason: it is one sentence by
    # construction, and a list-valued field cannot be escaped as a string.
    teaser = _reason(paper)
    body = (
        f'<p style="margin:2px 0 0;font-size:13px;color:#3C4642">{escape(teaser)}</p>'
        if teaser
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
        badge = f" · 相关度 {paper.scoring.relevance}" if paper.scoring else ""
        rows.append(
            '<tr><td style="padding:4px 0;border-bottom:1px solid #F0EEE8">'
            f'<a href="{link}" style="color:#0E5E5A;font-size:13px;text-decoration:none">'
            f"{escape(paper.title)}</a>"
            f'<span style="color:#5C6660;font-size:11px"> · {escape(_byline(paper))}{escape(badge)}</span>'
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

    # One block per section, heading and content together: a heading kept
    # without its papers is worse than dropping the section outright.
    blocks: list[str] = []
    if digest.top_picks:
        blocks.append(
            _email_heading("本周优先读") + "".join(_email_pick(p, fields) for p in digest.top_picks)
        )

    for name, papers in digest.clusters:
        blocks.append(
            _email_heading(f"{escape(name)}（{len(papers)} 篇）") + _email_list(papers)
        )

    if digest.focus and digest.focus.papers:
        heading = _email_heading(
            f"特定主题：{escape(digest.focus.topic)}（{len(digest.focus.papers)} 篇）"
        )
        note = (
            f'<p style="margin:0 0 6px;color:#5C6660;font-size:12px">{escape(digest.focus.summary)}</p>'
            if digest.focus.summary
            else ""
        )
        blocks.append(heading + note + _email_list(digest.focus.papers))

    if digest.backfill:
        blocks.append(_email_heading("经典补位") + _email_list(digest.backfill))

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
    # Skip a section that does not fit rather than stopping: a later, smaller
    # one may still earn its place.
    kept: list[str] = []
    for block in blocks:
        if len(assemble(kept + [block], note).encode("utf-8")) <= max_bytes:
            kept.append(block)
    return assemble(kept, note)
