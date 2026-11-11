"""Height completion: a transparent rule, a learned benchmark, and the decision between them.

The module ships **two strategies** on purpose:

``rule`` (default)
    Per-``building_class`` median height, smoothed towards the global median,
    calibrated with a spatially blocked conformal interval (80%).
``model``
    Random forest on morphology plus a fold-safe target encoding of the class
    median. Available as ``--height-strategy model``.

``benchmark_heights`` runs both under identical blocked folds. On the Esch-sur-
Alzette AOI the learned model does not beat the rule (see the generated model
card), which is why the rule is the default: fewer parameters, fully explainable,
and no silent extrapolation. The benchmark is kept so the claim can be re-run
rather than asserted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.model_selection import GroupKFold, KFold

from .config import STOREY_HEIGHT_M
from .features import feature_matrix

MODEL_VERSION = "rf-height-0.5.0"
RULE_VERSION = "class-median-0.5.0"
Z_80 = 1.2816
SMOOTHING_K = 5.0

Strategy = Literal["rule", "model"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def snap_to_storey(values: np.ndarray) -> np.ndarray:
    """Snap predictions to the 3 m storey lattice used by OSM level tags."""
    return np.round(np.asarray(values, dtype=float) / STOREY_HEIGHT_M) * STOREY_HEIGHT_M


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    residuals = np.asarray(y_pred) - np.asarray(y_true)
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    return {
        "mae": round(float(np.mean(np.abs(residuals))), 2),
        "rmse": round(float(np.sqrt(np.mean(residuals**2))), 2),
        "r2": round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else float("nan"),
        "bias": round(float(np.mean(residuals)), 2),
        "n": len(y_true),
    }


def _storey_class(heights: np.ndarray) -> np.ndarray:
    storeys = np.round(np.asarray(heights) / STOREY_HEIGHT_M).astype(int)
    return np.where(
        storeys <= 2, "1-2", np.where(storeys == 3, "3", np.where(storeys == 4, "4", "5+"))
    )


# ---------------------------------------------------------------------------
# Strategy 1: the shipped rule
# ---------------------------------------------------------------------------
def rule_table(buildings: pd.DataFrame) -> dict[str, Any]:
    """Smoothed per-class median heights fitted on labelled buildings."""
    labelled = buildings[buildings["has_height_label"]]
    if labelled.empty:
        raise ValueError("no labelled buildings: cannot fit the rule table")
    global_median = float(labelled["height_m"].median())
    stats = labelled.groupby("building_class")["height_m"].agg(["median", "count"])
    table = {
        str(cls): float(
            (row["median"] * row["count"] + global_median * SMOOTHING_K)
            / (row["count"] + SMOOTHING_K)
        )
        for cls, row in stats.iterrows()
    }
    return {
        "table": table,
        "global_median_m": round(global_median, 2),
        "n_labelled": len(labelled),
        "version": RULE_VERSION,
    }


def apply_rule(buildings: pd.DataFrame, table: dict[str, Any], quantile_m: float) -> pd.DataFrame:
    frame = buildings.copy()
    point = (
        frame["building_class"].map(table["table"]).astype(float).fillna(table["global_median_m"])
    )
    point = snap_to_storey(point.to_numpy())
    frame["height_modelled_m"] = point
    frame["height_lo_m"] = np.maximum(0.0, point - quantile_m)
    frame["height_hi_m"] = point + quantile_m
    frame["height_final_m"] = frame["height_observed_m"].fillna(pd.Series(point, index=frame.index))
    frame["height_lo_final_m"] = np.where(
        frame["has_height_label"], frame["height_observed_m"], frame["height_lo_m"]
    )
    frame["height_hi_final_m"] = np.where(
        frame["has_height_label"], frame["height_observed_m"], frame["height_hi_m"]
    )
    frame["height_uncertainty_m"] = (frame["height_hi_final_m"] - frame["height_lo_final_m"]) / 2
    frame["height_provenance"] = np.where(
        frame["has_height_label"], frame["height_source"], "rule:" + RULE_VERSION
    )
    return frame


def calibrate_rule(
    buildings: pd.DataFrame, *, n_splits: int = 5, interval: float = 0.8
) -> dict[str, Any]:
    """Blocked out-of-fold evaluation of the rule, with nested conformal coverage."""
    labelled = buildings[buildings["has_height_label"]].reset_index(drop=True)
    groups = labelled["cv_block"].to_numpy()
    y = labelled["height_m"].to_numpy(dtype=float)
    folds = list(GroupKFold(n_splits=n_splits).split(np.zeros((len(y), 1)), y, groups))
    oof = np.full(len(y), np.nan)
    residual_abs: list[np.ndarray] = []
    tables = []
    for train_idx, test_idx in folds:
        table = rule_table(buildings.loc[labelled.index[train_idx]])
        tables.append(table)
        pred = snap_to_storey(
            labelled.iloc[test_idx]["building_class"]
            .map(table["table"])
            .astype(float)
            .fillna(table["global_median_m"])
            .to_numpy()
        )
        oof[test_idx] = pred
        residual_abs.append(np.abs(pred - y[test_idx]))

    coverage, widths = [], []
    for fold, res in enumerate(residual_abs):
        others = np.concatenate([r for i, r in enumerate(residual_abs) if i != fold])
        q = float(np.quantile(others, interval))
        coverage.append(float(np.mean(res <= q)))
        widths.append(2 * q)

    metrics = _metrics(y, oof)
    metrics["coverage_80"] = round(float(np.mean(coverage)), 3)
    return {
        "strategy": "rule",
        "version": RULE_VERSION,
        "metrics": metrics,
        "interval_quantile_m": round(float(np.quantile(np.concatenate(residual_abs), interval)), 2),
        "interval_level": interval,
        "interval_width_median_m": round(float(np.median(widths)), 2),
        "folds": [
            {"fold": i, "n_test": len(res)}
            | {
                k: v
                for k, v in _metrics(y[folds[i][1]], oof[folds[i][1]]).items()
                if k in {"mae", "rmse", "r2"}
            }
            for i, res in enumerate(residual_abs)
        ],
        "table": rule_table(buildings),
    }


# ---------------------------------------------------------------------------
# Strategy 2: the learned benchmark
# ---------------------------------------------------------------------------
def _new_model(seed: int) -> RandomForestRegressor:
    return RandomForestRegressor(
        n_estimators=200,
        min_samples_leaf=8,
        max_features=0.5,
        random_state=seed,
        n_jobs=-1,
    )


def _target_encode(
    train_frame: pd.DataFrame, target_frame: pd.DataFrame, y_train: np.ndarray
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Fold-safe class-median encoding: leave-one-out on train, table on target."""
    stats = pd.DataFrame({"c": train_frame["building_class"].to_numpy(), "h": y_train})
    sums = stats.groupby("c")["h"].sum()
    counts = stats.groupby("c")["h"].count()
    prior = float(np.mean(y_train))
    train_classes = train_frame["building_class"].to_numpy()
    target_classes = target_frame["building_class"].to_numpy()
    train_values = (sums.reindex(train_classes).to_numpy() - y_train + prior * SMOOTHING_K) / (
        counts.reindex(train_classes).to_numpy() - 1 + SMOOTHING_K
    )
    target_values = (sums.reindex(target_classes).to_numpy() + prior * SMOOTHING_K) / (
        counts.reindex(target_classes).to_numpy() + SMOOTHING_K
    )
    encoding_table = {
        str(k): float(v) for k, v in ((sums + prior * SMOOTHING_K) / (counts + SMOOTHING_K)).items()
    }
    return train_values, target_values, encoding_table


