# NOAA Digital Coast Data Access Viewer (DAV) API & S3 Reference

This reference documents the reverse-engineered backend API for `https://coast.noaa.gov/dataviewer/#/lidar/search` (`https://coast.noaa.gov/dataviewer/api/v1`) and the NOAA Coastal LiDAR AWS S3 repository (`https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com`).

## 1. Base URL & Endpoints

- **Base API URL**: `https://coast.noaa.gov/dataviewer/api/v1`
- **Search Missions by AOI**: `POST /missions`
- **Mission Details by ID**: `GET /missions/mission?missionId=<id>`
- **Check Tidal Area**: `POST /search/isTidalArea` (`{"aoi": wkt, "missionId": "<id>"}`)
- **Projection Zones**: `POST /search/projectionZones` (`{"centroid": centroid, "aoi": wkt}`)
- **Submit Custom Checkout Order**: `POST /requests/request/submit`
- **Poll Request Status**: `GET /jobs/request?requestId=<requestId>`
- **Poll Single Job Status**: `GET /jobs/job/?jobId=<jobId>`
- **Job Progress Stages**: `GET /jobs/job/progress?jobId=<jobId>`
- **Get Job Download Link**: `GET /jobs/job/download?jobId=<jobId>&sendLink`

## 2. GeoJSON Restrictions & AOI WKT Format

### Why Standard GeoJSON Files Often Fail in NOAA's Web UI
NOAA's frontend (`Restricted GeoJSON` AJV schema) enforces strict rules when uploading a GeoJSON file manually in the browser:
1. Must be `type: "FeatureCollection"` with **exactly 1 feature** (`minItems: 1, maxItems: 1`).
2. `additionalProperties: false` at both `FeatureCollection` and `Feature` levels (meaning top-level keys like `crs`, `name`, or `bbox` fail validation).
3. `Feature.properties` may only contain `FID`, `Shape`, or `Id` (any other property names fail validation).
4. Geometry must be `Polygon` or closed `LineString` in `EPSG:4326` / `NAD83` (`SRID=4269`), with `<= 350` vertices after simplification and area `<= 1,500` sq miles.

`scripts/noaa_lidar_agent.py` automatically normalizes **any** user GeoJSON (multi-feature polygons, pipe networks, manholes/points, State Plane `EPSG:2881` / `EPSG:2236`, etc.) into:
- An API-ready `SRID=4269;POLYGON((lon1 lat1, lon2 lat2, ..., lon1 lat1))` WKT string.
- A strictly compliant `aoi_noaa_compatible.geojson` saved to the output folder so the user can also upload it manually to `https://coast.noaa.gov/dataviewer/#/lidar/search` if desired.

## 3. Searching Missions (`POST /missions`)

```json
{
  "aoi": "SRID=4269;POLYGON((-80.125 26.010, -80.115 26.010, -80.115 26.020, -80.125 26.020, -80.125 26.010))",
  "dataTypes": ["Lidar", "DEM"],
  "published": "true",
  "page": 0,
  "pageSize": 25,
  "sort": "desc",
  "sortField": "year"
}
```

To fetch detailed mission metadata (including `centroid`, `sizeEstimate`, `groundClasses`, `classTable`) for a single mission:
```json
{
  "aoi": "SRID=4269;POLYGON((...))",
  "published": "true",
  "missionIds": [9052],
  "sort": "asc",
  "pageSize": 10,
  "page": 0,
  "getGeoJson": "true"
}
```
*(Note: Omit `dataTypes` when querying with `missionIds`.)*

## 4. Submitting a Custom Checkout Request (`POST /requests/request/submit`)

Email validation on NOAA's backend requires a full domain with TLD (`/^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/`). The agent normalizes `fqiao@hazenandsawyer` to `fqiao@hazenandsawyer.com`.

### Example Payload (LiDAR Point Cloud / Raster Grid)
```json
{
  "jobs": {
    "tier": "production",
    "userEmail": "fqiao@hazenandsawyer.com",
    "organization": "Hazen and Sawyer",
    "surveyOptIn": "false",
    "digest": "false",
    "job": [
      {
        "label": "2018 USGS/NRCS Lidar: Southeast Florida",
        "missionId": "9052",
        "minX": -80.125,
        "minY": 26.01,
        "maxX": -80.115,
        "maxY": 26.02,
        "mercMinX": -8919476.33,
        "mercMinY": 2999950.25,
        "mercMaxX": -8918363.14,
        "mercMaxY": 3001188.62,
        "horizontalDatumId": "2",
        "horizontalUnitsId": "3",
        "verticalDatumId": "3",
        "verticalUnitsId": "3",
        "projectionId": "2",
        "zonesId": "255",
        "dataType": "Lidar",
        "dataTypeId": 5,
        "aoi": "SRID=4269;POLYGON((...))",
        "vDatumLabel": "NAVD88",
        "addAncillaryData": "0",
        "dataReturnsSelection": "0",
        "lidarDataClasses": "2",
        "geoidModel": "994d177c-5f55-4cf2-bb81-1c40aa66a578",
        "products": {
          "point": {
            "fileFormatId": "13"
          },
          "grid": {
            "fileFormatId": "8",
            "binMethodId": "2",
            "binUnitsId": "3",
            "binSize": "3",
            "fillGaps": "true"
          }
        }
      }
    ]
  }
}
```

### Example Payload (DEM Dataset, `dataTypeId: 2`)
For `DEM` datasets (`dataType: "DEM"`, `dataTypeId: 2`), omit `products`, `lidarDataClasses`, `dataReturnsSelection`, and `addAncillaryData`, and set `"fileFormatId": "1"` (GeoTIFF) at the top level of the job object.

## 5. Direct Tile Download via NOAA AWS S3 (`noaa-nos-coastal-lidar-pds`)

Every mission returned by `POST /missions` includes a `Bulk Download` link (`linkTypeId == "46"`), e.g.:
- Point Cloud (`.copc.laz` / `.laz`): `https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/laz/geoid18/<missionId>/index.html`
- DEM (`.tif`): `https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/<folder>_<missionId>/index.html`

Each S3 `index.html` links to:
1. `stac/noaa_item_collection_m<missionId>.json` (GeoJSON FeatureCollection of tiles with `geometry` in WGS84 and `assets.<id>.href` pointing to the `.tif` / `.laz` file on S3).
2. `tileindex_*.zip` (Shapefile index containing `geometry` and `url` / `location` column pointing to each tile's direct S3 URL).

By intersecting the user's AOI polygon with the STAC item collection or `tileindex_*.zip` shapefile, `noaa_lidar_agent.py` can download the exact intersecting `.laz` or `.tif` files immediately to the user's folder without waiting in NOAA's custom processing queue.
