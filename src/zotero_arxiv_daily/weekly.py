"""The weekly CMC digest pipeline.

Nine stages: fetch the Zotero corpus, cluster it into themes, distil a query
per theme, search the journal sources over the week's date window, collapse
duplicates, score and take a per-theme quota, top up a thin week, resolve
open-access full text, extract the configured fields, then render, archive
and send.
"""

import os
from datetime import date, datetime

import dotenv
import hydra
import numpy as np
from loguru import logger
from omegaconf import DictConfig
from openai import OpenAI

from zotero_arxiv_daily.backfill import backfill_papers
from zotero_arxiv_daily.dedup import (
    corpus_doi_set,
    dedup_papers,
    drop_seen,
    load_seen,
    normalize_doi,
    save_seen,
)
from zotero_arxiv_daily.executor import Executor, normalize_path_patterns
from zotero_arxiv_daily.extract import extract_all, load_field_specs
from zotero_arxiv_daily.fulltext.resolver import download_fulltext
from zotero_arxiv_daily.mailer import select_attachments, send_digest
from zotero_arxiv_daily.publish import git_commit_paths, git_push_artefacts, write_text
from zotero_arxiv_daily.quota import allocate_quota, take_by_quota
from zotero_arxiv_daily.report import build_digest, render_email_html, render_markdown, render_web_html
from zotero_arxiv_daily.reranker import get_reranker_cls
from zotero_arxiv_daily.reranker.base import time_decay_weights
from zotero_arxiv_daily.reranker.vector_cache import cached_similarity_matrix
from zotero_arxiv_daily.retriever import get_query_retriever_cls
from zotero_arxiv_daily.scoring import passing_papers, score_papers
from zotero_arxiv_daily.search.cluster import assign_clusters, load_or_build_clusters
from zotero_arxiv_daily.search.profile import load_or_build_profiles, query_for_source
from zotero_arxiv_daily.triage import triage_papers
from zotero_arxiv_daily.weeknum import library_dir, report_paths, week_label, week_window


def _stageable(path: str, root: str) -> str | None:
    """Return *path* relative to *root*, or None when it sits outside it.

    git cannot stage a path outside the repository, so anything the operator
    pointed elsewhere is written but not archived.
    """
    relative = os.path.relpath(os.path.join(root, path), root)
    return None if relative.startswith(os.pardir) else relative


def attachment_candidates(digest, limit: int) -> list[str]:
    """PDF paths worth attaching, best first.

    The top picks lead, then the rest of the digest by score, so a limit
    larger than ``top_picks`` still finds papers to attach.
    """
    if limit <= 0:
        return []
    ordered = list(digest.top_picks)
    seen = {id(p) for p in ordered}
    rest = [p for _, papers in digest.clusters for p in papers if id(p) not in seen]
    ordered.extend(sorted(rest, key=lambda p: -(p.score or 0.0)))
    # A thin week is mostly backfill; attaching nothing would be perverse.
    ordered.extend(p for p in digest.backfill if id(p) not in seen)
    return [p.pdf_path for p in ordered if p.pdf_path][:limit]


