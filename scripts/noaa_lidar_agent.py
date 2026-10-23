#!/usr/bin/env python3
"""
NOAA Digital Coast Data Access Viewer (DAV) LiDAR & DEM Download Agent
https://coast.noaa.gov/dataviewer/#/lidar/search

Capabilities:
1. Reads & normalizes ANY GeoJSON file (Polygon, MultiPolygon, LineString, Point,
   Multi-Feature, WGS84 EPSG:4326, or projected CRS like Florida State Plane EPSG:2881).
2. Produces:
   - WKT AOI string (`SRID=4269;POLYGON((...))`) for NOAA DAV API queries
   - Strictly schema-compliant `aoi_noaa_compatible.geojson` (`Restricted GeoJSON`)
     for manual upload to https://coast.noaa.gov/dataviewer/#/lidar/search
3. Queries `POST https://coast.noaa.gov/dataviewer/api/v1/missions` for matching
   LiDAR Point Cloud (.laz/.las) and DEM (.tif) missions.
4. Supports two download methods into a user-selected destination folder:
   - `s3`: Instant direct tile download from NOAA's AWS S3 bucket
     (`noaa-nos-coastal-lidar-pds.s3.amazonaws.com`) by spatially intersecting the
     user's GeoJSON AOI with the mission's STAC item collection or `tileindex_*.zip` shapefile.
   - `order`: Submits an official custom processing checkout order to
     `POST https://coast.noaa.gov/dataviewer/api/v1/requests/request/submit` using the
     user's email (`fqiao@hazenandsawyer` -> `fqiao@hazenandsawyer.com`), and polls
     `GET /jobs/request?requestId=<id>` + downloads via `GET /jobs/job/download?jobId=<id>&sendLink`.
5. Provides a built-in interactive web UI (`--serve`) with GeoJSON drag-and-drop upload,
   Leaflet map preview, native Windows folder picker dialog, and live download progress.
"""

from __future__ import annotations

import argparse
import html.parser
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiPoint, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform as shapely_transform, unary_union

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

NOAA_API_BASE = "https://coast.noaa.gov/dataviewer/api/v1"
DEFAULT_EMAIL = "fqiao@hazenandsawyer"
DEFAULT_ORG = "Hazen and Sawyer"
GEOID18_UUID = "994d177c-5f55-4cf2-bb81-1c40aa66a578"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NoaaLidarAgent/1.0"


def normalize_email(email: Optional[str]) -> str:
    """
    Normalize user email so it passes NOAA DAV's backend validator:
    /^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z]{2,63}$/
    Specifically converts 'fqiao@hazenandsawyer' -> 'fqiao@hazenandsawyer.com'.
    """
    raw = (email or DEFAULT_EMAIL).strip()
    if not raw:
        raw = DEFAULT_EMAIL
    if "@" not in raw:
        raw = f"{raw}@hazenandsawyer.com"
    local, domain = raw.split("@", 1)
    if "." not in domain:
        domain = f"{domain}.com"
    return f"{local}@{domain}"


def _http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    data_bytes = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body.strip() else {}


def _http_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _detect_crs_from_geojson_dict(
    data: Dict[str, Any], sample_xy: Tuple[float, float], override_crs: Optional[str] = None
) -> CRS:
    if override_crs:
        return CRS.from_user_input(override_crs)
    crs_obj = data.get("crs")
    if isinstance(crs_obj, dict):
        props = crs_obj.get("properties") or {}
        name = props.get("name") or ""
        if name:
            try:
                return CRS.from_user_input(name)
            except Exception:
                pass
    x, y = sample_xy
    if -180.0 <= x <= 180.0 and -90.0 <= y <= 90.0:
        return CRS.from_epsg(4326)
    # Heuristic for Florida East State Plane (EPSG:2881, US survey feet) common in user's GIS files
    if 500_000 <= x <= 1_100_000 and 200_000 <= y <= 2_000_000:
        return CRS.from_epsg(2881)
    raise ValueError(
        f"Coordinates ({x:.2f}, {y:.2f}) appear to be projected rather than lon/lat, "
        "and no 'crs' tag was found. Please specify --source-crs (e.g., EPSG:2881)."
    )


