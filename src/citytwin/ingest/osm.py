"""OpenStreetMap ingestion: fetch, parse and normalise vector layers.

The importer reads the OSM API 0.6 XML directly, so the only dependency is
``requests`` -- no GDAL/OSMium needed. Outputs are GeoJSON in WGS84, which is
what a downstream OGC API or STAC catalogue expects.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, Point, Polygon

from ..config import AOI, CRS_GEOGRAPHIC, RAW_DIR, STOREY_HEIGHT_M

OSM_API = "https://api.openstreetmap.org/api/0.6/map"
USER_AGENT = "citytwin/0.5 (research prototype; contact: caoqy.qianyi@gmail.com)"

GREEN_LEISURE = {"park", "garden", "playground", "pitch", "nature_reserve", "common"}
GREEN_LANDUSE = {
    "grass",
    "forest",
    "meadow",
    "village_green",
    "recreation_ground",
    "orchard",
    "flowerbed",
}
GREEN_NATURAL = {"wood", "scrub", "grassland", "heath"}
STREET_CLASSES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "pedestrian",
    "service",
    "footway",
    "cycleway",
    "path",
}


@dataclass
class Layers:
    buildings: gpd.GeoDataFrame
    green: gpd.GeoDataFrame
    trees: gpd.GeoDataFrame
    streets: gpd.GeoDataFrame
    metadata: dict[str, Any] = field(default_factory=dict)


def fetch_osm(
    aoi: AOI,
    dest: Path | None = None,
    timeout: int = 120,
    retries: int = 3,
    refresh: bool = False,
) -> Path:
    """Download the raw OSM XML for ``aoi``; cached unless the file is absent or ``refresh``."""
    dest = Path(dest) if dest else RAW_DIR / f"osm_{aoi.slug}.xml"
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(
                OSM_API,
                params={"bbox": aoi.bbox_str},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            response.raise_for_status()
            dest.write_bytes(response.content)
            return dest
        except Exception as exc:  # network flakiness is expected on the public API
            last_error = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"could not fetch OSM data for {aoi.name}: {last_error}")


def _parse_height(tags: dict[str, str]) -> tuple[float, str]:
    """Return (height_m, source) following the documented tag priority."""
    raw_height = tags.get("height", "").replace(",", ".").strip()
    if raw_height:
        value = "".join(ch for ch in raw_height if ch.isdigit() or ch == ".")
        try:
            parsed = float(value)
        except ValueError:
            parsed = 0.0
        if 0 < parsed < 300:
            return parsed, "height_tag"
    levels = tags.get("building:levels", "").replace(",", ".").strip()
    if levels:
        try:
            parsed_levels = float(levels)
        except ValueError:
            parsed_levels = 0.0
        if 0 < parsed_levels < 60:
            return parsed_levels * STOREY_HEIGHT_M, "levels"
    return float("nan"), "missing"


def _as_polygon(coords: list[tuple[float, float]]) -> Polygon | None:
    if len(coords) < 4:
        return None
    ring = list(coords)
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        return None
    polygon = Polygon(ring)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.geom_type != "Polygon":
        return None
    return polygon


def parse_osm(path: Path, aoi: AOI) -> Layers:
    """Parse an OSM XML file into normalised GeoDataFrames."""
    root = ET.parse(path).getroot()
    nodes: dict[str, tuple[float, float, dict[str, str]]] = {}
    ways: list[ET.Element] = []
    for element in root:
        if element.tag == "node":
            tags = {t.get("k", ""): t.get("v", "") for t in element.findall("tag")}
            nodes[element.get("id", "")] = (
                float(element.get("lon", "nan")),
                float(element.get("lat", "nan")),
                tags,
            )
        elif element.tag == "way":
            ways.append(element)

    building_rows: list[dict[str, Any]] = []
    green_rows: list[dict[str, Any]] = []
    street_rows: list[dict[str, Any]] = []

    for way in ways:
        tags = {t.get("k", ""): t.get("v", "") for t in way.findall("tag")}
        coords = [
            (nodes[ref][0], nodes[ref][1])
            for ref in (n.get("ref", "") for n in way.findall("nd"))
            if ref in nodes
        ]
        if len(coords) < 2:
            continue
        way_id = f"way/{way.get('id', '')}"

        if "building" in tags:
            polygon = _as_polygon(coords)
            if polygon is not None and polygon.area > 0:
                height, height_source = _parse_height(tags)
                building_rows.append(
                    {
                        "osm_id": way_id,
                        "building_type": tags.get("building", "yes") or "yes",
                        "height_m": height,
                        "height_source": height_source,
                        "levels": tags.get("building:levels"),
                        "name": tags.get("name"),
                        "geometry": polygon,
                    }
                )
            continue

        is_green = (
            tags.get("leisure") in GREEN_LEISURE
            or tags.get("landuse") in GREEN_LANDUSE
            or tags.get("natural") in GREEN_NATURAL
        )
        if is_green:
            polygon = _as_polygon(coords)
            if polygon is not None and polygon.area > 0:
                green_rows.append(
                    {
                        "osm_id": way_id,
                        "green_type": tags.get("leisure")
                        or tags.get("landuse")
                        or tags.get("natural")
                        or "green",
                        "name": tags.get("name"),
                        "geometry": polygon,
                    }
                )
            continue

        highway = tags.get("highway")
        if highway in STREET_CLASSES:
            street_rows.append(
                {
                    "osm_id": way_id,
                    "highway": highway,
                    "name": tags.get("name"),
                    "geometry": LineString(coords),
                }
            )

    tree_rows = [
        {"osm_id": f"node/{node_id}", "name": tags.get("species"), "geometry": Point(lon, lat)}
        for node_id, (lon, lat, tags) in nodes.items()
        if tags.get("natural") == "tree" and lon == lon and lat == lat
    ]

    def _frame(rows: list[dict[str, Any]]) -> gpd.GeoDataFrame:
        if not rows:
            return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=CRS_GEOGRAPHIC)
        frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS_GEOGRAPHIC)
        return frame[frame.geometry.notna() & ~frame.geometry.is_empty].reset_index(drop=True)

    buildings = _frame(building_rows)
    green = _frame(green_rows)
    trees = _frame(tree_rows)
    streets = _frame(street_rows)

    metadata = {
        "aoi": aoi.name,
        "aoi_slug": aoi.slug,
        "bbox_wgs84": list(aoi.bbox),
        "source": f"{OSM_API}?bbox={aoi.bbox_str}",
        "source_name": "OpenStreetMap (ODbL 1.0)",
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "counts": {
            "buildings": len(buildings),
            "buildings_with_height": int((buildings["height_source"] != "missing").sum())
            if len(buildings)
            else 0,
            "green_polygons": len(green),
            "trees": len(trees),
            "streets": len(streets),
        },
    }
    return Layers(buildings=buildings, green=green, trees=trees, streets=streets, metadata=metadata)


def write_layers(layers: Layers, out_dir: Path) -> dict[str, Path]:
    """Write the normalised layers plus a metadata sidecar, return the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in ("buildings", "green", "trees", "streets"):
        frame: gpd.GeoDataFrame = getattr(layers, name)
        target = out_dir / f"{name}.geojson"
        frame.to_file(target, driver="GeoJSON")
        paths[name] = target
    meta_path = out_dir / "layers_metadata.json"
    meta_path.write_text(json.dumps(layers.metadata, indent=2), encoding="utf-8")
    paths["metadata"] = meta_path
    return paths


def read_layers(out_dir: Path) -> Layers:
    """Read normalised layers written by :func:`write_layers`."""
    readers = {
        name: gpd.read_file(out_dir / f"{name}.geojson")
        for name in ("buildings", "green", "trees", "streets")
    }
    metadata = json.loads((out_dir / "layers_metadata.json").read_text(encoding="utf-8"))
    buildings = readers["buildings"]
    if "height_m" in buildings:
        buildings["height_m"] = pd.to_numeric(buildings["height_m"], errors="coerce")
    return Layers(
        buildings=buildings,
        green=readers["green"],
        trees=readers["trees"],
        streets=readers["streets"],
        metadata=metadata,
    )
