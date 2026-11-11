# ADR 0001 — The rule-based height model ships; the learned one is a benchmark

**Status:** accepted · **Date:** 2026-11-11

## Context

2 448 of 2 724 footprints have no height tag. We need a LoD1 model and an exposure
index, so imputation is unavoidable. The obvious choice is a gradient-boosted or
random-forest regressor on morphology and context features; the less glamorous one is
a smoothed per-class median snapped to the storey lattice.

## Decision

Ship the rule. Keep the learned model as a benchmark under the same folds, publish the
comparison in `docs/model_card.md`, and make the selection criterion explicit: MAE
under 300 m blocked cross-validation, with explainability as the tie-breaker.

## Evidence (270 labelled buildings, 5 blocked folds)

| Strategy | MAE | RMSE | R² | Bias | 80% coverage |
|---|---|---|---|---|---|
| **Rule: smoothed class median → 3 m lattice (shipped)** | **1.29 m** | 2.75 | 0.26 | −0.86 | 0.92 |
| Random forest, 27 features + fold-safe class medians | 1.37 m | 2.63 | 0.33 | −0.21 | 0.91 |
| Random forest, random folds (reference only) | 1.41 m | 2.70 | 0.29 | −0.21 | — |
| Global median baseline (6.0 m) | 1.83 m | 3.66 | — | — | — |

## Why the rule wins on MAE, and why that is not the whole story

- The labels are a three-spike histogram: 64% are exactly 6.0 m. A rule that predicts
  the class median is hard to beat on absolute error, and it is *not* beaten here.
- The forest is better on RMSE (2.63 vs 2.75) and R² (0.33 vs 0.26), and nearly
  unbiased. It trades median error for tail error: it hedges toward the mean, which is
  what a squared-error-trained model does. For a LoD1 model that means fewer absurd
  towers and more mildly wrong mid-rises — a different product decision, not a better
  one.
- Fold-safe class encoding matters: computing class medians outside the fold leaks the
  label into the feature and flatters the forest by a few centimetres per building.

Blocked folds (300 m blocks) give the forest 1.37 m; random folds give 1.41 m, i.e.
slightly *worse*. So this dataset shows no spatial optimism — but one AOI cannot prove
that random folds are safe elsewhere, and blocking remains the defensible design.
We quote only blocked numbers.

## Consequences

- The default output is explainable: a reviewer can read the class table and reproduce
  a height by hand.
- The model and its fold table stay in the repository, so the claim "the model does not
  win" is checkable and not a matter of taste.
- A LiDAR DSM is the next real improvement; `benchmark_heights` already exists to
  decide whether it beats the rule, and the criterion is already written down.
