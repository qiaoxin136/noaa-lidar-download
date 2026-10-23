# NOAA Digital Coast LiDAR & DEM Download Agent

Web application and Python CLI/Desktop agent for uploading any `.geojson` file (polygons, water pipe networks, demand nodes, WGS84 or State Plane projections) and downloading intersecting LiDAR Point Cloud (`.laz`) and DEM (`.tif`) datasets from the [NOAA Digital Coast Data Access Viewer](https://coast.noaa.gov/dataviewer/#/lidar/search).

- **Default Email**: `fqiao@hazenandsawyer.com` (automatically normalized from `fqiao@hazenandsawyer`)
- **Two Download Modes**:
  1. **Instant Direct S3 Tile Download**: Intersects the GeoJSON AOI against NOAA's `noaa-nos-coastal-lidar-pds` S3 tile index (`tileindex_*.zip` / STAC JSON) and streams intersecting `.tif` / `.copc.laz` tiles directly into your chosen local folder with a real-time progress bar.
  2. **NOAA DAV Custom Checkout Order**: Submits a custom processing request (`POST /api/v1/requests/request/submit`) to NOAA using `fqiao@hazenandsawyer.com`.

## Deploying to AWS Amplify

This repository is pre-configured with `amplify.yml` and an AWS Amplify Gen 2 backend proxy (`amplify/backend.ts`) so it can be deployed directly from GitHub:

1. Open the **AWS Amplify Console** and click **Create new app** -> **GitHub**.
2. Select repository **`fqiaohazen/noaa-lidar-download`** and branch **`main`**.
3. Amplify automatically detects `amplify.yml`, deploys the lightweight NOAA API CORS proxy (`amplify/backend.ts`), builds the Vite TypeScript app (`dist/`), and publishes your site.
4. *(Optional for static-only hosting without an Amplify backend role)*: In Amplify Console -> **Hosting** -> **Rewrites and redirects**, add a rewrite rule:
   - Source address: `/noaa-api/<*>`
   - Target address: `https://coast.noaa.gov/dataviewer/api/v1/<*>`
   - Type: `200 (Rewrite)`

## Running Locally

### Option A: Vite Web App
```bash
npm install
npm run dev
```

### Option B: Python Desktop Web UI & CLI Agent
```bash
python scripts/noaa_lidar_agent.py --serve --port 8765
```
