"""Retrieval and task metrics shared by every eval arm.

NDCG, recall and MRR use the same definitions as ``scripts/evaluate_search.py``
(graded relevance, gain ``2**grade - 1``, discount ``log2(rank + 1)``), so the
frozen-corpus baseline can be compared with the existing benchmark report.

The design's gates compare two arms on the same items, so differences are
reported with a paired bootstrap confidence interval
(docs/design/knowledge-graph-search.md, Goal).
"""

import math
import random
from collections.abc import Collection, Mapping, Sequence

from pydantic import BaseModel, Field

DEFAULT_K: int = 10
DEFAULT_RESAMPLES: int = 10000
DEFAULT_CONFIDENCE: float = 0.95


class RankingScores(BaseModel):
    """Scores for one ranked result list against its ground truth."""

    ndcg: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)
    reciprocal_rank: float = Field(ge=0.0, le=1.0)
    hit_at_1: float = Field(ge=0.0, le=1.0)


class PairedComparison(BaseModel):
    """Difference between two arms scored on the same items (b minus a)."""

    n: int
    mean_a: float
    mean_b: float
    mean_difference: float
    ci_low: float
    ci_high: float
    confidence: float
    resamples: int

    model_config = {
        "json_schema_extra": {
            "example": {
                "n": 100,
                "mean_a": 0.62,
                "mean_b": 0.71,
                "mean_difference": 0.09,
                "ci_low": 0.03,
                "ci_high": 0.15,
                "confidence": 0.95,
                "resamples": 10000,
            }
        }
    }

    @property
    def above_zero(self) -> bool:
        """True when the whole interval is above zero (arm b is better)."""
        return self.ci_low > 0.0


def _relevant(
    expected: Mapping[str, int],
) -> dict[str, int]:
    """Keep the expected items with a positive grade."""
    return {item: grade for item, grade in expected.items() if grade > 0}


def dcg_at_k(
    grades: Sequence[int],
    k: int = DEFAULT_K,
) -> float:
    """Return discounted cumulative gain over the first k grades."""
    return sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades[:k]))


def ndcg_at_k(
    ranked: Sequence[str],
    expected: Mapping[str, int],
    k: int = DEFAULT_K,
) -> float:
    """Return NDCG@k of a ranked list against graded ground truth.

    Args:
        ranked: Result ids in rank order.
        expected: Ground truth, id to grade. Grades of zero or less are ignored.
        k: Cut-off.

    Returns:
        NDCG between 0 and 1, or 0 when nothing is relevant.
    """
    relevant = _relevant(expected)
    ideal = dcg_at_k(sorted(relevant.values(), reverse=True), k)
    if ideal == 0:
        return 0.0
    return dcg_at_k([relevant.get(item, 0) for item in ranked], k) / ideal


def recall_at_k(
    ranked: Sequence[str],
    expected: Mapping[str, int],
    k: int = DEFAULT_K,
) -> float:
    """Return the share of relevant items found in the first k results."""
    relevant = _relevant(expected)
    if not relevant:
        return 0.0
    return len(relevant.keys() & set(ranked[:k])) / len(relevant)


def reciprocal_rank(
    ranked: Sequence[str],
    expected: Mapping[str, int],
    k: int = DEFAULT_K,
) -> float:
    """Return 1 / rank of the first relevant item within k, or 0 if none."""
    relevant = _relevant(expected)
    for rank, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def score_ranking(
    ranked: Sequence[str],
    expected: Mapping[str, int],
    k: int = DEFAULT_K,
) -> RankingScores:
    """Score one ranked list with every retrieval metric.

    Args:
        ranked: Result ids in rank order.
        expected: Ground truth, id to grade.
        k: Cut-off for NDCG, recall and MRR.

    Returns:
        NDCG@k, recall@k, reciprocal rank and whether the top result is relevant.
    """
    relevant = _relevant(expected)
    return RankingScores(
        ndcg=ndcg_at_k(ranked, expected, k),
        recall=recall_at_k(ranked, expected, k),
        reciprocal_rank=reciprocal_rank(ranked, expected, k),
        hit_at_1=1.0 if ranked and ranked[0] in relevant else 0.0,
    )


def task_completed(
    called: Collection[str],
    expected_tools: Collection[str],
) -> float:
    """Return 1.0 when every expected tool was called, else 0.0.

    This is the design's headline metric: did the agent finish the task with
    the right tools. Extra calls do not fail a task; the eval reports them
    separately.

    Raises:
        ValueError: The task lists no expected tools, so it cannot be scored.
    """
    if not expected_tools:
        raise ValueError("A task must list at least one expected tool to be scored")
    return 1.0 if set(expected_tools) <= set(called) else 0.0


def _percentile(
    sorted_values: Sequence[float],
    fraction: float,
) -> float:
    """Return the value at a fraction of a sorted list, interpolating linearly."""
    position = fraction * (len(sorted_values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def paired_bootstrap(
    scores_a: Sequence[float],
    scores_b: Sequence[float],
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
) -> PairedComparison:
    """Estimate a confidence interval for mean(b - a) on paired items.

    Resamples items with replacement and keeps each item's two scores
    together, so per-item difficulty cancels out.

    Args:
        scores_a: Per-item scores for the baseline arm.
        scores_b: Per-item scores for the compared arm, same order as a.
        resamples: Bootstrap resamples.
        confidence: Interval width, for example 0.95.
        seed: Random seed, so a run is reproducible.

    Returns:
        Means, the mean difference and its percentile interval.

    Raises:
        ValueError: The lists differ in length or are empty, or a setting is
            out of range.
    """
    if len(scores_a) != len(scores_b):
        raise ValueError(
            f"Paired scores need equal lengths, got {len(scores_a)} and {len(scores_b)}"
        )
    if not scores_a:
        raise ValueError("Paired bootstrap needs at least one item")
    if resamples < 1 or not 0.0 < confidence < 1.0:
        raise ValueError(f"Invalid resamples={resamples} or confidence={confidence}")
    differences = [b - a for a, b in zip(scores_a, scores_b, strict=True)]
    n = len(differences)
    # Statistical resampling, not cryptography. nosec B311
    rng = random.Random(seed)  # nosec B311
    means = sorted(
        sum(differences[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    tail = (1.0 - confidence) / 2
    return PairedComparison(
        n=n,
        mean_a=sum(scores_a) / n,
        mean_b=sum(scores_b) / n,
        mean_difference=sum(differences) / n,
        ci_low=_percentile(means, tail),
        ci_high=_percentile(means, 1.0 - tail),
        confidence=confidence,
        resamples=resamples,
    )
