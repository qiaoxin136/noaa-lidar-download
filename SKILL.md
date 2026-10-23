---
name: noaa-lidar-download
description: Upload or read a GeoJSON area-of-interest (AOI) file, search NOAA Digital Coast Data Access Viewer (https://coast.noaa.gov/dataviewer/#/lidar/search) for intersecting LiDAR point cloud (.laz/.las) and DEM (.tif) datasets, ask the user for a destination folder to save the files, and download LiDAR data using the user's email address (defaulting to fqiao@hazenandsawyer.com / fqiao@hazenandsawyer). Supports both instant direct S3 tile downloads (via STAC / shapefile tile index intersection) and custom NOAA DAV checkout orders with automatic polling and zip download, plus an interactive web UI. Triggers on requests to download NOAA LiDAR or DEM data for a GeoJSON file, search coast.noaa.gov/dataviewer/#/lidar/search, or launch the NOAA LiDAR downloader agent.
---

# NOAA LiDAR Download Agent (`noaa-lidar-download`)

This skill allows the user to provide or upload any GeoJSON file, choose a destination folder to save downloaded files, and download LiDAR Point Cloud (`.laz`/`.las`) or DEM (`.tif`) data from the **NOAA Digital Coast Data Access Viewer** (`https://coast.noaa.gov/dataviewer/#/lidar/search`) using the default email address `fqiao@hazenandsawyer` (normalized to `fqiao@hazenandsawyer.com` for NOAA's email validator).

## Required Workflow

Whenever a user invokes this skill or asks to download LiDAR data from NOAA for a GeoJSON file:

1. **Get the GeoJSON File & Save Folder**:
   - If the user has not yet provided a GeoJSON file path or destination folder to save the downloaded files, **always ask the user** for the destination folder (and GeoJSON path if needed) using the `ask_user` tool, or launch the interactive web UI (`scripts/noaa_lidar_agent.py --serve`) in the Browser Canvas where they can upload the `.geojson` file and browse/select the save folder directly.
2. **Use the Default Email Address**:
   - Always default the download email address to `fqiao@hazenandsawyer` (which `noaa_lidar_agent.py` automatically normalizes to `fqiao@hazenandsawyer.com` so NOAA's server accepts the request and sends confirmation/completion emails to Hazen and Sawyer).
3. **Normalize Any GeoJSON Input**:
   - Standard project GeoJSON files (such as multi-feature basin boundaries, pipe networks, or State Plane `EPSG:2881` / `EPSG:2236` coordinates) are rejected by NOAA's web UI (`Restricted GeoJSON` schema requires a 1-feature `FeatureCollection` in `EPSG:4326` with `<= 350` vertices and no extra properties).
   - `scripts/noaa_lidar_agent.py` automatically detects the CRS (including `EPSG:4326` and Florida State Plane `EPSG:2881`), reprojects to WGS84/NAD83 (`SRID=4269`), dissolves/hulls multiple features or lines/points into a single valid polygon, simplifies vertices to `<= 350`, and exports `aoi_noaa_compatible.geojson` to the destination folder.
4. **Search & Download LiDAR Data**:
   - Use `scripts/noaa_lidar_agent.py` to query `POST https://coast.noaa.gov/dataviewer/api/v1/missions` for all LiDAR and DEM missions intersecting the GeoJSON AOI.
   - Support two download modes into the user's chosen destination folder:
     - **Direct S3 Tile Download (`--mode s3` or `--mode both`)**: Spatially intersects the GeoJSON AOI with the mission's S3 STAC collection (`stac/noaa_item_collection_m<id>.json`) or shapefile tile index (`tileindex_*.zip`) on `noaa-nos-coastal-lidar-pds.s3.amazonaws.com` and immediately downloads the intersecting `.laz` or `.tif` files into the destination folder.
     - **NOAA DAV Custom Checkout Order (`--mode order` or `--mode both`)**: Submits a custom checkout job to `POST https://coast.noaa.gov/dataviewer/api/v1/requests/request/submit` with `userEmail: "fqiao@hazenandsawyer.com"` and `organization: "Hazen and Sawyer"`, and optionally polls `GET /dataviewer/api/v1/jobs/request?requestId=<id>` and downloads the finished `.zip` via `GET /dataviewer/api/v1/jobs/job/download?jobId=<jobId>&sendLink`.

## CLI Usage (`scripts/noaa_lidar_agent.py`)

### 1. Launch Interactive Web UI (Upload GeoJSON + Pick Save Folder + Map + Download)
```powershell
python "C:\Users\fqiao\.agents\skills\noaa-lidar-download\scripts\noaa_lidar_agent.py" --serve --port 8765
```
Then open `http://127.0.0.1:8765` in the Browser Canvas (`open_canvas` with `canvasId: "browser"`).

### 2. Search Available NOAA LiDAR & DEM Missions for a GeoJSON File
```powershell
python "C:\Users\fqiao\.agents\skills\noaa-lidar-download\scripts\noaa_lidar_agent.py" search --geojson "path\to\aoi.geojson"
```

### 3. Download Intersecting LiDAR / DEM Data to User's Save Folder
```powershell
# Instant direct S3 tile download + custom NOAA DAV order with fqiao@hazenandsawyer.com:
python "C:\Users\fqiao\.agents\skills\noaa-lidar-download\scripts\noaa_lidar_agent.py" download `
  --geojson "path\to\aoi.geojson" `
  --output-dir "C:\path\to\save\folder" `
  --email "fqiao@hazenandsawyer" `
  --data-type DEM `
  --mode s3
```
- `--mode s3`: Immediately downloads intersecting `.tif` (DEM) or `.copc.laz` (LiDAR point cloud) tiles from NOAA's S3 bucket (`noaa-nos-coastal-lidar-pds`) into `--output-dir`.
- `--mode order`: Submits a custom processing order to `https://coast.noaa.gov/dataviewer/#/lidar/search` (`requests/request/submit`) using `fqiao@hazenandsawyer.com`, saves the order metadata/tracking link to `--output-dir`, and (when `--wait` is passed) polls until completion and downloads the processed `.zip`.
- `--mode both`: Performs both the instant S3 tile download and submits the custom NOAA DAV checkout order.

### 4. Check Status / Download a Previously Submitted NOAA DAV Request ID
```powershell
python "C:\Users\fqiao\.agents\skills\noaa-lidar-download\scripts\noaa_lidar_agent.py" status `
  --request-id 123456 `
  --output-dir "C:\path\to\save\folder" `
  --download-when-ready
```

## Reference Documentation

See `references/noaa_dav_api.md` for full details on the NOAA Data Access Viewer REST API endpoints, payload schemas, projection zone IDs, and S3 bucket layout.
