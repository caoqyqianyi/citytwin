# Uncertainty: from missing heights to decision stability

## The problem

276 of 2 724 footprints carry a height label (10.1%). A LoD1 model needs a height for
every footprint, and a heat-exposure index built on those heights needs to know how
much of its ranking depends on guesswork.

Three layers handle this:

1. **A calibrated rule** for the point estimate.
2. **Conformal intervals** for each imputed height.
3. **Monte Carlo propagation** into the exposure index and the hotspot decision.

## 1. Point estimate

`rule_table()` computes a smoothed median per `building_class`:

```
h_class = (median_class * n_class + K * median_global) / (n_class + K),  K = 1
```

then snaps to the 3 m storey lattice. Evaluation uses 5-fold **blocked** spatial CV
(300 m blocks), so a building is never validated against its own neighbours.

| Metric | Value |
|---|---|
| MAE | 1.29 m |
| RMSE | 2.75 m |
| R² | 0.26 |
| Bias | −0.86 m |
| 80% interval coverage | 0.92 |
| Interval quantile | ±3.0 m |

The learned benchmark (random forest, 27 features + fold-safe class-median encoding)
reaches MAE 1.37 m, RMSE 2.63 m, R² 0.33 under the same folds and is **not** shipped:
it wins the tail but loses the median, and it is not explainable. Random folds give
MAE 1.41 m — slightly worse than blocked folds here, so no spatial optimism is visible;
blocked scores are still the only ones quoted (see ADR 0001).

## 2. Conformal intervals

For each fold, residuals of the held-out labels are collected; the 80th percentile of
the *other* folds' residuals gives the interval half-width for that fold. Pooled, the
quantile is 3.0 m, and the out-of-fold coverage is 0.92 — slightly conservative, which
is the right direction for a screening tool.

Every footprint carries:

```
height_final_m, height_lo_final_m, height_hi_final_m, height_uncertainty_m, height_provenance
```

Observed heights have zero-width intervals; imputed heights carry `rule:class-median-0.5`
as provenance.

## 3. Monte Carlo propagation

```
for draw in 1..n:
    h_i ~ Uniform(lo_i, hi_i)         for imputed footprints
    h_i  = observed                    for labelled footprints
    recompute cell means → HEI → hotspot set (≥ p90)
    record jaccard(draw hotspot set, observed hotspot set)
```

Results at n = 200:

| Quantity | Value |
|---|---|
| Mean Jaccard overlap with observed hotspot set | 0.45 |
| 5th percentile Jaccard | 0.2 |
| Cells hot in ≥ 80% of draws | 1 of 30 |
| Median 80% interval width of the HEI | ≈ 14 index points |

**Interpretation.** The hotspot map is a screening product, not a finding. Only the
cell that survives 80% of draws should be quoted as a hotspot; three cells cross the
threshold in the observed-height map, but two of them are conditional on the height
guess. The fix is data, not modelling: a LiDAR DSM or the national 3D building model
would collapse the intervals and make the hotspot set identifiable.

This is reported, not hidden: `docs/metrics.json` stores the numbers, the API exposes
`hotspot_probability` per cell, the viewer outlines only cells that pass 0.8, and the
agent answer repeats the caveat.

## 4. Weight sensitivity (a different uncertainty)

The index is a weighted sum of four normalised components. Perturbing the weights
(500 Dirichlet draws around the documented defaults) gives:

| Quantity | Value |
|---|---|
| Median Spearman ρ of cell ranking | 0.956 |
| 5th percentile ρ | 0.825 |
| Median hotspot Jaccard | 1.0 |
| 5th percentile Jaccard | 0.2 |

So the *ranking* is robust to weight choice; the *membership* of the top decile is not,
for cells sitting on the threshold. Both statements belong in a decision memo.

## What would change the conclusion

1. Height labels at 30–50% coverage, or a DSM — the dominant term.
2. A tree canopy layer instead of OSM tree nodes — the green component is currently
   coarse (27 polygons, 298 points).
3. Building age / construction type, which drives heat vulnerability but is absent
   from OSM.