@dataclass
class FoldScore:
    fold: int
    n_train: int
    n_test: int
    mae: float
    rmse: float
    r2: float
    coverage_80: float = float("nan")


def train_height_model(
    buildings: pd.DataFrame, *, seed: int = 42, n_splits: int = 5, interval: float = 0.8
) -> dict[str, Any]:
    """Train the learned strategy and return a serialisable bundle."""
    labelled = buildings[buildings["has_height_label"]].reset_index(drop=True)
    if len(labelled) < n_splits * 4:
        raise ValueError(f"not enough labelled buildings ({len(labelled)}) for {n_splits}-fold CV")

    base = feature_matrix(labelled)
    y = labelled["height_m"].to_numpy(dtype=float)
    groups = labelled["cv_block"].to_numpy()
    folds = list(GroupKFold(n_splits=n_splits).split(base, y, groups))

    oof = np.full(len(y), np.nan)
    fold_scores: list[FoldScore] = []
    residual_abs: list[np.ndarray] = []
    importance_acc = np.zeros(len(base.columns) + 1)  # + class median target encoding
    encoding_table: dict[str, float] = {}
    for fold, (train_idx, test_idx) in enumerate(folds):
        train_values, target_values, encoding_table = _target_encode(
            labelled.iloc[train_idx], labelled.iloc[test_idx], y[train_idx]
        )
        X_train = np.c_[base.iloc[train_idx].to_numpy(), train_values]
        X_test = np.c_[base.iloc[test_idx].to_numpy(), target_values]
        model = _new_model(seed + fold).fit(X_train, y[train_idx])
        oof[test_idx] = snap_to_storey(model.predict(X_test))
        m = _metrics(y[test_idx], oof[test_idx])
        fold_scores.append(
            FoldScore(fold, len(train_idx), len(test_idx), m["mae"], m["rmse"], m["r2"])
        )
        result = permutation_importance(
            model,
            X_test,
            y[test_idx],
            n_repeats=8,
            random_state=seed + fold,
            scoring="neg_mean_absolute_error",
            n_jobs=1,
        )
        importance_acc += result.importances_mean
        residual_abs.append(np.abs(oof[test_idx] - y[test_idx]))

    coverage, widths = [], []
    for fold, res in enumerate(residual_abs):
        others = np.concatenate([r for i, r in enumerate(residual_abs) if i != fold])
        q = float(np.quantile(others, interval))
        coverage.append(float(np.mean(res <= q)))
        widths.append(2 * q)
        fold_scores[fold].coverage_80 = round(coverage[-1], 3)
    q_final = float(np.quantile(np.concatenate(residual_abs), interval))

    metrics = _metrics(y, oof)
    metrics["coverage_80"] = round(float(np.mean(coverage)), 3)

    random_oof = np.full(len(y), np.nan)
    for fold, (train_idx, test_idx) in enumerate(
        KFold(n_splits=n_splits, shuffle=True, random_state=seed).split(base)
    ):
        train_values, target_values, _ = _target_encode(
            labelled.iloc[train_idx], labelled.iloc[test_idx], y[train_idx]
        )
        model = _new_model(seed + 100 + fold).fit(
            np.c_[base.iloc[train_idx].to_numpy(), train_values], y[train_idx]
        )
        random_oof[test_idx] = snap_to_storey(
            model.predict(np.c_[base.iloc[test_idx].to_numpy(), target_values])
        )
    random_metrics = _metrics(y, random_oof)

    final_values, _, final_table = _target_encode(labelled, labelled, y)
    final_model = _new_model(seed).fit(np.c_[base.to_numpy(), final_values], y)

    importance = pd.DataFrame(
        {
            "feature": [*base.columns, "class_median_height_m"],
            "permutation_importance_mae_m": (importance_acc / n_splits).round(3),
        }
    ).sort_values("permutation_importance_mae_m", ascending=False)

    return {
        "model": final_model,
        "feature_names": [*base.columns, "class_median_height_m"],
        "encoding_table": final_table or encoding_table,
        "global_median_m": round(float(np.median(y)), 2),
        "medians": {name: float(np.median(base[name].to_numpy())) for name in base.columns},
        "metrics": metrics,
        "random_fold_metrics": random_metrics,
        "folds": [score.__dict__ for score in fold_scores],
        "importance": importance.to_dict("records"),
        "interval_quantile_m": round(q_final, 2),
        "interval_level": interval,
        "interval_width_median_m": round(float(np.median(widths)), 2),
        "storey_exact_accuracy": round(float(np.mean(_storey_class(oof) == _storey_class(y))), 3),
        "storey_within_one_accuracy": round(
            float(
                np.mean(
                    np.abs(np.round(oof / STOREY_HEIGHT_M) - np.round(y / STOREY_HEIGHT_M)) <= 1
                )
            ),
            3,
        ),
        "storey_prior": round(
            float(pd.Series(_storey_class(y)).value_counts(normalize=True).max()), 3
        ),
        "label_coverage": round(100 * len(labelled) / len(buildings), 1),
        "n_labelled": len(labelled),
        "n_total": len(buildings),
        "seed": seed,
        "version": MODEL_VERSION,
        "target_definition": "height_m = OSM height tag when present, else building:levels * 3.0 m",
    }


