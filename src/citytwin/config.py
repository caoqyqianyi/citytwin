"""Project configuration: study area, coordinate systems, grid and index weights.

Everything that a reviewer might want to change lives here or in
``configs/exposure_weights.json``; nothing is hard-coded in the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Repository layout
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
BUILD_DIR = REPO_ROOT / "build"
DOCS_ASSETS = REPO_ROOT / "docs" / "assets"
WEB_DIR = REPO_ROOT / "web"
CONFIG_DIR = REPO_ROOT / "configs"


# ---------------------------------------------------------------------------
# Coordinate reference systems
# ---------------------------------------------------------------------------
#: Storage / exchange CRS (GeoJSON, OGC API, CityJSON).
CRS_GEOGRAPHIC = "EPSG:4326"
#: Metric CRS for the Luxembourg study area (ETRS89 / UTM zone 32N).
CRS_METRIC = "EPSG:25832"
#: Geocentric CRS used by 3D Tiles / glTF content.
CRS_ECEF = "EPSG:4978"


@dataclass(frozen=True)
class AOI:
    """Area of interest, expressed in WGS84 degrees."""

    name: str
    slug: str
    #: (min_lon, min_lat, max_lon, max_lat)
    bbox: tuple[float, float, float, float]
    description: str = ""

    @property
    def bbox_metric(self) -> tuple[float, float, float, float]:
        from pyproj import Transformer

        t = Transformer.from_crs(CRS_GEOGRAPHIC, CRS_METRIC, always_xy=True)
        x0, y0 = t.transform(self.bbox[0], self.bbox[1])
        x1, y1 = t.transform(self.bbox[2], self.bbox[3])
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    @property
    def bbox_str(self) -> str:
        """OSM API 0.6 ``bbox`` parameter order: minlon,minlat,maxlon,maxlat."""
        return ",".join(f"{v:.6f}" for v in self.bbox)


#: Default study area: Esch-sur-Alzette, Luxembourg (LIST SUBE host city).
ESCH_SUR_ALZETTE = AOI(
    name="Esch-sur-Alzette",
    slug="esch",
    bbox=(5.9730, 49.4920, 5.9860, 49.5020),
    description=(
        "Central Esch-sur-Alzette, Luxembourg: a former steel town undergoing "
        "urban regeneration, 2.5 km from the LIST Belvaux campus."
    ),
)

AOIS: dict[str, AOI] = {ESCH_SUR_ALZETTE.slug: ESCH_SUR_ALZETTE}
DEFAULT_AOI = ESCH_SUR_ALZETTE

#: Analysis grid edge length in metres.
GRID_SIZE_M = 250.0
#: Edge length of the blocks used for spatially blocked cross-validation.
CV_BLOCK_SIZE_M = 300.0
#: Assumed storey height when only ``building:levels`` is mapped.
STOREY_HEIGHT_M = 3.0
#: Sketch-engine default when neither height nor levels is mapped.
DEFAULT_HEIGHT_M = 9.0


@dataclass(frozen=True)
class ExposureWeights:
    """Weights of the four normalised components of the Heat Exposure Index.

    The index is deliberately transparent: a weighted sum over percentile-ranked
    components, so that every cell's score can be decomposed and explained.
    """

    building_height: float = 0.35
    lack_of_green: float = 0.30
    distance_to_green: float = 0.20
    built_up_share: float = 0.15

    def __post_init__(self) -> None:
        total = (
            self.building_height + self.lack_of_green + self.distance_to_green + self.built_up_share
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total:.3f}")

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


DEFAULT_WEIGHTS = ExposureWeights()


def load_weights(path: Path | None = None) -> ExposureWeights:
    """Load index weights from JSON, falling back to the documented defaults."""
    path = path or (CONFIG_DIR / "exposure_weights.json")
    if not path.exists():
        return DEFAULT_WEIGHTS
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ExposureWeights(**raw)


def aoi_from_slug(slug: str | None) -> AOI:
    if slug is None:
        return DEFAULT_AOI
    try:
        return AOIS[slug]
    except KeyError as exc:  # pragma: no cover - user input guard
        raise KeyError(f"unknown AOI {slug!r}; available: {sorted(AOIS)}") from exc