class WeeklyExecutor(Executor):
    """Reuses the Zotero corpus plumbing, replaces everything downstream."""

    def __init__(self, config: DictConfig):
        # Deliberately not calling Executor.__init__: the firehose retrievers it
        # constructs are not part of this pipeline.
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        # Populated by run(); read by _gate(). Defaulted here so _gate() never
        # AttributeErrors if it is ever reached before run() sets the real list.
        self._clusters: list = []

    def _search_all(self, profiles, start: date, end: date):
        limit = int(self.config.search.per_cluster_limit)
        candidates = []
        for source in self.config.search.sources:
            retriever = get_query_retriever_cls(source)(self.config)
            for profile in profiles:
                query = query_for_source(profile, source)
                found = retriever.search(query, start, end, limit)
                logger.info(f"{source}/{profile.cluster}: {len(found)} candidates")
                candidates.extend(found)
        return candidates

    def _similarity(self, candidates, corpus):
        """Similarity matrix, served from the vector cache when configured.

        Caching is an optimisation: a reranker that cannot embed, or a cache
        that cannot be used, falls back to the plain path rather than failing.
        """
        cache_rel = self.config.reranker.get("vector_cache")
        if cache_rel:
            model = str(
                self.config.reranker.api.get("model")
                if self.config.executor.reranker == "api"
                else self.config.reranker.local.model
            )
            try:
                return cached_similarity_matrix(
                    self.reranker,
                    candidates,
                    corpus,
                    os.path.join(str(self.config.report.output_dir), str(cache_rel)),
                    model,
                )
            except Exception as exc:  # noqa: BLE001 - an optimisation must never cost the digest
                logger.warning(f"Vector cache unusable ({exc}); embedding the corpus this run")
        return self.reranker.similarity_matrix(candidates, corpus)

    def _score_and_assign(self, candidates, corpus, clusters):
        """Score candidates and route them to a theme in one embedding pass."""
        order = sorted(range(len(corpus)), key=lambda i: corpus[i].added_date, reverse=True)
        ordered = [corpus[i] for i in order]

        sim_sorted = self._similarity(candidates, ordered)
        scores = (sim_sorted * time_decay_weights(len(ordered))).sum(axis=1) * 10
        for score, paper in zip(scores, candidates):
            paper.score = float(score)

        # Cluster membership indexes the corpus as fetched, so undo the sort.
        sim_original = np.empty_like(sim_sorted)
        sim_original[:, order] = sim_sorted
        assign_clusters(
            candidates,
            sim_original,
            clusters,
            desc_sim=self._description_similarity(candidates, clusters),
            description_weight=float(
                self.config.search.get("cluster_assignment_description_weight", 0.6)
            ),
        )

    def _description_similarity(self, candidates, clusters):
        """Candidate-to-cluster-description similarity, or None if it can't be had.

        A cluster's one-sentence description is a sharper, deliberately
        topical signal than the mean similarity to its corpus members, which
        is exactly what corrects a candidate that only shares surface
        vocabulary with a theme's papers.  It is a genuine enhancement,
        though, not a requirement: a failure here must fall back to the
        existing corpus-only routing, not cost the digest.
        """
        try:
            return self.reranker.get_similarity_score(
                [p.abstract for p in candidates], [c.description for c in clusters]
            )
        except Exception as exc:  # noqa: BLE001 - an enhancement must never cost the digest
            logger.warning(f"Cluster-description similarity unusable ({exc}); routing by corpus mean only")
            return None

    def _gate(self, papers):
        """Triage, score, and keep only what clears both thresholds.

        ``themes`` gives triage the real theme names and descriptions to
        validate ``paper.cluster`` against, so a candidate that is generally
        CMC-relevant but does not fit any theme the library actually holds
        gets excluded rather than forced under whichever theme's embedding
        similarity happened to be highest. Populated from ``self._clusters``,
        the same list quota allocation uses, so the two never disagree about
        what the real themes are. Read defensively: this method is also
        reachable, via the ``gate`` callback passed to backfill_papers, from
        any caller that might not have gone through run() first.
        """
        if not papers:
            return []
        themes = {c.name: c.description for c in getattr(self, "_clusters", []) if c.members}
        triage_papers(
            papers,
            self.openai_client,
            self.config.llm,
            int(self.config.report.get("triage_batch", 8)),
            themes=themes,
        )
        score_papers(papers, self.config)
        return passing_papers(papers, self.config)

    def run(self, anchor: date | None = None):
        anchor = anchor or datetime.now().date()
        label = week_label(anchor)
        start, end = week_window(anchor)
        logger.info(f"Building digest {label} covering {start} to {end}")

        root = str(self.config.report.output_dir)
        corpus = self.filter_corpus(self.fetch_zotero_corpus())
        if not corpus:
            logger.error(f"No Zotero papers matched. Check your settings:\n{self.config.zotero}")
            return None

        cluster_cache_rel = str(self.config.search.cluster_cache)
        profile_cache_rel = str(self.config.search.profile_cache)
        clusters = load_or_build_clusters(
            os.path.join(root, cluster_cache_rel),
            corpus,
            self.openai_client,
            self.config.llm,
            int(self.config.search.n_clusters),
        )
        # Read by _gate() to validate/correct candidate cluster assignment
        # against the same theme list quota allocation below uses.
        self._clusters = clusters
        profiles = load_or_build_profiles(
            os.path.join(root, profile_cache_rel),
            clusters,
            corpus,
            self.openai_client,
            self.config.llm,
        )

        # Anything already in the library, or already delivered in an earlier
        # week, is noise rather than a recommendation (spec 8.5).
        already_held = corpus_doi_set(corpus)
        seen_rel = str(self.config.search.seen_state)
        seen = load_seen(os.path.join(root, seen_rel))
        exclude = seen | already_held
        candidates = drop_seen(dedup_papers(self._search_all(profiles, start, end)), exclude)
        logger.info(
            f"{len(candidates)} candidates after de-duplication "
            f"({len(already_held)} library DOIs and {len(seen)} previously delivered DOIs excluded)"
        )

        chosen = []
        if candidates:
            self._score_and_assign(candidates, corpus, clusters)
            candidates.sort(key=lambda p: -(p.score or 0.0))
            # Triage is the cost ceiling: only the most similar candidates are
            # worth an LLM call, and everything below is the least similar of
            # an already-filtered pool.
            pool = candidates[: int(self.config.report.get("triage_pool", 60))]
            eligible = self._gate(pool)
            quota = allocate_quota(
                {c.name: len(c.members) for c in clusters},
                int(self.config.report.max_papers),
                int(self.config.report.min_per_cluster),
            )
            # Quota is allocated among survivors only.  Allocating it over all
            # candidates is what forced five themes times five slots to be
            # filled from the tail of the list.
            chosen = take_by_quota(eligible, quota)

        shortfall = int(self.config.report.min_papers) - len(chosen)
        backfill = []
        if shortfall > 0:
            backfill = backfill_papers(
                profiles,
                get_query_retriever_cls("openalex")(self.config),
                shortfall,
                exclude | {d for d in (normalize_doi(p.doi) for p in chosen) if d},
                gate=self._gate,
            )

        delivered = chosen + backfill
        if not delivered:
            logger.warning("No papers to deliver this week")
            return None

        min_papers = int(self.config.report.min_papers)
        if len(delivered) < min_papers:
            # backfill_papers() already logs *why* it fell short (OpenAlex,
            # exclude/dedup, or the relevance gate); this is the digest-level
            # symptom, so it is visible even to someone only skimming for the
            # final count rather than reading the full run log.
            logger.warning(
                f"Digest {label} delivered only {len(delivered)} papers "
                f"({len(chosen)} fresh + {len(backfill)} backfilled), short of the "
                f"{min_papers}-paper minimum even after backfill"
            )

        pdf_rel = library_dir(anchor)
        download_fulltext(delivered, self.config, os.path.join(root, pdf_rel))

        fields = load_field_specs(self.config)
        extract_all(delivered, self.openai_client, self.config.llm, fields)

        digest = build_digest(chosen, backfill, anchor, int(self.config.report.top_picks))
        md_rel, html_rel = report_paths(anchor)
        md_path = write_text(os.path.join(root, md_rel), render_markdown(digest, fields))
        html_path = write_text(os.path.join(root, html_rel), render_web_html(digest, fields))

        # Paths are staged relative to *root*, which is also where git runs, so
        # a non-default output_dir still archives.
        pdf_paths = attachment_candidates(digest, int(self.config.report.attach_pdfs))
        attachments = select_attachments([html_path] + pdf_paths)
        # Deliver first: recording these DOIs before the email lands would
        # suppress them from every future digest with nothing sent.
        send_digest(
            self.config,
            f"CMC 文献周报 {label}（共 {digest.total} 篇）",
            render_email_html(digest, fields),
            attachments,
        )

        save_seen(
            os.path.join(root, seen_rel),
            seen | {d for d in (normalize_doi(p.doi) for p in delivered) if d},
        )

        # The runner is ephemeral, so an uncommitted cache is no cache: without
        # these the LLM re-clusters every week and the theme names drift.
        wanted = [
            md_rel,
            html_rel,
            seen_rel,
            cluster_cache_rel,
            profile_cache_rel,
        ]
        vector_cache_rel = self.config.reranker.get("vector_cache")
        if vector_cache_rel:
            wanted.append(str(vector_cache_rel))
        if any(p.pdf_path for p in delivered) and self.config.git.get("include_pdfs", True):
            wanted.append(pdf_rel)
        staged = [rel for rel in (_stageable(w, root) for w in wanted) if rel]
        committed = git_commit_paths(
            staged,
            f"docs: add CMC literature digest {label}",
            self.config,
            cwd=root,
        )
        # An unpushed commit dies with the runner, taking the report, the
        # seen-DOI state and the caches with it — and the mail has already
        # gone out, so nothing else would ever reveal the loss.
        if not git_push_artefacts(self.config, cwd=root):
            raise RuntimeError(
                f"Digest {label} was mailed but could not be pushed; "
                "the archive and the seen-DOI state are lost with this runner"
            )

        logger.info(f"Digest {label} delivered: {digest.total} papers, archived at {md_path}")
        return digest


dotenv.load_dotenv()


@hydra.main(version_base=None, config_path="../../config", config_name="default")
def main(config: DictConfig) -> None:
    WeeklyExecutor(config).run()


if __name__ == "__main__":
    main()