def _is_valid_num(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return not (math.isnan(val) or math.isinf(val))
    try:
        f = float(val)
        return not (math.isnan(f) or math.isinf(f))
    except (TypeError, ValueError):
        return False


def _extract_first_coord(obj: Any) -> Optional[Tuple[float, float]]:
    if isinstance(obj, (list, tuple)):
        if len(obj) >= 2 and _is_valid_num(obj[0]) and _is_valid_num(obj[1]):
            return float(obj[0]), float(obj[1])
        for item in obj:
            found = _extract_first_coord(item)
            if found is not None:
                return found
    return None


def _strip_coords_to_2d(coords: Any) -> Any:
    """
    Recursively strips 3D/4D coordinates (including 4D ZM arrays with null M values
    like `[-79.1834, 35.5221, 0, null]`) down to 2D `[float(x), float(y)]`.
    """
    if not isinstance(coords, (list, tuple)) or len(coords) == 0:
        return []
    first = coords[0]
    if not isinstance(first, (list, tuple)):
        if len(coords) >= 2 and _is_valid_num(coords[0]) and _is_valid_num(coords[1]):
            return [float(coords[0]), float(coords[1])]
        return []
    out = []
    for item in coords:
        cleaned = _strip_coords_to_2d(item)
        if cleaned:
            out.append(cleaned)
    return out


def _collect_2d_points(coords: Any, out_pts: List[Tuple[float, float]]) -> None:
    """Fast recursive collector of all valid 2D (x, y) tuples from any nested coordinate array."""
    if not isinstance(coords, (list, tuple)) or len(coords) == 0:
        return
    if not isinstance(coords[0], (list, tuple)):
        if len(coords) >= 2 and _is_valid_num(coords[0]) and _is_valid_num(coords[1]):
            out_pts.append((float(coords[0]), float(coords[1])))
        return
    for item in coords:
        _collect_2d_points(item, out_pts)


def _sanitize_geometry_dict_2d(geom_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not geom_dict or not isinstance(geom_dict, dict):
        return None
    gtype = geom_dict.get("type")
    if gtype == "GeometryCollection":
        sub = [
            c for c in (_sanitize_geometry_dict_2d(g) for g in (geom_dict.get("geometries") or []))
            if c is not None
        ]
        return {"type": "GeometryCollection", "geometries": sub} if sub else None
    coords = _strip_coords_to_2d(geom_dict.get("coordinates"))
    if not coords:
        return None
    return {"type": gtype, "coordinates": coords}


def _count_ring_vertices(poly: Polygon) -> int:
    return len(poly.exterior.coords)


def parse_and_normalize_geojson(
    geojson_input: Dict[str, Any] | str | Path,
    source_crs: Optional[str] = None,
    max_vertices: int = 340,
) -> Dict[str, Any]:
    """
    Accepts a file path, JSON string, or parsed GeoJSON dict (including Esri JSON
    and 3D/4D coordinates with null Z/M values like `[x, y, 0, null]`),
    reprojects to EPSG:4326 if needed, converts any geometry/multi-feature set into
    a single clean exterior Polygon with <= max_vertices, and computes all metadata
    required by NOAA Data Access Viewer.
    """
    if isinstance(geojson_input, (str, Path)):
        s_in = str(geojson_input)
        if len(s_in) < 1024 and not s_in.lstrip().startswith("{"):
            p = Path(s_in)
            raw_text = p.read_text(encoding="utf-8") if p.exists() else s_in
        else:
            raw_text = s_in
        data = json.loads(raw_text)
    else:
        data = dict(geojson_input)

    # Support Esri JSON polygon/polyline/point format
    if "geometryType" in data and isinstance(data.get("features"), list):
        gtype_esri = data.get("geometryType") or ""
        features = []
        for idx, feat in enumerate(data["features"]):
            g = feat.get("geometry") or {}
            if "rings" in g and g["rings"]:
                features.append(
                    {
                        "type": "Feature",
                        "id": idx + 1,
                        "properties": {"FID": idx + 1},
                        "geometry": {"type": "Polygon", "coordinates": g["rings"]},
                    }
                )
            elif "paths" in g and g["paths"]:
                features.append(
                    {
                        "type": "Feature",
                        "id": idx + 1,
                        "properties": {"FID": idx + 1},
                        "geometry": {"type": "MultiLineString", "coordinates": g["paths"]},
                    }
                )
            elif _is_valid_num(g.get("x")) and _is_valid_num(g.get("y")):
                features.append(
                    {
                        "type": "Feature",
                        "id": idx + 1,
                        "properties": {"FID": idx + 1},
                        "geometry": {"type": "Point", "coordinates": [float(g["x"]), float(g["y"])]},
                    }
                )
        data = {"type": "FeatureCollection", "features": features}

    raw_geoms: List[Dict[str, Any]] = []
    gtype = data.get("type")
    if gtype == "FeatureCollection":
        for f in data.get("features") or []:
            if f and f.get("geometry"):
                raw_geoms.append(f["geometry"])
    elif gtype == "Feature":
        if data.get("geometry"):
            raw_geoms.append(data["geometry"])
    elif gtype in (
        "Polygon",
        "MultiPolygon",
        "LineString",
        "MultiLineString",
        "Point",
        "MultiPoint",
        "GeometryCollection",
    ):
        raw_geoms.append(data)
    else:
        raise ValueError(f"Unsupported GeoJSON top-level type: {gtype!r}")

    if not raw_geoms:
        raise ValueError("GeoJSON contains no valid geometries.")

    feature_count = len(raw_geoms)

    # Extract all 2D points cleanly (ignoring 3rd/4th Z/M values or null M values)
    all_pts: List[Tuple[float, float]] = []
    for g in raw_geoms:
        if g.get("type") == "GeometryCollection":
            for sub in g.get("geometries") or []:
                _collect_2d_points(sub.get("coordinates"), all_pts)
        else:
            _collect_2d_points(g.get("coordinates"), all_pts)

    if not all_pts:
        raise ValueError("Could not extract any valid 2D coordinates from GeoJSON.")

    sample_coord = all_pts[0]
    detected_crs = _detect_crs_from_geojson_dict(data, sample_coord, override_crs=source_crs)
    epsg4326 = CRS.from_epsg(4326)
    needs_reproj = not detected_crs.equals(epsg4326)
    to_4326 = Transformer.from_crs(detected_crs, epsg4326, always_xy=True).transform if needs_reproj else None

    # Build AOI polygon:
    # - If single Polygon feature, preserve its exact 2D exterior boundary
    # - Otherwise (multi-feature or LineString/Point network like 14,500+ water pipes),
    #   compute the fast 2D MultiPoint convex hull across all unique vertices
    is_single_polygon = (
        len(raw_geoms) == 1
        and raw_geoms[0].get("type") == "Polygon"
    )
    if is_single_polygon:
        clean_geom = _sanitize_geometry_dict_2d(raw_geoms[0])
        if not clean_geom:
            raise ValueError("Polygon geometry has no valid 2D coordinates.")
        merged = shape(clean_geom)
        merged_4326 = shapely_transform(to_4326, merged) if to_4326 else merged
        poly_4326 = Polygon(merged_4326.exterior.coords)
    else:
        # Deduplicate points for ultra-fast convex hull even on 100,000+ vertices
        unique_pts = list(dict.fromkeys(all_pts))
        mp = MultiPoint(unique_pts)
        mp_4326 = shapely_transform(to_4326, mp) if to_4326 else mp
        hull = mp_4326.convex_hull
        if isinstance(hull, Polygon) and hull.area > 1e-10:
            poly_4326 = Polygon(hull.exterior.coords)
        else:
            # Degenerate point/collinear segment: buffer by ~100 meters (~0.001 degrees)
            buffered = mp_4326.buffer(0.001)
            poly_4326 = Polygon(buffered.envelope.exterior.coords)

    if not poly_4326.is_valid:
        poly_4326 = poly_4326.buffer(0)
        if isinstance(poly_4326, MultiPolygon):
            poly_4326 = poly_4326.convex_hull

    original_vertices = _count_ring_vertices(poly_4326)

    # Simplify if vertex count exceeds NOAA DAV's 350-vertex limit
    tolerance = 0.0001
    simplified = poly_4326
    while _count_ring_vertices(simplified) > max_vertices and tolerance < 0.1:
        cand = poly_4326.simplify(tolerance, preserve_topology=True)
        if isinstance(cand, Polygon) and not cand.is_empty:
            simplified = Polygon(cand.exterior.coords)
        tolerance *= 1.5
    if _count_ring_vertices(simplified) > max_vertices:
        simplified = Polygon(poly_4326.convex_hull.exterior.coords)

    poly_4326 = simplified
    simplified_vertices = _count_ring_vertices(poly_4326)

    # Calculate area in square miles using Web Mercator / equal-area projection
    to_3857 = Transformer.from_crs(epsg4326, CRS.from_epsg(3857), always_xy=True).transform
    poly_3857 = shapely_transform(to_3857, poly_4326)
    # Approximate true geodesic area using cos(lat) scaling from EPSG:3857 or equal-area EPSG:5070
    try:
        to_5070 = Transformer.from_crs(epsg4326, CRS.from_epsg(5070), always_xy=True).transform
        area_sq_meters = shapely_transform(to_5070, poly_4326).area
    except Exception:
        lat_rad = math.radians(poly_4326.centroid.y)
        area_sq_meters = poly_3857.area * (math.cos(lat_rad) ** 2)
    area_sq_miles = area_sq_meters / 2_589_988.110336

    # Build ring coordinates (2D [lon, lat], closed ring)
    coords_2d = [[round(float(x), 7), round(float(y), 7)] for x, y, *_ in poly_4326.exterior.coords]
    if coords_2d[0] != coords_2d[-1]:
        coords_2d.append(list(coords_2d[0]))

    wkt_Pairs = ", ".join(f"{pt[0]} {pt[1]}" for pt in coords_2d)
    wkt_aoi = f"SRID=4269;POLYGON(({wkt_Pairs}))"

    minx, miny, maxx, maxy = poly_4326.bounds
    merc_minx, merc_miny, merc_maxx, merc_maxy = poly_3857.bounds

    # Strictly compliant Restricted GeoJSON for NOAA DAV web upload
    noaa_restricted_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": 1,
                "properties": {"FID": 1, "Id": 0},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords_2d],
                },
            }
        ],
    }

    return {
        "source_crs": detected_crs.to_string(),
        "feature_count": feature_count,
        "original_vertex_count": original_vertices,
        "simplified_vertex_count": simplified_vertices,
        "area_sq_miles": round(area_sq_miles, 4),
        "area_acres": round(area_sq_miles * 640.0, 2),
        "bbox_4326": [round(minx, 7), round(miny, 7), round(maxx, 7), round(maxy, 7)],
        "bbox_3857": [
            round(merc_minx, 3),
            round(merc_miny, 3),
            round(merc_maxx, 3),
            round(merc_maxy, 3),
        ],
        "wkt_aoi": wkt_aoi,
        "polygon_4326": poly_4326,
        "noaa_restricted_geojson": noaa_restricted_geojson,
    }


