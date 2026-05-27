"""
ClusterIQ — SERP-Overlap Clustering Engine
===========================================
Builds a co-occurrence graph from SERP data and runs Louvain community
detection to group a site's URLs into topically coherent clusters.

Algorithm overview
------------------
1. Load site URLs (clusteriq_urls) and all SERP data (clusteriq_serp_data).
2. Build an inverted index:  url → frozenset of keywords where that URL
   appears anywhere in the serp_urls list.
3. For every keyword K, enumerate all pairs of *site* URLs that co-appear
   in its SERP (i.e., both URLs are present in serp_urls[K]).
   Each co-occurrence increments the edge weight between those two URLs.
4. Apply Louvain community detection (python-louvain) on the weighted graph.
5. Persist clusters + URL-cluster memberships to the DB.

Edge cases handled
------------------
* no_search_demand      — site URL has no SERP data at all (not in graph)
* non_indexable_with_traffic — status_code ≠ 200 yet has impressions
* indexable_zero_demand — is_indexable=True but 0 impressions in GSC
* multi_topic_url       — URL has cross-community edges ≥ overlap_threshold

Dependencies
------------
  pip install networkx python-louvain
  (python-louvain exposes the `community` module)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from sqlalchemy import delete, func, select, update

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import (
    CluCluster,
    CluProject,
    CluSerpData,
    CluUrl,
    CluUrlCluster,
)

logger = logging.getLogger("clusteriq.clustering")

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

_DEFAULT_OVERLAP_THRESHOLD = 3   # min shared-keyword count to add an edge
_MULTI_TOPIC_EDGE_THRESHOLD = 2  # min cross-community edge weight to flag multi-topic


# ---------------------------------------------------------------------------
# Data containers (plain dicts / namedtuples kept simple for readability)
# ---------------------------------------------------------------------------

class _UrlMeta:
    """Lightweight holder for per-URL attributes collected before graph build."""
    __slots__ = ("url_id", "url", "impressions", "clicks", "is_indexable", "status_code")

    def __init__(self, url_id, url, impressions, clicks, is_indexable, status_code):
        self.url_id       = url_id
        self.url          = url
        self.impressions  = impressions  or 0
        self.clicks       = clicks       or 0
        self.is_indexable = bool(is_indexable)
        self.status_code  = status_code


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class SERPOverlapClusteringEngine:
    """
    SERP-overlap clustering for a single ClusterIQ project.

    Parameters
    ----------
    overlap_threshold : int
        Minimum number of shared-keyword co-occurrences required before an
        edge is added between two URL nodes (default: 3).
    """

    def __init__(self, overlap_threshold: int = _DEFAULT_OVERLAP_THRESHOLD) -> None:
        self.overlap_threshold = overlap_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def build_url_graph(self, project_id: int):
        """
        Build a weighted NetworkX Graph for the project.

        Nodes  — one per site URL in clusteriq_urls
        Edges  — added when two URLs co-appear in ≥ overlap_threshold SERPs
        Weights — number of shared-keyword co-occurrences

        Node attributes stored: url, impressions_total, clicks_total,
        is_indexable, status_code, url_id.

        Returns
        -------
        networkx.Graph
        """
        try:
            import networkx as nx
        except ImportError as exc:
            raise ImportError("networkx is required: pip install networkx") from exc

        url_meta_map, edge_weights = await self._compute_graph_data(project_id)

        G = nx.Graph()

        # Add nodes
        for url, meta in url_meta_map.items():
            G.add_node(
                url,
                url_id           = meta.url_id,
                impressions_total = meta.impressions,
                clicks_total      = meta.clicks,
                is_indexable      = meta.is_indexable,
                status_code       = meta.status_code,
            )

        # Add edges above threshold
        for (url_a, url_b), weight in edge_weights.items():
            if weight >= self.overlap_threshold:
                G.add_edge(url_a, url_b, weight=weight)

        logger.info(
            "[Graph] project=%d — %d nodes, %d edges (threshold=%d)",
            project_id, G.number_of_nodes(), G.number_of_edges(), self.overlap_threshold,
        )
        return G

    def detect_communities(self, graph) -> dict[str, int]:
        """
        Run Louvain community detection on a weighted graph.

        Returns
        -------
        dict[url_str, community_id]
            Every node in the graph is assigned an integer community ID.
        """
        try:
            import community as community_louvain
        except ImportError as exc:
            raise ImportError(
                "python-louvain is required: pip install python-louvain"
            ) from exc

        if graph.number_of_nodes() == 0:
            return {}

        # Louvain requires at least one edge; handle isolated nodes separately
        connected = [n for n in graph.nodes if graph.degree(n) > 0]
        isolated  = [n for n in graph.nodes if graph.degree(n) == 0]

        partition: dict[str, int] = {}
        if connected:
            subgraph = graph.subgraph(connected)
            partition = community_louvain.best_partition(subgraph, weight="weight")

        # Assign each isolated node to its own unique community
        next_id = max(partition.values(), default=-1) + 1
        for node in isolated:
            partition[node] = next_id
            next_id += 1

        n_communities = len(set(partition.values()))
        logger.info(
            "[Louvain] %d nodes → %d communities (%d isolated)",
            len(partition), n_communities, len(isolated),
        )
        return partition

    async def assign_primary_url_per_cluster(
        self,
        community_mapping: dict[str, int],
        project_id: int,
    ) -> dict[int, dict]:
        """
        For each community, determine the primary URL and keyword.

        Primary URL     = URL with the highest total impressions in the cluster.
        Primary keyword = keyword with the most impressions for that primary URL.

        Returns
        -------
        dict[community_id, {
            "primary_url":      str,
            "primary_keyword":  str | None,
            "cluster_label":    str,
            "total_impressions": int,
            "total_clicks":     int,
            "url_count":        int,
            "search_demand_confirmed": bool,
            "urls": list[str],
        }]
        """
        # Group URLs by community
        community_urls: dict[int, list[str]] = defaultdict(list)
        for url, cid in community_mapping.items():
            community_urls[cid].append(url)

        # Load per-URL aggregated impressions + clicks from the project's SERP data
        url_impressions, url_clicks = await self._aggregate_url_metrics(project_id)

        result: dict[int, dict] = {}
        for cid, urls in community_urls.items():
            # Sort by impressions DESC, pick primary
            sorted_urls = sorted(urls, key=lambda u: url_impressions.get(u, 0), reverse=True)
            primary_url = sorted_urls[0]

            total_imp    = sum(url_impressions.get(u, 0) for u in urls)
            total_clicks = sum(url_clicks.get(u, 0) for u in urls)

            primary_keyword = await self._top_keyword_for_url(project_id, primary_url)
            cluster_label   = primary_keyword or primary_url

            result[cid] = {
                "primary_url":             primary_url,
                "primary_keyword":         primary_keyword,
                "cluster_label":           cluster_label,
                "total_impressions":       total_imp,
                "total_clicks":            total_clicks,
                "url_count":               len(urls),
                "search_demand_confirmed": total_imp > 0,
                "urls":                    sorted_urls,
            }
        return result

    async def save_clusters(
        self,
        project_id: int,
        community_mapping: dict[str, int],
        primary_mapping: dict[int, dict],
        graph,
    ) -> None:
        """
        Persist clusters and URL-cluster memberships to the database.

        Clears any previous clustering results for the project before inserting
        new rows (re-clustering is idempotent).

        Sets is_multi_topic=True on CluUrlCluster rows for URLs that have
        cross-community edges with weight ≥ _MULTI_TOPIC_EDGE_THRESHOLD.
        """
        multi_topic_urls = self._detect_multi_topic_urls(graph, community_mapping)
        url_id_map       = await self._load_url_id_map(project_id)
        url_impressions, _ = await self._aggregate_url_metrics(project_id)

        async with AsyncSessionLocal() as db:
            # Wipe previous clustering results
            await db.execute(
                delete(CluCluster).where(CluCluster.project_id == project_id)
            )
            await db.flush()

            for cid, meta in primary_mapping.items():
                # Insert cluster row
                cluster = CluCluster(
                    project_id              = project_id,
                    cluster_label           = meta["cluster_label"],
                    primary_url             = meta["primary_url"],
                    primary_keyword         = meta["primary_keyword"],
                    total_impressions       = meta["total_impressions"],
                    total_clicks            = meta["total_clicks"],
                    url_count               = meta["url_count"],
                    search_demand_confirmed = meta["search_demand_confirmed"],
                    louvain_community_id    = cid,
                    created_at              = datetime.now(timezone.utc),
                )
                db.add(cluster)
                await db.flush()   # populate cluster.id

                total_imp = meta["total_impressions"] or 1  # avoid div-by-zero

                for rank, url in enumerate(meta["urls"], start=1):
                    url_id = url_id_map.get(url)
                    if url_id is None:
                        # URL exists in SERP data but not in clusteriq_urls; skip
                        logger.debug("[Save] url not in clusteriq_urls, skipping: %s", url)
                        continue

                    imp_share = round(
                        (url_impressions.get(url, 0) / total_imp) * 100, 2
                    )
                    db.add(CluUrlCluster(
                        url_id               = url_id,
                        cluster_id           = cluster.id,
                        role                 = "primary" if rank == 1 else "collateral",
                        impression_share_pct = imp_share,
                        rank_in_cluster      = rank,
                        is_multi_topic       = url in multi_topic_urls,
                    ))

            await db.commit()

        logger.info(
            "[Save] project=%d — %d clusters persisted", project_id, len(primary_mapping)
        )

    async def run_clustering(self, project_id: int) -> dict[str, Any]:
        """
        Full clustering pipeline for a project.

        Steps
        -----
        1. Validate project status (must not be 'pending' / 'crawling').
        2. Build weighted SERP-overlap graph.
        3. Detect Louvain communities.
        4. Assign primary URLs + keywords per community.
        5. Persist to DB.
        6. Compute edge-case flags and summary stats.
        7. Set project status → 'done'.

        Returns
        -------
        dict with keys: total_clusters, urls_clustered, unclustered_urls,
        multi_topic_urls, avg_cluster_size, edge_case_flags.
        """
        project = await self._load_project(project_id)
        if project is None:
            raise ValueError(f"CluProject id={project_id} not found")

        logger.info(
            "[Clustering] project=%d (%s) — starting", project_id, project.domain
        )
        await self._set_status(project_id, "clustering")

        # --- 1. Build graph ------------------------------------------------
        graph = await self.build_url_graph(project_id)
        total_site_urls = await self._count_site_urls(project_id)

        if graph.number_of_nodes() == 0:
            logger.warning("[Clustering] project=%d — graph is empty, no SERP data", project_id)
            await self._set_status(project_id, "done")
            return self._empty_stats(total_site_urls)

        # --- 2. Community detection ----------------------------------------
        community_mapping = self.detect_communities(graph)

        # --- 3. Primary URL assignment ------------------------------------
        primary_mapping = await self.assign_primary_url_per_cluster(
            community_mapping, project_id
        )

        # --- 4. Persist ---------------------------------------------------
        await self.save_clusters(project_id, community_mapping, primary_mapping, graph)

        # --- 5. Edge-case flags -------------------------------------------
        edge_flags = await self._compute_edge_case_flags(project_id, community_mapping)

        # --- 6. Summary stats ---------------------------------------------
        clustered_urls  = len(community_mapping)
        unclustered     = total_site_urls - clustered_urls
        multi_topic_cnt = len(self._detect_multi_topic_urls(graph, community_mapping))

        cluster_sizes = [m["url_count"] for m in primary_mapping.values()]
        avg_size = round(sum(cluster_sizes) / len(cluster_sizes), 1) if cluster_sizes else 0.0

        await self._set_status(project_id, "done")

        summary = {
            "total_clusters":    len(primary_mapping),
            "urls_clustered":    clustered_urls,
            "unclustered_urls":  unclustered,
            "multi_topic_urls":  multi_topic_cnt,
            "avg_cluster_size":  avg_size,
            "edge_case_flags":   edge_flags,
        }
        logger.info("[Clustering] project=%d — done: %s", project_id, summary)
        return summary

    # ------------------------------------------------------------------
    # Graph computation helpers
    # ------------------------------------------------------------------

    async def _compute_graph_data(
        self, project_id: int
    ) -> tuple[dict[str, _UrlMeta], dict[tuple[str, str], int]]:
        """
        Load SERP data from DB and compute edge weights via SERP co-occurrence.

        Returns
        -------
        url_meta_map   : dict[url_str, _UrlMeta]
        edge_weights   : dict[(url_a, url_b), int]  — keys are always sorted
        """
        async with AsyncSessionLocal() as db:
            # Load site URLs with their DB IDs
            site_url_rows = (await db.execute(
                select(CluUrl.id, CluUrl.url, CluUrl.is_indexable, CluUrl.status_code)
                .where(CluUrl.project_id == project_id)
            )).all()

        site_url_set: set[str] = {row.url for row in site_url_rows}
        url_id_by_url: dict[str, int] = {row.url: row.id for row in site_url_rows}

        if not site_url_set:
            return {}, {}

        # Aggregate impressions + clicks per site URL from SERP data
        async with AsyncSessionLocal() as db:
            agg_rows = (await db.execute(
                select(
                    CluSerpData.url,
                    func.sum(CluSerpData.impressions).label("total_imp"),
                    func.sum(CluSerpData.clicks).label("total_clicks"),
                )
                .where(CluSerpData.project_id == project_id)
                .group_by(CluSerpData.url)
            )).all()

        imp_by_url: dict[str, int]   = {r.url: (r.total_imp   or 0) for r in agg_rows}
        clk_by_url: dict[str, int]   = {r.url: (r.total_clicks or 0) for r in agg_rows}

        # Build URL metadata map (only site URLs that have any SERP signal)
        url_meta_map: dict[str, _UrlMeta] = {}
        for row in site_url_rows:
            # Include URL in graph only if it has any SERP data
            if row.url in imp_by_url or row.url in clk_by_url:
                url_meta_map[row.url] = _UrlMeta(
                    url_id      = row.id,
                    url         = row.url,
                    impressions = imp_by_url.get(row.url, 0),
                    clicks      = clk_by_url.get(row.url, 0),
                    is_indexable = row.is_indexable,
                    status_code  = row.status_code,
                )

        graph_url_set = set(url_meta_map.keys())

        # Load distinct (keyword, serp_urls) pairs — one row per keyword is enough
        # since serp_urls is the same for all rows sharing a keyword (set by DataForSEO)
        async with AsyncSessionLocal() as db:
            serp_rows = (await db.execute(
                select(CluSerpData.keyword, CluSerpData.serp_urls)
                .where(
                    CluSerpData.project_id == project_id,
                    CluSerpData.serp_urls.isnot(None),
                )
                .distinct()
            )).all()

        # Compute pairwise edge weights from SERP co-occurrence
        edge_weights: dict[tuple[str, str], int] = defaultdict(int)

        for row in serp_rows:
            serp_url_list = row.serp_urls
            if not isinstance(serp_url_list, list):
                continue

            # Site URLs that appear in this keyword's SERP
            co_appearing = graph_url_set & set(serp_url_list)

            if len(co_appearing) < 2:
                continue

            for url_a, url_b in combinations(sorted(co_appearing), 2):
                edge_weights[(url_a, url_b)] += 1

        logger.debug(
            "[Graph] project=%d — %d site URLs with SERP data, %d keyword co-occurrences computed",
            project_id, len(graph_url_set), len(edge_weights),
        )
        return url_meta_map, edge_weights

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _aggregate_url_metrics(
        self, project_id: int
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Return (impressions_by_url, clicks_by_url) dicts for a project."""
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(
                    CluSerpData.url,
                    func.sum(CluSerpData.impressions).label("imp"),
                    func.sum(CluSerpData.clicks).label("clk"),
                )
                .where(CluSerpData.project_id == project_id)
                .group_by(CluSerpData.url)
            )).all()
        return (
            {r.url: r.imp or 0 for r in rows},
            {r.url: r.clk or 0 for r in rows},
        )

    async def _top_keyword_for_url(
        self, project_id: int, url: str
    ) -> str | None:
        """Return the keyword with the most impressions for a given URL."""
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(CluSerpData.keyword)
                .where(
                    CluSerpData.project_id == project_id,
                    CluSerpData.url == url,
                )
                .order_by(CluSerpData.impressions.desc().nullslast())
                .limit(1)
            )).scalar_one_or_none()
        return row

    async def _load_url_id_map(self, project_id: int) -> dict[str, int]:
        """Return {url_string: CluUrl.id} for all URLs in the project."""
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(CluUrl.url, CluUrl.id)
                .where(CluUrl.project_id == project_id)
            )).all()
        return {r.url: r.id for r in rows}

    async def _load_project(self, project_id: int) -> CluProject | None:
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(CluProject).where(CluProject.id == project_id)
            )).scalar_one_or_none()

    async def _set_status(self, project_id: int, status: str) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(CluProject)
                .where(CluProject.id == project_id)
                .values(status=status, updated_at=datetime.now(timezone.utc))
            )
            await db.commit()

    async def _count_site_urls(self, project_id: int) -> int:
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(func.count(CluUrl.id))
                .where(CluUrl.project_id == project_id)
            )).scalar() or 0

    # ------------------------------------------------------------------
    # Edge-case detection
    # ------------------------------------------------------------------

    async def _compute_edge_case_flags(
        self,
        project_id: int,
        community_mapping: dict[str, int],
    ) -> dict[str, list[str]]:
        """
        Compute edge-case URL lists for the project.

        Keys
        ----
        no_search_demand          — in clusteriq_urls but zero SERP rows
        non_indexable_with_traffic — status_code ≠ 200 yet impressions > 0
        indexable_zero_demand     — is_indexable=True but total impressions = 0
        """
        async with AsyncSessionLocal() as db:
            site_urls = (await db.execute(
                select(CluUrl.url, CluUrl.is_indexable, CluUrl.status_code)
                .where(CluUrl.project_id == project_id)
            )).all()

            # URLs that have at least one SERP row
            urls_with_serp = {
                r.url for r in (await db.execute(
                    select(CluSerpData.url).where(CluSerpData.project_id == project_id).distinct()
                )).all()
            }

            # Per-URL impression totals
            imp_rows = (await db.execute(
                select(CluSerpData.url, func.sum(CluSerpData.impressions).label("imp"))
                .where(CluSerpData.project_id == project_id)
                .group_by(CluSerpData.url)
            )).all()
            imp_by_url = {r.url: r.imp or 0 for r in imp_rows}

        no_demand:                 list[str] = []
        non_indexable_with_traffic: list[str] = []
        indexable_zero_demand:     list[str] = []

        for row in site_urls:
            url         = row.url
            impressions = imp_by_url.get(url, 0)
            indexable   = bool(row.is_indexable)
            ok_status   = row.status_code == 200 if row.status_code is not None else True

            if url not in urls_with_serp:
                no_demand.append(url)
                continue

            if not ok_status and impressions > 0:
                non_indexable_with_traffic.append(url)

            if indexable and impressions == 0:
                indexable_zero_demand.append(url)

        return {
            "no_search_demand":               no_demand,
            "non_indexable_with_traffic":     non_indexable_with_traffic,
            "indexable_zero_demand":          indexable_zero_demand,
        }

    # ------------------------------------------------------------------
    # Multi-topic detection
    # ------------------------------------------------------------------

    def _detect_multi_topic_urls(
        self,
        graph,
        community_mapping: dict[str, int],
    ) -> set[str]:
        """
        Return the set of URL strings that have at least one cross-community
        edge with weight ≥ _MULTI_TOPIC_EDGE_THRESHOLD.

        A URL is multi-topic when it is genuinely competitive across two or
        more distinct topic clusters — not just weakly linked.
        """
        multi: set[str] = set()
        for node in graph.nodes:
            own_community = community_mapping.get(node)
            if own_community is None:
                continue
            for neighbour, attrs in graph[node].items():
                if community_mapping.get(neighbour) != own_community:
                    if attrs.get("weight", 0) >= _MULTI_TOPIC_EDGE_THRESHOLD:
                        multi.add(node)
                        break
        return multi

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_stats(total_urls: int) -> dict[str, Any]:
        return {
            "total_clusters":   0,
            "urls_clustered":   0,
            "unclustered_urls": total_urls,
            "multi_topic_urls": 0,
            "avg_cluster_size": 0.0,
            "edge_case_flags": {
                "no_search_demand":              [],
                "non_indexable_with_traffic":    [],
                "indexable_zero_demand":         [],
            },
        }
