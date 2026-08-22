"""Optional monthly synthesis over the month's weekly digests.

Route B in the spec: a pass that reads what already shipped and looks for the
cross-cutting story — which themes grew, which questions recurred, what a
month of reading adds up to.  It is deliberately a separate workflow: if it
fails, the weekly digest is untouched.
"""

import os
import re
from datetime import date, datetime, timedelta
from html import escape

import dotenv
import hydra
from loguru import logger
from omegaconf import DictConfig
from openai import OpenAI

from zotero_arxiv_daily.mailer import select_attachments, send_digest
from zotero_arxiv_daily.publish import git_commit_paths, git_push_artefacts, write_text
from zotero_arxiv_daily.utils import truncate_for_prompt

_WEEK_FILE_RE = re.compile(r"^(\d{4})-(\d{2})-W\d+\.md$")
_MAX_REPORT_TOKENS = 4000


def synthesis_anchor(today: date) -> date:
    """Return a date inside the most recently completed month.

    A month whose last Friday falls on the 29th-31st only finishes at the
    month boundary, so the synthesis runs afterwards and looks back.
    """
    return today.replace(day=1) - timedelta(days=1)


def collect_month_reports(root: str, year: int, month: int) -> list[tuple[str, str]]:
    """Return ``(label, body)`` for every weekly digest of *year*-*month*."""
    folder = os.path.join(root, "reports", str(year))
    if not os.path.isdir(folder):
        return []
    found = []
    for name in sorted(os.listdir(folder)):
        match = _WEEK_FILE_RE.match(name)
        if not match or (int(match.group(1)), int(match.group(2))) != (year, month):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            found.append((name[: -len(".md")], handle.read()))
    return found


def synthesise(reports: list[tuple[str, str]], client, llm_params: dict) -> str:
    """Ask the model for the month's cross-cutting story; never raises."""
    language = llm_params.get("language", "中文")
    body = "\n\n---\n\n".join(
        f"## {label}\n{truncate_for_prompt(text, _MAX_REPORT_TOKENS)}" for label, text in reports
    )
    prompt = (
        f"下面是本月 {len(reports)} 份 CMC 文献周报。请用{language}写一份月度综述，包含：\n"
        "1. 本月主题分布与相较上月的演化\n"
        "2. 跨周反复出现的科学问题或技术路线\n"
        "3. 对生物制药 CMC 分析实践最有价值的 3–5 篇及理由\n"
        "4. 值得关注但本月证据尚不充分的方向\n\n"
        "输出 markdown，不要重复罗列每篇文献。\n\n"
        f"{body}"
    )
    try:
        response = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": f"你是一位生物制药 CMC 分析领域的资深研究员，用{language}撰写综述。",
                },
                {"role": "user", "content": prompt},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        return response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - the monthly layer must never break anything
        logger.warning(f"Monthly synthesis failed: {exc}")
        return ""


class MonthlyExecutor:
    def __init__(self, config: DictConfig):
        self.config = config
        self.client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)

    def run(self, anchor: date | None = None) -> str | None:
        anchor = anchor or datetime.now().date()
        root = self.config.report.output_dir
        reports = collect_month_reports(root, anchor.year, anchor.month)
        if not reports:
            logger.info(f"No weekly digests found for {anchor:%Y-%m}; nothing to synthesise")
            return None

        text = synthesise(reports, self.client, self.config.llm)
        if not text.strip():
            logger.warning("Monthly synthesis produced no content; skipping delivery")
            return None

        label = f"{anchor:%Y-%m}"
        rel = f"reports/{anchor.year}/{label}-monthly.md"
        path = write_text(f"{root}/{rel}", f"# CMC 文献月度综述 {label}\n\n{text}\n")
        git_commit_paths(
            [rel],
            f"docs: add CMC literature monthly synthesis {label}",
            self.config,
            cwd=root,
        )
        if not git_push_artefacts(self.config, cwd=root):
            raise RuntimeError(f"Synthesis {label} could not be pushed; the archive is lost")

        send_digest(
            self.config,
            f"CMC 文献月度综述 {label}",
            '<div style="font-family:-apple-system,Arial,sans-serif;white-space:pre-wrap">'
            f"{escape(text)}</div>",
            select_attachments([path]),
        )
        logger.info(f"Monthly synthesis {label} delivered, archived at {path}")
        return path


dotenv.load_dotenv()


@hydra.main(version_base=None, config_path="../../config", config_name="default")
def main(config: DictConfig) -> None:
    MonthlyExecutor(config).run(anchor=synthesis_anchor(datetime.now().date()))


if __name__ == "__main__":
    main()