def search_missions(
    wkt_aoi: str,
    data_types: Optional[List[str]] = None,
    page_size: int = 50,
) -> List[Dict[str, Any]]:
    """
    Queries POST https://coast.noaa.gov/dataviewer/api/v1/missions for all published
    LiDAR and DEM missions intersecting `wkt_aoi`.
    """
    if not data_types:
        data_types = ["Lidar", "DEM"]
    payload = {
        "aoi": wkt_aoi,
        "dataTypes": data_types,
        "published": "true",
        "page": 0,
        "pageSize": page_size,
        "sort": "desc",
        "sortField": "year",
    }
    resp = _http_json(f"{NOAA_API_BASE}/missions", method="POST", payload=payload, timeout=30)
    raw_items = (resp.get("missions") or {}).get("data") or []
    results = []
    for item in raw_items:
        attrs = item.get("attributes") or item
        links = attrs.get("links") or attrs.get("Links") or item.get("Links") or item.get("links") or []
        bulk_url = None
        metadata_url = None
        wms_url = None
        for lnk in links:
            lid = str(lnk.get("linkTypeId") or "")
            ltitle = (lnk.get("title") or lnk.get("linkTypeName") or "").lower()
            uri = (lnk.get("uri") or lnk.get("link") or "").strip()
            if lid == "46" or "bulk download" in ltitle:
                bulk_url = uri
            elif lid in ("35", "39") or "metadata" in ltitle:
                metadata_url = uri
            elif lid == "43" or "web map service" in ltitle:
                wms_url = uri

        providers = [p.get("name") or p.get("abbreviated_name") for p in (attrs.get("providers") or []) if p]
        vdatum_list = attrs.get("verticalDatum") or []
        vdatum = vdatum_list[0].get("name") if vdatum_list and isinstance(vdatum_list[0], dict) else None
        hdatum_list = attrs.get("horizontalDatum") or []
        hdatum = hdatum_list[0].get("name") if hdatum_list and isinstance(hdatum_list[0], dict) else None

        is_s3_bulk = bool(
            bulk_url
            and (
                "amazonaws.com" in bulk_url.lower()
                or bulk_url.lower().endswith("index.html")
                or "coast.noaa.gov/htdata" in bulk_url.lower()
            )
        )

        results.append(
            {
                "missionId": str(item.get("missionId") or item.get("id") or attrs.get("id")),
                "title": item.get("title") or attrs.get("title"),
                "year": attrs.get("year") or attrs.get("date") or item.get("year"),
                "dataType": item.get("dataType") or attrs.get("dataType"),
                "providers": providers,
                "horizontalDatum": hdatum,
                "verticalDatum": vdatum,
                "bulkDownloadUrl": bulk_url if is_s3_bulk else None,
                "externalPortalUrl": bulk_url if (bulk_url and not is_s3_bulk) else None,
                "metadataUrl": metadata_url,
                "wmsUrl": wms_url,
            }
        )
    return results