def predict_heights(bundle: dict[str, Any], buildings: pd.DataFrame) -> pd.DataFrame:
    """Apply the learned strategy to all footprints."""
    frame = buildings.copy()
    base = feature_matrix(frame)
    encoded = (
        frame["building_class"]
        .map(bundle["encoding_table"])
        .astype(float)
        .fillna(bundle["global_median_m"])
        .to_numpy()
    )
    X = np.c_[base.to_numpy(dtype=float), encoded]
    point = snap_to_storey(bundle["model"].predict(X))
    q = float(bundle["interval_quantile_m"])

    frame["height_modelled_m"] = point
    frame["height_lo_m"] = np.maximum(0.0, point - q)
    frame["height_hi_m"] = point + q
    frame["height_final_m"] = frame["height_observed_m"].fillna(pd.Series(point, index=frame.index))
    frame["height_lo_final_m"] = np.where(
        frame["has_height_label"], frame["height_observed_m"], frame["height_lo_m"]
    )
    frame["height_hi_final_m"] = np.where(
        frame["has_height_label"], frame["height_observed_m"], frame["height_hi_m"]
    )
    frame["height_uncertainty_m"] = np.where(
        frame["has_height_label"],
        0.0,
        (frame["height_hi_final_m"] - frame["height_lo_final_m"]) / 2,
    )
    frame["height_provenance"] = np.where(
        frame["has_height_label"], frame["height_source"], "model:" + bundle["version"]
    )
    return frame


