"""
Statistical inference for evaluation results.

A retrieval comparison on ~100 queries is a *sample*, not a measurement. Saying
"hybrid is +10.7 % over dense" without an interval invites two errors:

1. Reporting noise as an improvement.
2. Dismissing a real improvement because the point estimate looks small.

Two techniques are used here, both non-parametric, because NDCG per query is
bounded in [0, 1], heavily skewed, and frequently exactly 1.0 — so a t-test's
normality assumption is plainly violated.

## Bootstrap confidence intervals

Resample the queries with replacement, recompute the mean each time, and take
the 2.5th and 97.5th percentiles. This makes no distributional assumption and
answers "if I had drawn a different sample of questions from the same pool,
how much would this number move?"

## Paired bootstrap for comparisons

Strategies are evaluated on **the same queries**, so the comparison is paired
and the pairing must be preserved: resample *query indices*, then compute both
strategies on that resample. Treating the two as independent samples throws
away the pairing and badly overstates the uncertainty.

The reported p-value is the fraction of resamples in which the difference has
the opposite sign to the observed one, doubled for a two-sided test. This is an
achieved significance level, not a t-test, and is stated as such.

Deterministic: the RNG is seeded, so a reported interval is reproducible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 20240601


@dataclass(frozen=True)
class Interval:
    """A point estimate with a bootstrap confidence interval."""

    mean: float
    lower: float
    upper: float
    n: int
    confidence: float = 0.95

    @property
    def margin(self) -> float:
        return (self.upper - self.lower) / 2

    def format(self, places: int = 3) -> str:
        return f"{self.mean:.{places}f} [{self.lower:.{places}f}, {self.upper:.{places}f}]"

    def __str__(self) -> str:
        return self.format()


@dataclass(frozen=True)
class Comparison:
    """A paired comparison between two strategies on the same queries."""

    name_a: str
    name_b: str
    mean_a: float
    mean_b: float
    difference: float
    ci_lower: float
    ci_upper: float
    p_value: float
    n: int
    wins: int  # queries where B beat A
    losses: int  # queries where A beat B
    ties: int

    @property
    def relative_percent(self) -> float:
        return (self.difference / self.mean_a * 100) if self.mean_a else 0.0

    @property
    def is_significant(self) -> bool:
        """Significant when the 95 % interval excludes zero."""
        return not (self.ci_lower <= 0.0 <= self.ci_upper)

    @property
    def verdict(self) -> str:
        if not self.is_significant:
            return "no significant difference"
        direction = "better" if self.difference > 0 else "WORSE"
        return f"{self.name_b} is {direction}"

    def format(self) -> str:
        return (
            f"{self.name_b} vs {self.name_a}: "
            f"{self.difference:+.4f} [{self.ci_lower:+.4f}, {self.ci_upper:+.4f}] "
            f"({self.relative_percent:+.1f} %), p={self.p_value:.4f}, "
            f"W/L/T={self.wins}/{self.losses}/{self.ties} -> {self.verdict}"
        )


def bootstrap_mean(
    values: list[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = 0.95,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """Percentile bootstrap confidence interval for the mean."""
    if not values:
        return Interval(mean=0.0, lower=0.0, upper=0.0, n=0, confidence=confidence)
    if len(values) == 1:
        return Interval(values[0], values[0], values[0], 1, confidence)

    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)

    means.sort()
    alpha = (1.0 - confidence) / 2
    lower = means[int(alpha * resamples)]
    upper = means[min(int((1 - alpha) * resamples), resamples - 1)]
    return Interval(sum(values) / n, lower, upper, n, confidence)


def clustered_bootstrap_mean(
    values: list[float],
    clusters: list[str],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = 0.95,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """Bootstrap that resamples *clusters*, not individual observations.

    ## Why the ordinary bootstrap is wrong for this evaluation set

    `bootstrap_mean` assumes every observation is independent. The held-out
    questions are not: 96 of them were generated from **55 source passages**
    across **17 documents**, so roughly 1.75 questions share each passage. Two
    questions written from the same passage have the same relevant chunk and
    almost always the same retrieval outcome -- they are one observation
    counted twice.

    Resampling individual questions therefore treats correlated duplicates as
    fresh evidence and reports an interval narrower than the data supports.
    That is false precision, and it gets worse the more questions are generated
    per passage: the fix for a weak interval is a larger *corpus*, not more
    questions drawn from the passages already in it.

    This function resamples whole clusters with replacement instead, so the
    interval reflects the number of independent sources rather than the number
    of rows. It is wider, and the width is the honest one.
    """
    if not values:
        return Interval(mean=0.0, lower=0.0, upper=0.0, n=0, confidence=confidence)
    if len(values) != len(clusters):
        raise ValueError(f"values and clusters must align: {len(values)} vs {len(clusters)}")

    grouped: dict[str, list[float]] = {}
    for value, key in zip(values, clusters, strict=True):
        grouped.setdefault(key, []).append(value)

    keys = sorted(grouped)
    observed = sum(values) / len(values)
    if len(keys) == 1:
        return Interval(observed, observed, observed, len(keys), confidence)

    rng = random.Random(seed)
    groups = [grouped[key] for key in keys]
    count = len(groups)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        size = 0
        for _ in range(count):
            picked = groups[rng.randrange(count)]
            total += sum(picked)
            size += len(picked)
        means.append(total / size)

    means.sort()
    alpha = (1.0 - confidence) / 2
    lower = means[int(alpha * resamples)]
    upper = means[min(int((1 - alpha) * resamples), resamples - 1)]
    # n is the number of independent clusters, which is what the interval is
    # actually built from -- reporting 96 here would restate the same overclaim.
    return Interval(observed, lower, upper, len(keys), confidence)


def paired_bootstrap(
    name_a: str,
    values_a: list[float],
    name_b: str,
    values_b: list[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = 0.95,
    seed: int = DEFAULT_SEED,
) -> Comparison:
    """Paired bootstrap comparison of two strategies on the same queries.

    `values_a[i]` and `values_b[i]` must be the same query. Resampling happens
    on indices so the pairing survives, which is the whole point.
    """
    if len(values_a) != len(values_b):
        raise ValueError(
            f"paired comparison needs equal lengths, got {len(values_a)} and {len(values_b)}"
        )
    n = len(values_a)
    if n == 0:
        return Comparison(name_a, name_b, 0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0)

    mean_a = sum(values_a) / n
    mean_b = sum(values_b) / n
    observed = mean_b - mean_a

    wins = sum(1 for a, b in zip(values_a, values_b, strict=True) if b > a + 1e-12)
    losses = sum(1 for a, b in zip(values_a, values_b, strict=True) if a > b + 1e-12)
    ties = n - wins - losses

    rng = random.Random(seed)
    differences: list[float] = []
    for _ in range(resamples):
        total_a = total_b = 0.0
        for _ in range(n):
            index = rng.randrange(n)
            total_a += values_a[index]
            total_b += values_b[index]
        differences.append((total_b - total_a) / n)

    differences.sort()
    alpha = (1.0 - confidence) / 2
    ci_lower = differences[int(alpha * resamples)]
    ci_upper = differences[min(int((1 - alpha) * resamples), resamples - 1)]

    # Achieved significance level: how often the resampled difference lands on
    # the opposite side of zero from what we observed.
    if observed >= 0:
        opposite = sum(1 for d in differences if d <= 0)
    else:
        opposite = sum(1 for d in differences if d >= 0)
    p_value = min(1.0, 2.0 * opposite / resamples)

    return Comparison(
        name_a=name_a,
        name_b=name_b,
        mean_a=mean_a,
        mean_b=mean_b,
        difference=observed,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        p_value=p_value,
        n=n,
        wins=wins,
        losses=losses,
        ties=ties,
    )


def holm_bonferroni(
    comparisons: list[Comparison], alpha: float = 0.05
) -> list[tuple[Comparison, bool]]:
    """Correct for multiple comparisons, Holm-Bonferroni.

    Comparing four strategies pairwise is six tests. At alpha = 0.05 the chance
    of at least one false positive is roughly 26 %, so an uncorrected "p < 0.05"
    across a table of comparisons means much less than it appears to.

    Holm is used rather than plain Bonferroni: it is uniformly more powerful
    and makes no independence assumption, which matters because these tests
    share data.
    """
    if not comparisons:
        return []
    ordered = sorted(enumerate(comparisons), key=lambda pair: pair[1].p_value)
    total = len(ordered)
    results: list[tuple[int, bool]] = []
    rejected_so_far = True
    for rank, (index, comparison) in enumerate(ordered):
        threshold = alpha / (total - rank)
        significant = rejected_so_far and comparison.p_value <= threshold
        if not significant:
            rejected_so_far = False  # Holm stops at the first failure
        results.append((index, significant))

    verdicts = dict(results)
    return [(c, verdicts[i]) for i, c in enumerate(comparisons)]


def required_sample_size(
    effect: float, spread: float, *, power: float = 0.80, alpha: float = 0.05
) -> int:
    """Roughly how many paired queries are needed to detect `effect`.

    Normal approximation, so it is an order-of-magnitude guide rather than a
    precise figure. Useful for answering "is this evaluation set big enough?"
    honestly instead of hoping.
    """
    if effect <= 0 or spread <= 0:
        return 0
    z_alpha = 1.959964  # two-sided 0.05
    z_power = {0.80: 0.841621, 0.90: 1.281552, 0.95: 1.644854}.get(power, 0.841621)
    return max(1, math.ceil(((z_alpha + z_power) * spread / effect) ** 2))


def standard_deviation(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


__all__ = [
    "Comparison",
    "Interval",
    "bootstrap_mean",
    "clustered_bootstrap_mean",
    "holm_bonferroni",
    "paired_bootstrap",
    "required_sample_size",
    "standard_deviation",
]