def get_mission_details_and_zones(mission_id: str | int, wkt_aoi: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Fetches detailed mission info (centroid, sizeEstimate, groundClasses, etc.) and
    available projection zones (State Plane NAD83 / UTM) for a specific mission & AOI.
    """
    payload = {
        "aoi": wkt_aoi,
        "published": "true",
        "missionIds": [int(mission_id)],
        "sort": "asc",
        "pageSize": 10,
        "page": 0,
        "getGeoJson": "true",
    }
    resp = _http_json(f"{NOAA_API_BASE}/missions", method="POST", payload=payload, timeout=30)
    data_list = (resp.get("missions") or {}).get("data") or []
    mission = data_list[0] if data_list else {}
    centroid = mission.get("centroid")
    zones = {}
    if centroid:
        zones = _http_json(
            f"{NOAA_API_BASE}/search/projectionZones",
            method="POST",
            payload={"centroid": centroid, "aoi": wkt_aoi},
            timeout=20,
        )
    return mission, zones


class _HrefParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            for k, v in attrs:
                if k.lower() == "href" and v:
                    self.hrefs.append(v.strip())


def discover_s3_index_urls(bulk_download_url: str, mission_id: str) -> Dict[str, Optional[str]]:
    """
    Inspects the S3 `index.html` page from a mission's `bulkDownloadUrl` and finds:
    - `stac_collection_url`: `stac/noaa_item_collection_m<id>.json`
    - `tileindex_zip_url`: `tileindex_*.zip` (shapefile index)
    - `urllist_url`: `urllist*.txt`
    """
    html_bytes = _http_bytes(bulk_download_url, timeout=30)
    html_text = html_bytes.decode("utf-8", errors="ignore")
    parser = _HrefParser()
    parser.feed(html_text)

    stac_url = None
    tileindex_zip = None
    tileindex_gpkg = None
    urllist_url = None

    for href in parser.hrefs:
        full_url = urllib.parse.urljoin(bulk_download_url, href)
        lower = href.lower()
        if "noaa_item_collection" in lower and lower.endswith(".json"):
            stac_url = full_url
        elif "tileindex" in lower and lower.endswith(".zip"):
            tileindex_zip = full_url
        elif "tileindex" in lower and lower.endswith(".gpkg"):
            tileindex_gpkg = full_url
        elif "urllist" in lower and lower.endswith(".txt"):
            urllist_url = full_url

    return {
        "bulk_index_url": bulk_download_url,
        "stac_collection_url": stac_url,
        "tileindex_zip_url": tileindex_zip,
        "tileindex_gpkg_url": tileindex_gpkg,
        "urllist_url": urllist_url,
    }


def find_intersecting_s3_tiles(
    bulk_download_url: str,
    mission_id: str,
    aoi_polygon_4326: Polygon,
) -> Dict[str, Any]:
    """
    Finds all S3 tiles (.tif or .copc.laz/.laz) whose footprints intersect `aoi_polygon_4326`.
    Prefers `tileindex_*.zip` (fast, compact shapefile available for both LAZ and DEM)
    with fallback to `stac/noaa_item_collection_*.json`.
    """
    index_info = discover_s3_index_urls(bulk_download_url, mission_id)
    tiles: List[Dict[str, Any]] = []
    index_source = None

    if index_info.get("tileindex_zip_url"):
        index_source = index_info["tileindex_zip_url"]
        zip_bytes = _http_bytes(index_source, timeout=60)
        with tempfile.TemporaryDirectory() as tmpdir:
            zpath = os.path.join(tmpdir, "tileindex.zip")
            with open(zpath, "wb") as f:
                f.write(zip_bytes)
            gdf = gpd.read_file(f"zip://{zpath}")
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
            hits = gdf[gdf.geometry.intersects(aoi_polygon_4326)].copy()
            for _, row in hits.iterrows():
                url_val = None
                for col in ("url", "URL", "Url", "location", "LOCATION", "href"):
                    if col in row and row[col] and str(row[col]).startswith("http"):
                        url_val = str(row[col]).strip()
                        break
                fname = None
                for col in ("filename", "FILENAME", "location", "LOCATION", "name", "tile"):
                    if col in row and row[col]:
                        fname = os.path.basename(str(row[col]).strip())
                        break
                if not fname and url_val:
                    fname = os.path.basename(urllib.parse.urlparse(url_val).path)
                if url_val:
                    tiles.append(
                        {
                            "filename": fname or "tile.dat",
                            "url": url_val,
                            "bbox": [round(v, 6) for v in row.geometry.bounds],
                            "geometry": mapping(row.geometry),
                        }
                    )
    elif index_info.get("stac_collection_url"):
        index_source = index_info["stac_collection_url"]
        stac_data = _http_json(index_source, timeout=60)
        for feat in stac_data.get("features") or []:
            geom_dict = feat.get("geometry")
            if not geom_dict:
                continue
            geom = shape(geom_dict)
            if geom.intersects(aoi_polygon_4326):
                assets = feat.get("assets") or {}
                for _, asset in assets.items():
                    href = asset.get("href")
                    if href:
                        fname = os.path.basename(urllib.parse.urlparse(href).path)
                        tiles.append(
                            {
                                "filename": fname,
                                "url": href,
                                "bbox": [round(v, 6) for v in geom.bounds],
                                "geometry": mapping(geom),
                            }
                        )
                        break

    return {
        "missionId": str(mission_id),
        "index_source": index_source,
        "tile_count": len(tiles),
        "tiles": tiles,
    }


def download_s3_tiles(
    tiles: List[Dict[str, Any]],
    output_dir: str | Path,
    max_tiles: int = 25,
    progress_callback: Optional[ Any ] = None,
) -> List[Dict[str, Any]]:
    """
    Downloads the intersecting tiles from NOAA S3 into `output_dir`.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    selected = tiles if max_tiles <= 0 else tiles[:max_tiles]
    downloaded: List[Dict[str, Any]] = []

    for idx, tile in enumerate(selected, start=1):
        url = tile["url"]
        fname = tile["filename"]
        dest = out_path / fname
        if progress_callback:
            progress_callback(idx, len(selected), fname, "downloading")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            total_bytes = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total_bytes += len(chunk)
        downloaded.append(
            {
                "filename": fname,
                "path": str(dest.resolve()),
                "url": url,
                "size_bytes": total_bytes,
                "size_mb": round(total_bytes / (1024 * 1024), 2),
            }
        )
        if progress_callback:
            progress_callback(idx, len(selected), fname, "completed")

    return downloaded


def submit_noaa_dav_order(
    aoi_info: Dict[str, Any],
    mission_id: str | int,
    email: Optional[str] = DEFAULT_EMAIL,
    organization: str = DEFAULT_ORG,
    product_mode: str = "default",
) -> Dict[str, Any]:
    """
    Submits a custom checkout job to `POST https://coast.noaa.gov/dataviewer/api/v1/requests/request/submit`
    using the normalized user email (`fqiao@hazenandsawyer.com`).
    """
    user_email = normalize_email(email)
    wkt_aoi = aoi_info["wkt_aoi"]
    minx, miny, maxx, maxy = aoi_info["bbox_4326"]
    m_minx, m_miny, m_maxx, m_maxy = aoi_info["bbox_3857"]

    mission_detail, zones = get_mission_details_and_zones(mission_id, wkt_aoi)
    dtype = mission_detail.get("dataType") or "Lidar"
    title = mission_detail.get("title") or f"NOAA Dataset {mission_id}"
    attrs = (mission_detail.get("attributes") or {}).get("details") or {}
    lidar_details = attrs.get("lidar") or {}

    # Choose State Plane NAD83 (US Survey Feet) if available for the centroid, else UTM (Meters)
    sp83_list = zones.get("sp83") or []
    utm_list = zones.get("utm") or []
    if sp83_list and sp83_list[0].get("projectionZonesId"):
        projection_id = "2"  # State Plane
        zones_id = str(sp83_list[0]["projectionZonesId"])
        horiz_units_id = "3"  # Survey Feet
        vert_units_id = "3"   # Survey Feet
    elif utm_list and utm_list[0].get("utmZone"):
        projection_id = "1"  # UTM
        zones_id = str(utm_list[0]["utmZone"])
        horiz_units_id = "1"  # Meters
        vert_units_id = "1"   # Meters
    else:
        projection_id = "5"  # Geographic
        zones_id = None
        horiz_units_id = "4"  # Decimal Degrees
        vert_units_id = "1"   # Meters

    job_obj: Dict[str, Any] = {
        "label": title,
        "missionId": str(mission_id),
        "minX": minx,
        "minY": miny,
        "maxX": maxx,
        "maxY": maxy,
        "mercMinX": m_minx,
        "mercMinY": m_miny,
        "mercMaxX": m_maxx,
        "mercMaxY": m_maxy,
        "horizontalDatumId": "2",  # NAD83
        "horizontalUnitsId": horiz_units_id,
        "verticalDatumId": "3",    # NAVD88
        "verticalUnitsId": vert_units_id,
        "projectionId": projection_id,
        "aoi": wkt_aoi,
        "vDatumLabel": "NAVD88",
        "geoidModel": GEOID18_UUID,
    }
    if zones_id:
        job_obj["zonesId"] = zones_id

    if dtype.lower() == "dem":
        job_obj["dataType"] = "DEM"
        job_obj["dataTypeId"] = 2
        job_obj["fileFormatId"] = "1"  # GeoTIFF
    else:
        job_obj["dataType"] = "Lidar"
        job_obj["dataTypeId"] = 5
        job_obj["addAncillaryData"] = "0"
        job_obj["dataReturnsSelection"] = "0"  # Any returns
        ground_classes = lidar_details.get("groundClasses") or "2"
        job_obj["lidarDataClasses"] = str(ground_classes)

        if product_mode == "point":
            job_obj["products"] = {"point": {"fileFormatId": "13"}}  # LAZ point cloud
        elif product_mode == "both":
            job_obj["products"] = {
                "point": {"fileFormatId": "13"},
                "grid": {
                    "fileFormatId": "8",  # GeoTIFF DEM from LiDAR
                    "binMethodId": "2",   # TIN
                    "binUnitsId": horiz_units_id if horiz_units_id in ("1", "2", "3") else "1",
                    "binSize": "3",
                    "fillGaps": "true",
                },
            }
        else:
            # Default: request both LAZ point cloud and GeoTIFF DEM grid
            job_obj["products"] = {
                "point": {"fileFormatId": "13"},
                "grid": {
                    "fileFormatId": "8",
                    "binMethodId": "2",
                    "binUnitsId": horiz_units_id if horiz_units_id in ("1", "2", "3") else "1",
                    "binSize": "3",
                    "fillGaps": "true",
                },
            }

    submit_payload = {
        "jobs": {
            "tier": "production",
            "userEmail": user_email,
            "organization": organization or DEFAULT_ORG,
            "surveyOptIn": "false",
            "digest": "false",
            "job": [job_obj],
        }
    }

    resp = _http_json(
        f"{NOAA_API_BASE}/requests/request/submit",
        method="POST",
        payload=submit_payload,
        timeout=30,
    )
    req_id = (resp.get("data") or {}).get("requestId") or resp.get("requestId")
    return {
        "requestId": req_id,
        "userEmail": user_email,
        "missionId": str(mission_id),
        "title": title,
        "dataType": dtype,
        "statusUrl": f"https://coast.noaa.gov/dataviewer/#/admin/request/{req_id}" if req_id else None,
        "apiStatusUrl": f"{NOAA_API_BASE}/jobs/request?requestId={req_id}" if req_id else None,
        "rawResponse": resp,
    }


def check_and_download_dav_request(
    request_id: str | int,
    output_dir: str | Path,
    wait: bool = False,
    poll_interval: int = 15,
    max_wait_seconds: int = 600,
) -> Dict[str, Any]:
    """
    Checks the status of a NOAA DAV `requestId` (`GET /jobs/request?requestId=<id>`),
    and if `requestStatus == "complete"`, fetches the signed zip download URL from
    `GET /jobs/job/download?jobId=<jobId>&sendLink` and saves the `.zip` into `output_dir`.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    start_time = time.time()

    while True:
        status_data = _http_json(f"{NOAA_API_BASE}/jobs/request?requestId={request_id}", timeout=30)
        req_status = (status_data.get("requestStatus") or "").lower()
        jobs = status_data.get("jobs") or []

        if req_status in ("complete", "failed") or not wait:
            break
        if time.time() - start_time >= max_wait_seconds:
            break
        time.sleep(poll_interval)

    downloaded_zips: List[Dict[str, Any]] = []
    if (status_data.get("requestStatus") or "").lower() == "complete":
        for job in status_data.get("jobs") or []:
            job_id = job.get("jobId")
            if not job_id:
                continue
            dl_info = _http_json(f"{NOAA_API_BASE}/jobs/job/download?jobId={job_id}&sendLink", timeout=30)
            zip_url = dl_info.get("url")
            if zip_url:
                fname = os.path.basename(urllib.parse.urlparse(zip_url).path.strip()) or f"noaa_job_{job_id}.zip"
                dest = out_path / fname
                req = urllib.request.Request(zip_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
                    total = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                downloaded_zips.append(
                    {
                        "jobId": job_id,
                        "filename": fname,
                        "path": str(dest.resolve()),
                        "url": zip_url,
                        "size_bytes": total,
                    }
                )

    return {
        "requestId": request_id,
        "requestStatus": status_data.get("requestStatus"),
        "completedJobs": status_data.get("completedJobs"),
        "jobs": status_data.get("jobs"),
        "downloadedZips": downloaded_zips,
    }


def pick_folder_dialog(initial_dir: Optional[str] = None) -> Optional[str]:
    """Opens a native Windows folder selection dialog via tkinter."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            initialdir=initial_dir or os.path.expanduser("~"),
            title="Select Folder to Save NOAA LiDAR / DEM Files",
        )
        root.destroy()
        if selected:
            return str(Path(selected).resolve())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Built-in Interactive Web UI Server (`--serve`)
# ---------------------------------------------------------------------------

HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>NOAA LiDAR & DEM Download Agent</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --card: #334155;
    --accent: #0284c7;
    --accent-hover: #0369a1;
    --green: #059669;
    --text: #f8fafc;
    --muted: #94a3b8;
    --border: #475569;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    flex-direction: column;
    height: 100vh;
  }
  header {
    background: #0b1120;
    border-bottom: 1px solid var(--border);
    padding: 12px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  header .badge {
    background: var(--accent);
    color: #fff;
    padding: 3px 8px;
    border-radius: 99px;
    font-size: 11px;
    font-weight: 600;
  }
  .main {
    display: grid;
    grid-template-columns: 430px 1fr;
    flex: 1;
    overflow: hidden;
  }
  .sidebar {
    background: var(--panel);
    border-right: 1px solid var(--border);
    padding: 16px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .section {
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
  }
  .section h2 {
    margin: 0 0 8px 0;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #38bdf8;
  }
  label {
    display: block;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 4px;
  }
  input[type="text"], input[type="email"], select {
    width: 100%;
    padding: 8px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: #0f172a;
    color: var(--text);
    font-size: 13px;
    margin-bottom: 8px;
  }
  .row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .row input {
    margin-bottom: 0;
    flex: 1;
  }
  button {
    background: var(--accent);
    color: white;
    border: none;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s;
  }
  button:hover { background: var(--accent-hover); }
  button.secondary {
    background: var(--card);
    border: 1px solid var(--border);
  }
  button.secondary:hover { background: #475569; }
  button.success { background: var(--green); }
  button.success:hover { background: #047857; }
  .dropzone {
    border: 2px dashed #38bdf8;
    border-radius: 8px;
    padding: 16px;
    text-align: center;
    background: rgba(2, 132, 199, 0.08);
    cursor: pointer;
  }
  .dropzone:hover { background: rgba(2, 132, 199, 0.15); }
  .stats {
    font-size: 12px;
    color: #cbd5e1;
    margin-top: 8px;
    line-height: 1.5;
  }
  .mission-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px;
    margin-bottom: 8px;
  }
  .mission-title {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .mission-meta {
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .mission-actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .mission-actions button {
    font-size: 11px;
    padding: 6px 9px;
  }
  #map {
    width: 100%;
    height: 100%;
  }
  #logBox {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px;
    background: #090d16;
    color: #a7f3d0;
    padding: 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    max-height: 180px;
    overflow-y: auto;
    white-space: pre-wrap;
  }
  .status-banner {
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    font-size: 13px;
  }
  .status-banner.running {
    display: flex;
    background: #1e3a8a;
    border-bottom-color: #38bdf8;
  }
  .status-banner.completed {
    display: flex;
    background: #064e3b;
    border-bottom-color: #10b981;
  }
  .status-banner.error {
    display: flex;
    background: #7f1d1d;
    border-bottom-color: #f87171;
  }
  .progress-wrap {
    flex: 1;
    max-width: 340px;
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid rgba(255,255,255,0.2);
    border-radius: 99px;
    height: 14px;
    overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    background: linear-gradient(90deg, #38bdf8, #10b981);
    width: 0%;
    transition: width 0.25s ease;
  }
</style>
</head>
<body>
<header>
  <h1>
    <span>NOAA Digital Coast LiDAR & DEM Agent</span>
    <span class="badge">coast.noaa.gov/dataviewer</span>
  </h1>
  <div style="font-size:12px;color:var(--muted);">
    Email: <strong style="color:#38bdf8;">fqiao@hazenandsawyer.com</strong>
  </div>
</header>
<div id="statusBanner" class="status-banner">
  <div id="statusText" style="flex:1;font-weight:600;"></div>
  <div id="progressWrap" class="progress-wrap" style="display:none;">
    <div id="progressBar" class="progress-bar"></div>
  </div>
  <div id="statusActions" style="display:flex;gap:8px;align-items:center;"></div>
</div>
<div class="main">
  <div class="sidebar">
    <div class="section">
      <h2>1. Upload GeoJSON AOI</h2>
      <div class="dropzone" id="dropzone" onclick="document.getElementById('fileInput').click()">
        <input type="file" id="fileInput" accept=".geojson,.json" style="display:none" onchange="handleFile(this.files[0])" />
        <div style="font-weight:600;margin-bottom:4px;">Click or Drop a .geojson / .json File Here</div>
        <div style="font-size:11px;color:var(--muted);">Supports WGS84 (EPSG:4326) & State Plane (e.g. EPSG:2881), Polygons, Pipes, or Points</div>
      </div>
      <div class="row" style="margin-top:8px;">
        <label style="margin:0;white-space:nowrap;">Source CRS:</label>
        <input type="text" id="sourceCrs" placeholder="Auto-detect (e.g. EPSG:4326 or EPSG:2881)" />
      </div>
      <div id="aoiStats" class="stats" style="display:none;"></div>
    </div>

    <div class="section">
      <h2>2. Destination Folder & Email</h2>
      <label>Folder to Save Downloaded LiDAR / DEM Files (Required):</label>
      <div class="row" style="margin-bottom:8px;">
        <input type="text" id="outputDir" placeholder="C:\\Users\\fqiao\\Downloads\\NOAA_Lidar" />
        <button class="secondary" onclick="browseFolder()" title="Open Windows Folder Picker">Browse...</button>
      </div>
      <label>Email Address for NOAA Data Access Viewer:</label>
      <input type="email" id="userEmail" value="fqiao@hazenandsawyer.com" />
      <label>Max Direct S3 Tiles per Download (0 = all intersecting tiles):</label>
      <input type="text" id="maxTiles" value="10" />
    </div>

    <div class="section">
      <h2>3. Available NOAA LiDAR & DEM Datasets</h2>
      <div id="missionsList" style="font-size:12px;color:var(--muted);">
        Upload a GeoJSON file above to search intersecting NOAA missions.
      </div>
    </div>

    <div class="section">
      <h2>4. Activity & Download Log</h2>
      <div id="logBox">Ready. Upload a GeoJSON file and choose a save folder.</div>
    </div>
  </div>
  <div id="map"></div>
</div>

<script>
const map = L.map('map').setView([26.05, -80.15], 10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

let aoiLayer = null;
let tilesLayer = L.layerGroup().addTo(map);
let currentAoiInfo = null;

function log(msg) {
  const box = document.getElementById('logBox');
  const ts = new Date().toLocaleTimeString();
  box.textContent += `\\n[${ts}] ${msg}`;
  box.scrollTop = box.scrollHeight;
}

const dz = document.getElementById('dropzone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.style.borderColor = '#10b981'; });
dz.addEventListener('dragleave', e => { e.preventDefault(); dz.style.borderColor = '#38bdf8'; });
dz.addEventListener('drop', e => {
  e.preventDefault();
  dz.style.borderColor = '#38bdf8';
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});

async function browseFolder() {
  log('Opening native Windows folder picker...');
  const current = document.getElementById('outputDir').value;
  const res = await fetch('/api/pick-folder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({initialDir: current})
  });
  const data = await res.json();
  if (data.folder) {
    document.getElementById('outputDir').value = data.folder;
    log('Selected save folder: ' + data.folder);
  } else {
    log('Folder selection cancelled.');
  }
}

function applyLoadedGeojson(filename, data) {
  currentAoiInfo = data.aoi;
  if (aoiLayer) map.removeLayer(aoiLayer);
  tilesLayer.clearLayers();
  aoiLayer = L.geoJSON(data.aoi.noaa_restricted_geojson, {
    style: {color: '#38bdf8', weight: 3, fillOpacity: 0.2}
  }).addTo(map);
  map.fitBounds(aoiLayer.getBounds(), {padding: [30, 30]});

  const statsEl = document.getElementById('aoiStats');
  statsEl.style.display = 'block';
  statsEl.innerHTML = `
    <strong>File:</strong> ${filename} (${(data.aoi.feature_count || 1).toLocaleString()} features)<br/>
    <strong>Detected CRS:</strong> ${data.aoi.source_crs} &rarr; EPSG:4326<br/>
    <strong>Area:</strong> ${data.aoi.area_sq_miles} sq mi (${data.aoi.area_acres} acres)<br/>
    <strong>Vertices:</strong> ${data.aoi.original_vertex_count} (simplified to ${data.aoi.simplified_vertex_count})
  `;
  log(`Normalized AOI (${data.aoi.area_sq_miles} sq mi, ${data.aoi.feature_count || 1} features). Found ${data.missions.length} NOAA LiDAR/DEM datasets.`);
  renderMissions(data.missions);
}

async function handleFile(file) {
  if (!file) return;
  log(`Reading ${file.name} (${(file.size / (1024 * 1024)).toFixed(2)} MB)...`);
  const text = await file.text();
  const sourceCrs = document.getElementById('sourceCrs').value.trim();
  const res = await fetch('/api/upload-geojson', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filename: file.name, content: text, sourceCrs: sourceCrs || null})
  });
  const data = await res.json();
  if (!res.ok || data.error) {
    log('ERROR: ' + (data.error || 'Failed to parse GeoJSON'));
    alert(data.error || 'Failed to parse GeoJSON');
    return;
  }
  applyLoadedGeojson(file.name, data);
}

function updateStatusBanner(prog) {
  const banner = document.getElementById('statusBanner');
  const textEl = document.getElementById('statusText');
  const pWrap = document.getElementById('progressWrap');
  const pBar = document.getElementById('progressBar');
  const actionsEl = document.getElementById('statusActions');
  if (!prog || prog.state === 'idle') {
    banner.className = 'status-banner';
    return;
  }
  if (prog.state === 'indexing' || prog.state === 'downloading') {
    banner.className = 'status-banner running';
    pWrap.style.display = 'block';
    pBar.style.width = `${prog.percent || 5}%`;
    textEl.innerHTML = `⏳ <strong>DOWNLOAD IN PROGRESS (${prog.percent || 0}%):</strong> ${prog.message || 'Downloading...'}`;
    actionsEl.innerHTML = `<span style="font-size:12px;color:#bae6fd;">Saving to: ${prog.outputDir || ''}</span>`;
  } else if (prog.state === 'completed') {
    banner.className = 'status-banner completed';
    pWrap.style.display = 'block';
    pBar.style.width = '100%';
    const count = (prog.downloadedFiles || []).length;
    const totalMb = (prog.downloadedFiles || []).reduce((acc, f) => acc + (f.size_mb || 0), 0).toFixed(2);
    textEl.innerHTML = `✅ <strong>DOWNLOAD FINISHED!</strong> Saved <strong>${count} file(s) (${totalMb} MB)</strong> for Mission ${prog.missionId} to <code>${prog.outputDir}</code>`;
    const safeDir = (prog.outputDir || '').replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'");
    actionsEl.innerHTML = `<button class="secondary" style="padding:5px 10px;font-size:12px;" onclick="openFolder('${safeDir}')">📂 Open Folder in Explorer</button>`;
  } else if (prog.state === 'order_submitted') {
    banner.className = 'status-banner completed';
    pWrap.style.display = 'none';
    textEl.innerHTML = `✅ <strong>NOAA ORDER SUBMITTED!</strong> Request ID <strong>#${prog.requestId}</strong> (${prog.userEmail}). Receipt saved to <code>${prog.outputDir}</code>`;
    const safeDir = (prog.outputDir || '').replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'");
    actionsEl.innerHTML = `
      ${prog.statusUrl ? `<button class="secondary" style="padding:5px 10px;font-size:12px;" onclick="window.open('${prog.statusUrl}', '_blank')">🌐 View NOAA Status</button>` : ''}
      <button class="secondary" style="padding:5px 10px;font-size:12px;" onclick="openFolder('${safeDir}')">📂 Open Folder</button>
    `;
  } else if (prog.state === 'error') {
    banner.className = 'status-banner error';
    pWrap.style.display = 'none';
    textEl.innerHTML = `❌ <strong>DOWNLOAD ERROR:</strong> ${prog.message || 'Failed'}`;
    actionsEl.innerHTML = '';
  }
}

async function openFolder(folderPath) {
  await fetch('/api/open-folder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({folder: folderPath})
  });
}

let pollTimer = null;
function startProgressPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/download-progress');
      if (res.ok) {
        const prog = await res.json();
        updateStatusBanner(prog);
        const btn = prog.missionId ? document.getElementById(`btn-s3-${prog.missionId}`) : null;
        if (btn) {
          if (prog.state === 'indexing' || prog.state === 'downloading') {
            btn.textContent = `⏳ Downloading (${prog.currentTile || 0}/${prog.totalTiles || '?'} - ${prog.percent || 0}%)...`;
            btn.disabled = true;
          } else if (prog.state === 'completed') {
            btn.textContent = `✅ Finished! (${(prog.downloadedFiles || []).length} Tiles Saved)`;
            btn.disabled = false;
          } else {
            btn.disabled = false;
          }
        }
        if (prog.state === 'completed' || prog.state === 'error' || prog.state === 'idle') {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      }
    } catch (e) {}
  }, 500);
}