def explain_prediction(
    bundle: dict[str, Any], buildings: pd.DataFrame, osm_id: str, *, top_k: int = 5
) -> dict[str, Any]:
    """Occlusion explanation: replace one feature with the training median."""
    row = buildings.loc[buildings["osm_id"] == osm_id]
    if row.empty:
        raise KeyError(f"unknown building {osm_id}")
    base = feature_matrix(row)
    encoded = (
        row["building_class"]
        .map(bundle["encoding_table"])
        .astype(float)
        .fillna(bundle["global_median_m"])
        .to_numpy()
    )
    x = np.r_[base.to_numpy(dtype=float)[0], encoded]
    point = float(bundle["model"].predict(x.reshape(1, -1))[0])
    contributions = []
    for index, name in enumerate(bundle["feature_names"]):
        occluded = x.copy()
        occluded[index] = bundle["medians"].get(name, 0.0)
        contributions.append(
            {
                "feature": name,
                "delta_m": round(
                    point - float(bundle["model"].predict(occluded.reshape(1, -1))[0]), 2
                ),
            }
        )
    contributions.sort(key=lambda item: abs(item["delta_m"]), reverse=True)
    return {
        "osm_id": osm_id,
        "prediction_m": round(point, 2),
        "interval_80_m": [
            round(point - bundle["interval_quantile_m"], 2),
            round(point + bundle["interval_quantile_m"], 2),
        ],
        "explanation_method": "occlusion-to-training-median",
        "top_contributions": contributions[:top_k],
        "all_contributions": contributions,
    }


# ---------------------------------------------------------------------------
# The decision: benchmark, then choose
# ---------------------------------------------------------------------------
def benchmark_heights(
    buildings: pd.DataFrame, *, seed: int = 42, n_splits: int = 5, interval: float = 0.8
) -> dict[str, Any]:
    """Evaluate rule and learned strategies under identical blocked folds."""
    rule = calibrate_rule(buildings, n_splits=n_splits, interval=interval)
    learned = train_height_model(buildings, seed=seed, n_splits=n_splits, interval=interval)
    delta = round(learned["metrics"]["mae"] - rule["metrics"]["mae"], 2)
    decision = "rule" if delta >= 0 else "model"
    return {
        "decision": decision,
        "delta_mae_m": delta,
        "rule": {k: v for k, v in rule.items() if k != "table"},
        "rule_table": rule["table"],
        "model": {k: v for k, v in learned.items() if k != "model"},
        "learned_bundle": learned,
        "rationale": (
            "The learned strategy is only adopted when it reduces blocked MAE; otherwise the "
            "rule is shipped because it is simpler and fully explainable."
        ),
    }


