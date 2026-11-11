# Model card — building height completion

**Study area:** Esch-sur-Alzette · **Regenerate with:** `citytwin height --benchmark`

## What this module does

276 of 2720 footprints (10.1%) carry a
height label. The rest need a height so that a LoD1 city model can be generated.
Two strategies are implemented and compared; the shipped default is **rule**.

| Strategy | Definition |
|---|---|
| `rule` (default) | Smoothed per-`building_class` median, snapped to the 3 m storey lattice |
| `model` | Random forest on morphology + fold-safe class-median encoding |

Target: height_m = OSM height tag when present, else building:levels * 3.0 m · Source: © OpenStreetMap contributors (ODbL 1.0).

## Blocked cross-validation (300 m spatial blocks, 5 folds)

| Strategy | MAE (m) | RMSE (m) | R² | 80% coverage | Interval width |
|---|---|---|---|---|---|
| **rule (shipped)** | **1.29** | **2.75** | **0.26** | 0.92 | ±3.0 m |
| model (benchmark) | 1.37 | 2.63 | 0.326 | 0.907 | ±3.0 m |
| model, random folds (reference only) | 1.41 | 2.7 | 0.288 | — | — |

ΔMAE (model − rule): **0.08 m**. The learned strategy is only adopted when it reduces blocked MAE; otherwise the rule is shipped because it is simpler and fully explainable.

The model has the lower RMSE (2.63 m vs 2.75 m) but the
higher MAE, so it trades typical error for a better tail; the rule is shipped because it also
wins the median case and is fully explainable. Random-fold validation gives MAE
1.41 m — here it is slightly *worse* than blocked folds
(1.37 m), so no spatial optimism is visible with these features. Blocked
scores are still the ones quoted: they are the defensible design, and a single dataset cannot
show that random folds are safe.

### Rule calibration table

| Building class | Estimated height (m) |
|---|---|
| `apartments` | 8.79 |
| `detached` | 6.00 |
| `house` | 6.00 |
| `other` | 8.12 |
| `school` | 6.43 |

### Learned model per fold

| Fold | Test | MAE | RMSE | R² | Coverage@80 |
|---|---|---|---|---|---|
| 0 | 61 | 2.11 | 3.05 | 0.276 | 0.90 |
| 1 | 60 | 0.25 | 1.02 | 0.339 | 0.98 |
| 2 | 52 | 1.44 | 2.97 | 0.383 | 0.86 |
| 3 | 52 | 1.31 | 2.44 | 0.315 | 0.96 |
| 4 | 51 | 1.76 | 3.14 | -0.025 | 0.82 |

### Learned model importance (ΔMAE, metres)

| Feature | Importance |
|---|---|
| `class_median_height_m` | 0.22 |
| `is_house` | 0.18 |
| `is_apartments` | 0.13 |
| `dist_road_m` | 0.05 |
| `dist_tree_m` | 0.01 |
| `area_m2` | 0.01 |

## Uncertainty

Every imputed footprint carries an 80% conformal interval (±3.0 m for the
rule), stored as `height_lo_final_m` / `height_hi_final_m` and propagated by Monte
Carlo into the exposure index (`docs/uncertainty.md`).

## What this benchmark shows

Footprint morphology carries little height signal in a homogeneous low-rise town:
the label distribution is dominated by two-storey buildings, and building class is
the only strong predictor. That is a property of the data, not a tuning failure,
and it is why the platform reports intervals instead of pretending to metre
accuracy. Adding a LiDAR-derived DSM or the national 3D building model would
change this conclusion; the benchmark can be re-run with any label source.

## Limitations

1. Small, biased label set; tagged buildings skew towards public and recently mapped structures.
2. Flat-roof LoD1 only — no roof shape, setbacks or building parts.
3. `building:levels` is mapped inconsistently (mezzanines, attic floors); 3 m/storey is an assumption.
4. Static heights: no construction year, no vertical extension over time.

## Operational notes

Each geometry keeps `height_provenance` (`height_tag`, `levels`, `rule:<version>`,
`model:<version>`). The provenance document records the strategy, the quantile and
the label hash, so an auditor can reproduce every interval.
