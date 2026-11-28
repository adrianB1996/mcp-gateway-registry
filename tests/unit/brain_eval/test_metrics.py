"""Unit tests for scripts/brain_eval/metrics.py."""

import importlib.util
import math
from pathlib import Path

import pytest
from brain_eval.metrics import (
    dcg_at_k,
    ndcg_at_k,
    paired_bootstrap,
    recall_at_k,
    reciprocal_rank,
    score_ranking,
    task_completed,
)

pytestmark = pytest.mark.unit

_EVALUATE_SEARCH = Path(__file__).resolve().parents[3] / "scripts" / "evaluate_search.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("evaluate_search", _EVALUATE_SEARCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestNdcg:
    def test_scores_a_perfect_ranking_as_one(self):
        assert ndcg_at_k(["a", "b"], {"a": 3, "b": 1}) == 1.0

    def test_scores_a_ranking_with_nothing_relevant_as_zero(self):
        assert ndcg_at_k(["x", "y"], {"a": 3}) == 0.0

    def test_penalises_a_relevant_item_ranked_second(self):
        assert ndcg_at_k(["x", "a"], {"a": 1}) == pytest.approx(1 / math.log2(3))

    def test_ignores_items_beyond_k(self):
        assert ndcg_at_k(["x", "a"], {"a": 1}, k=1) == 0.0

    def test_matches_the_existing_benchmark_definition(self):
        benchmark = _load_benchmark()
        expected = {"a": 3, "b": 2, "c": 1}
        ranked = ["b", "x", "a", "c"]

        theirs = benchmark._ndcg_at_k(
            [expected.get(i, 0) for i in ranked], list(expected.values()), 10
        )

        assert ndcg_at_k(ranked, expected) == pytest.approx(theirs)

    def test_dcg_uses_exponential_gain(self):
        assert dcg_at_k([2]) == 3.0


class TestRecallAndRank:
    def test_recall_counts_relevant_items_in_the_top_k(self):
        assert recall_at_k(["a", "x", "b"], {"a": 1, "b": 1, "c": 1}, k=2) == pytest.approx(1 / 3)

    def test_recall_ignores_zero_grades(self):
        assert recall_at_k(["a"], {"a": 1, "b": 0}) == 1.0

    def test_reciprocal_rank_of_the_first_relevant_item(self):
        assert reciprocal_rank(["x", "y", "a"], {"a": 1}) == pytest.approx(1 / 3)

    def test_score_ranking_reports_a_hit_at_one(self):
        scores = score_ranking(["a", "b"], {"a": 1})

        assert (scores.hit_at_1, scores.reciprocal_rank) == (1.0, 1.0)

    def test_score_ranking_handles_an_empty_result_list(self):
        scores = score_ranking([], {"a": 1})

        assert scores.model_dump() == {
            "ndcg": 0.0,
            "recall": 0.0,
            "reciprocal_rank": 0.0,
            "hit_at_1": 0.0,
        }


class TestTaskCompleted:
    def test_completes_when_every_expected_tool_was_called(self):
        assert task_completed({"ledger/a", "ledger/b", "files/c"}, {"ledger/a", "ledger/b"}) == 1.0

    def test_fails_when_an_expected_tool_is_missing(self):
        assert task_completed({"ledger/a"}, {"ledger/a", "ledger/b"}) == 0.0

    def test_rejects_a_task_with_no_expected_tools(self):
        with pytest.raises(ValueError, match="at least one expected tool"):
            task_completed({"a"}, set())


class TestPairedBootstrap:
    def test_interval_sits_above_zero_when_b_is_always_better(self):
        comparison = paired_bootstrap([0.0] * 50, [1.0] * 50, resamples=500)

        assert comparison.above_zero
        assert comparison.mean_difference == 1.0

    def test_interval_spans_zero_when_the_arms_tie(self):
        a = [1.0, 0.0] * 25
        b = [0.0, 1.0] * 25

        comparison = paired_bootstrap(a, b, resamples=2000)

        assert comparison.ci_low < 0.0 < comparison.ci_high

    def test_same_seed_gives_the_same_interval(self):
        a = [0.2, 0.5, 0.9, 0.1]
        b = [0.3, 0.4, 1.0, 0.6]

        assert paired_bootstrap(a, b, resamples=300, seed=7) == paired_bootstrap(
            a, b, resamples=300, seed=7
        )

    @pytest.mark.parametrize(
        ("a", "b", "message"),
        [([1.0], [1.0, 0.0], "equal lengths"), ([], [], "at least one item")],
    )
    def test_rejects_unusable_input(self, a, b, message):
        with pytest.raises(ValueError, match=message):
            paired_bootstrap(a, b)