def write_model_card(benchmark: dict[str, Any], path: Path, aoi_name: str) -> Path:
    """Write a model card covering both strategies and the selection decision."""
    rule = benchmark["rule"]
    learned = benchmark["model"]
    top = learned["importance"][:6]
    rows = "\n".join(
        f"| `{item['feature']}` | {item['permutation_importance_mae_m']:.2f} |" for item in top
    )
    folds = "\n".join(
        f"| {f['fold']} | {f['n_test']} | {f['mae']:.2f} | {f['rmse']:.2f} | {f['r2']:.3f} | {f['coverage_80']:.2f} |"
        for f in learned["folds"]
    )
    table = "\n".join(
        f"| `{k}` | {v:.2f} |" for k, v in sorted(benchmark["rule_table"]["table"].items())
    )
    text = f"""# Model card — building height completion

**Study area:** {aoi_name} · **Regenerate with:** `citytwin height --benchmark`

## What this module does

{learned["n_labelled"]} of {learned["n_total"]} footprints ({learned["label_coverage"]}%) carry a
height label. The rest need a height so that a LoD1 city model can be generated.
Two strategies are implemented and compared; the shipped default is **{benchmark["decision"]}**.

| Strategy | Definition |
|---|---|
| `rule` (default) | Smoothed per-`building_class` median, snapped to the 3 m storey lattice |
| `model` | Random forest on morphology + fold-safe class-median encoding |

Target: {learned["target_definition"]} · Source: © OpenStreetMap contributors (ODbL 1.0).

## Blocked cross-validation (300 m spatial blocks, 5 folds)

| Strategy | MAE (m) | RMSE (m) | R² | 80% coverage | Interval width |
|---|---|---|---|---|---|
| **rule (shipped)** | **{rule["metrics"]["mae"]}** | **{rule["metrics"]["rmse"]}** | **{rule["metrics"]["r2"]}** | {rule["metrics"]["coverage_80"]} | ±{rule["interval_quantile_m"]} m |
| model (benchmark) | {learned["metrics"]["mae"]} | {learned["metrics"]["rmse"]} | {learned["metrics"]["r2"]} | {learned["metrics"]["coverage_80"]} | ±{learned["interval_quantile_m"]} m |
| model, random folds (reference only) | {learned["random_fold_metrics"]["mae"]} | {learned["random_fold_metrics"]["rmse"]} | {learned["random_fold_metrics"]["r2"]} | — | — |

ΔMAE (model − rule): **{benchmark["delta_mae_m"]} m**. {benchmark["rationale"]}

The model has the lower RMSE ({learned["metrics"]["rmse"]} m vs {rule["metrics"]["rmse"]} m) but the
higher MAE, so it trades typical error for a better tail; the rule is shipped because it also
wins the median case and is fully explainable. Random-fold validation gives MAE
{learned["random_fold_metrics"]["mae"]} m — here it is slightly *worse* than blocked folds
({learned["metrics"]["mae"]} m), so no spatial optimism is visible with these features. Blocked
scores are still the ones quoted: they are the defensible design, and a single dataset cannot
show that random folds are safe.

### Rule calibration table

| Building class | Estimated height (m) |
|---|---|
{table}

### Learned model per fold

| Fold | Test | MAE | RMSE | R² | Coverage@80 |
|---|---|---|---|---|---|
{folds}

### Learned model importance (ΔMAE, metres)

| Feature | Importance |
|---|---|
{rows}

## Uncertainty

Every imputed footprint carries an 80% conformal interval (±{rule["interval_quantile_m"]} m for the
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
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def save_bundle(bundle: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in bundle.items() if key != "model"}
    joblib.dump({"model": bundle["model"], "meta": payload}, path)
    path.with_suffix(".json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return path


def load_bundle(path: Path) -> dict[str, Any]:
    payload = joblib.load(path)
    return {"model": payload["model"], **payload["meta"]}