window.addEventListener('DOMContentLoaded', async () => {
  try {
    const res = await fetch('/api/preload');
    if (res.ok) {
      const data = await res.json();
      if (data && data.aoi) {
        applyLoadedGeojson(data.filename || 'preloaded.geojson', data);
      }
      if (data && data.lastProgress && data.lastProgress.state !== 'idle') {
        if (data.lastProgress.outputDir) {
          document.getElementById('outputDir').value = data.lastProgress.outputDir;
        }
        updateStatusBanner(data.lastProgress);
      }
    }
  } catch (e) {}
});

function renderMissions(missions) {
  const container = document.getElementById('missionsList');
  if (!missions || !missions.length) {
    container.innerHTML = 'No published NOAA LiDAR/DEM datasets found for this AOI.';
    return;
  }
  container.innerHTML = '';
  for (const m of missions) {
    const div = document.createElement('div');
    div.className = 'mission-card';
    div.innerHTML = `
      <div class="mission-title">[${m.dataType}] ${m.title}</div>
      <div class="mission-meta">
        Mission ID: <strong>${m.missionId}</strong> | Year: <strong>${m.year || 'N/A'}</strong> |
        VDatum: ${m.verticalDatum || 'NAVD88'}
      </div>
      <div class="mission-actions">
        ${m.bulkDownloadUrl ? `<button id="btn-s3-${m.missionId}" class="success" onclick="downloadS3('${m.missionId}', '${m.bulkDownloadUrl.replace(/'/g, "\\'")}', '${m.title.replace(/'/g, "\\'")}')">Instant Download Tiles (S3)</button>` : ''}
        <button id="btn-order-${m.missionId}" onclick="submitOrder('${m.missionId}', '${m.title.replace(/'/g, "\\'")}')">Submit NOAA Checkout Order (Email)</button>
        ${m.externalPortalUrl ? `<button class="secondary" onclick="window.open('${m.externalPortalUrl.replace(/'/g, "\\'")}', '_blank')">External Portal</button>` : ''}
      </div>
    `;
    container.appendChild(div);
  }
}

function requireOutputDir() {
  let out = document.getElementById('outputDir').value.trim();
  if (!out) {
    out = prompt('Please enter the destination folder path to save the downloaded LiDAR files:', 'C:\\\\temp\\\\phil');
    if (out) document.getElementById('outputDir').value = out.trim();
  }
  return out;
}

async function downloadS3(missionId, bulkUrl, title) {
  const outputDir = requireOutputDir();
  if (!outputDir) {
    alert('Please specify a destination folder to save the files.');
    return;
  }
  const email = document.getElementById('userEmail').value.trim();
  const maxTiles = parseInt(document.getElementById('maxTiles').value || '10', 10);
  updateStatusBanner({
    state: 'indexing',
    missionId,
    percent: 5,
    outputDir,
    message: `Intersecting AOI with S3 tile index for Mission ${missionId}...`
  });
  startProgressPolling();
  log(`Intersecting AOI with S3 tile index for Mission ${missionId} (${title})...`);
  const res = await fetch('/api/download-s3', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      aoi: currentAoiInfo,
      missionId,
      bulkDownloadUrl: bulkUrl,
      outputDir,
      email,
      maxTiles
    })
  });
  const data = await res.json();
  if (!res.ok || data.error) {
    updateStatusBanner({state: 'error', message: data.error || 'Download failed'});
    log('ERROR: ' + (data.error || 'Download failed'));
    alert(data.error || 'Download failed');
    return;
  }
  tilesLayer.clearLayers();
  for (const t of (data.intersectingTiles || [])) {
    if (t.geometry) {
      L.geoJSON(t.geometry, {
        style: {color: '#10b981', weight: 2, fillOpacity: 0.15}
      }).bindPopup(`<strong>${t.filename}</strong><br/><a href="${t.url}" target="_blank">Direct S3 Link</a>`).addTo(tilesLayer);
    }
  }
  updateStatusBanner({
    state: 'completed',
    missionId,
    outputDir: data.outputDir,
    downloadedFiles: data.downloadedFiles
  });
  const btn = document.getElementById(`btn-s3-${missionId}`);
  if (btn) {
    btn.textContent = `✅ Finished! (${data.downloadedFiles.length} Tiles Saved)`;
    btn.disabled = false;
  }
  log(`✅ DOWNLOAD FINISHED! Found ${data.totalIntersectingTiles} intersecting tiles; saved ${data.downloadedFiles.length} file(s) to ${data.outputDir}:`);
  for (const f of data.downloadedFiles) {
    log(`  -> ${f.path} (${f.size_mb} MB)`);
  }
}

async function submitOrder(missionId, title) {
  const outputDir = requireOutputDir();
  if (!outputDir) {
    alert('Please specify a destination folder to save the order files.');
    return;
  }
  const email = document.getElementById('userEmail').value.trim();
  updateStatusBanner({
    state: 'indexing',
    missionId,
    percent: 30,
    outputDir,
    message: `Submitting custom NOAA DAV order for Mission ${missionId} (${email})...`
  });
  log(`Submitting custom NOAA DAV order for Mission ${missionId} (${title}) with email ${email}...`);
  const res = await fetch('/api/submit-order', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      aoi: currentAoiInfo,
      missionId,
      outputDir,
      email
    })
  });
  const data = await res.json();
  if (!res.ok || data.error) {
    updateStatusBanner({state: 'error', message: data.error || 'Order submission failed'});
    log('ERROR: ' + (data.error || 'Order submission failed'));
    alert(data.error || 'Order submission failed');
    return;
  }
  updateStatusBanner({
    state: 'order_submitted',
    missionId,
    outputDir,
    requestId: data.order.requestId,
    userEmail: data.order.userEmail,
    statusUrl: data.order.statusUrl
  });
  log(`✅ SUCCESS! NOAA Request ID #${data.order.requestId} submitted for ${data.order.userEmail}.`);
  log(`Status URL: ${data.order.statusUrl}`);
  log(`Order receipt saved to: ${data.receiptPath}`);
}
</script>
</body>
</html>
"""


def run_web_server(port: int = 8765, default_output_dir: str = "", preload_geojson: str = "") -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    progress_state: Dict[str, Any] = {"state": "idle"}

    # Check if there is an existing completed manifest in default_output_dir
    if default_output_dir and Path(default_output_dir).exists():
        for mf in sorted(Path(default_output_dir).glob("manifest_mission_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                mdata = json.loads(mf.read_text(encoding="utf-8"))
                progress_state.update(
                    {
                        "state": "completed",
                        "missionId": str(mdata.get("missionId")),
                        "outputDir": str(mdata.get("outputDir") or default_output_dir),
                        "downloadedFiles": mdata.get("downloadedFiles") or [],
                        "percent": 100,
                    }
                )
                break
            except Exception:
                pass

    preloaded_payload: Optional[Dict[str, Any]] = None
    if preload_geojson and Path(preload_geojson).exists():
        try:
            aoi = parse_and_normalize_geojson(Path(preload_geojson))
            missions = search_missions(aoi["wkt_aoi"])
            preloaded_payload = {
                "filename": Path(preload_geojson).name,
                "aoi": {k: v for k, v in aoi.items() if k != "polygon_4326"},
                "missions": missions,
                "lastProgress": progress_state,
            }
        except Exception as exc:
            print(f"Warning: failed to preload {preload_geojson}: {exc}")

    class AgentHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, obj: Dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/index.html"):
                html_str = HTML_UI
                if default_output_dir:
                    escaped = default_output_dir.replace("\\", "\\\\")
                    html_str = html_str.replace(
                        'id="outputDir" placeholder="C:\\\\Users\\\\fqiao\\\\Downloads\\\\NOAA_Lidar"',
                        f'id="outputDir" value="{escaped}"',
                    )
                raw = html_str.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/health":
                self._send_json({"status": "ok", "email": normalize_email(DEFAULT_EMAIL)})
            elif self.path == "/api/preload":
                if preloaded_payload is not None:
                    preloaded_payload["lastProgress"] = progress_state
                self._send_json(preloaded_payload or {"lastProgress": progress_state})
            elif self.path == "/api/download-progress":
                self._send_json(progress_state)
            else:
                self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            try:
                if self.path == "/api/pick-folder":
                    chosen = pick_folder_dialog(body.get("initialDir"))
                    self._send_json({"folder": chosen})
                elif self.path == "/api/open-folder":
                    folder = body.get("folder")
                    if folder and Path(folder).exists():
                        os.startfile(str(Path(folder).resolve()))
                    self._send_json({"ok": True})
                elif self.path == "/api/upload-geojson":
                    aoi = parse_and_normalize_geojson(
                        body["content"], source_crs=body.get("sourceCrs")
                    )
                    missions = search_missions(aoi["wkt_aoi"])
                    aoi_serializable = {k: v for k, v in aoi.items() if k != "polygon_4326"}
                    self._send_json({"aoi": aoi_serializable, "missions": missions})
                elif self.path == "/api/download-s3":
                    aoi_dict = body["aoi"]
                    poly = shape(aoi_dict["noaa_restricted_geojson"]["features"][0]["geometry"])
                    out_dir = Path(body["outputDir"])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    mid = str(body["missionId"])
                    progress_state.clear()
                    progress_state.update(
                        {
                            "state": "indexing",
                            "missionId": mid,
                            "outputDir": str(out_dir.resolve()),
                            "percent": 5,
                            "message": f"Fetching S3 tile index for Mission {mid}...",
                        }
                    )
                    # Save NOAA-compatible GeoJSON alongside downloads
                    compat_path = out_dir / "aoi_noaa_compatible.geojson"
                    compat_path.write_text(
                        json.dumps(aoi_dict["noaa_restricted_geojson"], indent=2), encoding="utf-8"
                    )
                    tile_res = find_intersecting_s3_tiles(
                        body["bulkDownloadUrl"], mid, poly
                    )

                    def _on_prog(idx: int, total: int, fname: str, st: str) -> None:
                        pct = int(round(((idx - (0 if st == "completed" else 0.5)) / max(total, 1)) * 100))
                        progress_state.update(
                            {
                                "state": "downloading",
                                "missionId": mid,
                                "outputDir": str(out_dir.resolve()),
                                "currentTile": idx,
                                "totalTiles": total,
                                "percent": max(5, min(99, pct)),
                                "message": f"Tile {idx} of {total}: {fname}",
                            }
                        )

                    dl_files = download_s3_tiles(
                        tile_res["tiles"],
                        out_dir,
                        max_tiles=int(body.get("maxTiles") or 10),
                        progress_callback=_on_prog,
                    )
                    manifest = {
                        "email": normalize_email(body.get("email")),
                        "missionId": mid,
                        "outputDir": str(out_dir.resolve()),
                        "indexSource": tile_res["index_source"],
                        "totalIntersectingTiles": tile_res["tile_count"],
                        "downloadedFiles": dl_files,
                    }
                    (out_dir / f"manifest_mission_{mid}.json").write_text(
                        json.dumps(manifest, indent=2), encoding="utf-8"
                    )
                    progress_state.clear()
                    progress_state.update(
                        {
                            "state": "completed",
                            "missionId": mid,
                            "outputDir": str(out_dir.resolve()),
                            "downloadedFiles": dl_files,
                            "percent": 100,
                        }
                    )
                    self._send_json(
                        {
                            "outputDir": str(out_dir.resolve()),
                            "totalIntersectingTiles": tile_res["tile_count"],
                            "intersectingTiles": tile_res["tiles"],
                            "downloadedFiles": dl_files,
                        }
                    )
                elif self.path == "/api/submit-order":
                    aoi_dict = body["aoi"]
                    out_dir = Path(body["outputDir"])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    compat_path = out_dir / "aoi_noaa_compatible.geojson"
                    compat_path.write_text(
                        json.dumps(aoi_dict["noaa_restricted_geojson"], indent=2), encoding="utf-8"
                    )
                    order_res = submit_noaa_dav_order(
                        aoi_dict,
                        mission_id=body["missionId"],
                        email=body.get("email") or DEFAULT_EMAIL,
                    )
                    receipt_path = out_dir / f"noaa_dav_order_{order_res.get('requestId') or body['missionId']}.json"
                    receipt_path.write_text(json.dumps(order_res, indent=2), encoding="utf-8")
                    self._send_json({"order": order_res, "receiptPath": str(receipt_path.resolve())})
                else:
                    self._send_json({"error": f"Unknown endpoint {self.path}"}, status=404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)

    server = ThreadingHTTPServer(("127.0.0.1", port), AgentHandler)
    print(f"NOAA LiDAR Download Agent UI listening at http://127.0.0.1:{port}")
    sys.stdout.flush()
    server.serve_forever()


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NOAA Digital Coast LiDAR & DEM Download Agent (coast.noaa.gov/dataviewer)"
    )
    parser.add_argument("--serve", action="store_true", help="Launch interactive web UI")
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve web UI")
    parser.add_argument("--default-output-dir", default="", help="Pre-populated save folder for --serve UI")
    parser.add_argument("--preload-geojson", default="", help="Optional GeoJSON file to preload on --serve startup")

    subparsers = parser.add_subparsers(dest="command")

    # search
    p_search = subparsers.add_parser("search", help="Search NOAA LiDAR/DEM missions for a GeoJSON file")
    p_search.add_argument("--geojson", required=True, help="Path to input .geojson / .json file")
    p_search.add_argument("--source-crs", default=None, help="Optional source CRS override (e.g. EPSG:2881)")
    p_search.add_argument("--json", action="store_true", help="Output results as JSON")

    # download
    p_dl = subparsers.add_parser("download", help="Download NOAA LiDAR/DEM data for a GeoJSON file")
    p_dl.add_argument("--geojson", required=True, help="Path to input .geojson / .json file")
    p_dl.add_argument("--output-dir", default=None, help="Folder to save downloaded LiDAR/DEM files")
    p_dl.add_argument("--ask-folder", action="store_true", help="Open Windows folder picker if --output-dir is omitted")
    p_dl.add_argument("--email", default=DEFAULT_EMAIL, help="Email address for NOAA DAV (default: fqiao@hazenandsawyer)")
    p_dl.add_argument("--source-crs", default=None, help="Optional source CRS override (e.g. EPSG:2881)")
    p_dl.add_argument("--mission-id", default=None, help="Specific NOAA missionId to download (defaults to newest matching mission)")
    p_dl.add_argument("--data-type", choices=["Lidar", "DEM", "all"], default="DEM", help="Preferred data type when --mission-id is not specified")
    p_dl.add_argument("--mode", choices=["s3", "order", "both"], default="s3", help="s3 = instant S3 tile download, order = custom NOAA DAV checkout order, both = both")
    p_dl.add_argument("--max-tiles", type=int, default=25, help="Max tiles to download in s3 mode (0 = unlimited)")
    p_dl.add_argument("--wait", action="store_true", help="Wait/poll for custom order completion and download ZIP")

    # status
    p_st = subparsers.add_parser("status", help="Check status and download ZIP for a NOAA DAV requestId")
    p_st.add_argument("--request-id", required=True, help="NOAA DAV requestId")
    p_st.add_argument("--output-dir", required=True, help="Folder to save completed job ZIPs")
    p_st.add_argument("--download-when-ready", action="store_true", help="Poll until complete and download ZIP")

    args = parser.parse_args()

    if args.serve or args.command is None:
        if args.serve:
            run_web_server(
                port=args.port,
                default_output_dir=args.default_output_dir,
                preload_geojson=args.preload_geojson,
            )
            return
        parser.print_help()
        return

    if args.command == "search":
        aoi = parse_and_normalize_geojson(args.geojson, source_crs=args.source_crs)
        missions = search_missions(aoi["wkt_aoi"])
        if args.json:
            out = {
                "aoi": {k: v for k, v in aoi.items() if k != "polygon_4326"},
                "missions": missions,
            }
            print(json.dumps(out, indent=2))
        else:
            print(f"AOI CRS: {aoi['source_crs']} -> EPSG:4326")
            print(f"AOI Area: {aoi['area_sq_miles']} sq mi ({aoi['area_acres']} acres)")
            print(f"AOI BBox (Lon/Lat): {aoi['bbox_4326']}")
            print(f"Found {len(missions)} intersecting NOAA missions:\n")
            for m in missions:
                print(
                    f"  - ID {m['missionId']:>6} | {str(m['year']):<4} | {m['dataType']:<5} | {m['title']}"
                )
                if m.get("bulkDownloadUrl"):
                    print(f"             S3 Bulk Index: {m['bulkDownloadUrl']}")
        return

    if args.command == "download":
        output_dir = args.output_dir
        if not output_dir and args.ask_folder:
            output_dir = pick_folder_dialog()
        if not output_dir:
            raise SystemExit("ERROR: --output-dir is required (please ask the user for a folder to save the files).")

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        email_norm = normalize_email(args.email)

        aoi = parse_and_normalize_geojson(args.geojson, source_crs=args.source_crs)
        compat_file = out_path / "aoi_noaa_compatible.geojson"
        compat_file.write_text(json.dumps(aoi["noaa_restricted_geojson"], indent=2), encoding="utf-8")

        missions = search_missions(aoi["wkt_aoi"])
        if not missions:
            raise SystemExit("No published NOAA LiDAR or DEM missions intersect this GeoJSON AOI.")

        selected_mission = None
        if args.mission_id:
            for m in missions:
                if str(m["missionId"]) == str(args.mission_id):
                    selected_mission = m
                    break
            if not selected_mission:
                selected_mission = {"missionId": str(args.mission_id), "title": f"Mission {args.mission_id}", "dataType": args.data_type, "bulkDownloadUrl": None}
        else:
            for m in missions:
                if args.data_type == "all" or m["dataType"].lower() == args.data_type.lower():
                    if args.mode in ("s3", "both") and not m.get("bulkDownloadUrl"):
                        continue
                    selected_mission = m
                    break
            if not selected_mission:
                selected_mission = missions[0]

        summary: Dict[str, Any] = {
            "email": email_norm,
            "outputDir": str(out_path.resolve()),
            "noaaCompatibleGeojson": str(compat_file.resolve()),
            "selectedMission": selected_mission,
        }

        if args.mode in ("s3", "both"):
            bulk_url = selected_mission.get("bulkDownloadUrl")
            if not bulk_url:
                raise SystemExit(
                    f"Mission {selected_mission['missionId']} does not list an S3 bulk download index; use --mode order."
                )
            print(
                f"Finding S3 tiles intersecting AOI for Mission {selected_mission['missionId']} ({selected_mission['title']})..."
            )
            tile_res = find_intersecting_s3_tiles(
                bulk_url, str(selected_mission["missionId"]), aoi["polygon_4326"]
            )
            print(f"Found {tile_res['tile_count']} intersecting tile(s) from {tile_res['index_source']}.")
            dl_files = download_s3_tiles(
                tile_res["tiles"],
                out_path,
                max_tiles=args.max_tiles,
                progress_callback=lambda i, n, fn, st: print(f"  [{i}/{n}] {st}: {fn}"),
            )
            summary["s3Download"] = {
                "indexSource": tile_res["index_source"],
                "totalIntersectingTiles": tile_res["tile_count"],
                "downloadedFiles": dl_files,
            }

        if args.mode in ("order", "both"):
            print(
                f"Submitting NOAA DAV custom checkout order for Mission {selected_mission['missionId']} using email {email_norm}..."
            )
            order_res = submit_noaa_dav_order(
                aoi,
                mission_id=selected_mission["missionId"],
                email=email_norm,
            )
            receipt_file = out_path / f"noaa_dav_order_{order_res.get('requestId') or selected_mission['missionId']}.json"
            receipt_file.write_text(json.dumps(order_res, indent=2), encoding="utf-8")
            summary["davOrder"] = order_res
            if args.wait and order_res.get("requestId"):
                print(f"Polling NOAA DAV Request #{order_res['requestId']} until completion...")
                poll_res = check_and_download_dav_request(
                    order_res["requestId"], out_path, wait=True
                )
                summary["davOrderPoll"] = poll_res

        manifest_file = out_path / "download_manifest.json"
        manifest_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nSaved download manifest to: {manifest_file.resolve()}")
        print(json.dumps(summary, indent=2))
        return

    if args.command == "status":
        res = check_and_download_dav_request(
            args.request_id, args.output_dir, wait=args.download_when_ready
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
