"""Community detection. Leiden over Louvain: Louvain can produce internally
disconnected communities, which here would mean a "network" whose members have no
behavioural relationship to each other."""

from dataclasses import dataclass

import igraph as ig
import leidenalg

from app.services.coordination.fusion import FusedEdge

DEFAULT_K_CORE = 3
DEFAULT_RESOLUTION = 1.0
DEFAULT_N_MIN = 5
DEFAULT_RHO_MIN = 0.30
DEFAULT_RANDOM_SEED = 42


@dataclass(frozen=True)
class DetectedCommunity:
    account_ids: list[str]
    internal_density: float
    conductance: float


def _build_graph(edges: list[FusedEdge], account_ids: list[str]) -> ig.Graph:
    graph = ig.Graph()
    graph.add_vertices(account_ids)
    if edges:
        graph.add_edges(
            [(e.account_a, e.account_b) for e in edges],
            attributes={"weight": [e.w_total for e in edges]},
        )
    return graph


def _k_core_subgraph(graph: ig.Graph, k: int) -> ig.Graph:
    """Removes accounts connected to fewer than k others, stripping incidental pairs
    and one-off coincidences before clustering."""
    coreness = graph.coreness()
    keep = [v.index for v in graph.vs if coreness[v.index] >= k]
    return graph.subgraph(keep)


def _conductance(graph: ig.Graph, member_indices: set[int]) -> float:
    """External edge weight over total incident edge weight - "dense internally,
    isolated externally"."""
    internal_weight = 0.0
    incident_weight = 0.0
    for edge in graph.es:
        w = edge["weight"]
        a, b = edge.tuple
        a_in, b_in = a in member_indices, b in member_indices
        if a_in and b_in:
            internal_weight += w
            incident_weight += 2 * w
        elif a_in or b_in:
            incident_weight += w
    return internal_weight / incident_weight if incident_weight > 0 else 0.0


def _internal_density(graph: ig.Graph, member_indices: set[int]) -> float:
    n = len(member_indices)
    if n < 2:
        return 0.0
    max_edges = n * (n - 1) / 2
    internal_edges = sum(
        1
        for edge in graph.es
        if edge.tuple[0] in member_indices and edge.tuple[1] in member_indices
    )
    return internal_edges / max_edges


def detect_communities(
    edges: list[FusedEdge],
    account_ids: list[str],
    k_core: int = DEFAULT_K_CORE,
    resolution: float = DEFAULT_RESOLUTION,
    n_min: int = DEFAULT_N_MIN,
    rho_min: float = DEFAULT_RHO_MIN,
    random_seed: int = DEFAULT_RANDOM_SEED,
) -> list[DetectedCommunity]:
    """Retention filters here: size >= n_min, internal density >= rho_min. Claim
    relevance and confidence banding are applied separately by the orchestrator."""
    graph = _build_graph(edges, account_ids)
    core = _k_core_subgraph(graph, k_core)
    if core.vcount() == 0:
        return []

    partition = leidenalg.find_partition(
        core,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight" if core.ecount() else None,
        resolution_parameter=resolution,
        seed=random_seed,
    )

    communities: list[DetectedCommunity] = []
    for community_indices in partition:
        if len(community_indices) < n_min:
            continue
        member_set = set(community_indices)
        density = _internal_density(core, member_set)
        if density < rho_min:
            continue
        conductance = _conductance(core, member_set)
        communities.append(
            DetectedCommunity(
                account_ids=[core.vs[idx]["name"] for idx in community_indices],
                internal_density=round(density, 4),
                conductance=round(conductance, 4),
            )
        )
    return communities
