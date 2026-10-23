import './style.css'
import L from 'leaflet'
import proj4 from 'proj4'
import shp from 'shpjs'

// Register common projections used in municipal water/sewer GIS files
proj4.defs('EPSG:4326', '+proj=longlat +datum=WGS84 +no_defs')
proj4.defs('EPSG:4269', '+proj=longlat +datum=NAD83 +no_defs')
proj4.defs('EPSG:6318', '+proj=longlat +ellps=GRS80 +no_defs')
proj4.defs('EPSG:3857', '+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 +x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs')
// Florida East State Plane NAD83 HARN (US Survey Feet)
proj4.defs(
  'EPSG:2881',
  '+proj=tmerc +lat_0=24.3333333333333 +lon_0=-81 +k=0.999941177 +x_0=200000.0001016 +y_0=0 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=us-ft +no_defs'
)
// North Carolina State Plane NAD83 (US Survey Feet)
proj4.defs(
  'EPSG:2264',
  '+proj=lcc +lat_1=36.16666666666666 +lat_2=34.33333333333334 +lat_0=33.75 +lon_0=-79 +x_0=609601.2192024384 +y_0=0 +datum=NAD83 +units=us-ft +no_defs'
)

const DEFAULT_EMAIL = 'fqiao@hazenandsawyer.com'
const DEFAULT_ORG = 'Hazen and Sawyer'
const GEOID18_UUID = '994d177c-5f55-4cf2-bb81-1c40aa66a578'

interface AoiInfo {
  source_crs: string
  feature_count: number
  original_vertex_count: number
  simplified_vertex_count: number
  area_sq_miles: number
  area_acres: number
  bbox_4326: [number, number, number, number]
  bbox_3857: [number, number, number, number]
  wkt_aoi: string
  ring_4326: [number, number][]
  noaa_restricted_geojson: GeoJSON.FeatureCollection
}

interface MissionItem {
  missionId: string
  title: string
  year: string | number
  dataType: string
  verticalDatum?: string
  bulkDownloadUrl?: string | null
  externalPortalUrl?: string | null
  metadataUrl?: string | null
}

interface TileItem {
  filename: string
  url: string
  bbox: [number, number, number, number]
  geometry: GeoJSON.Geometry
}

const DEFAULT_LAMBDA_PROXY = 'https://4nefpv7vg74hxvwgeym6xafssy0whqxq.lambda-url.us-east-1.on.aws'
let noaaApiBase =
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? '/noaa-api'
    : DEFAULT_LAMBDA_PROXY
let currentAoiInfo: AoiInfo | null = null
let selectedDirHandle: FileSystemDirectoryHandle | null = null

const map = L.map('map').setView([35.48, -79.18], 10)
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors',
}).addTo(map)

let aoiLayer: L.GeoJSON | null = null
const tilesLayer = L.layerGroup().addTo(map)

function log(msg: string): void {
  const box = document.getElementById('logBox')
  if (!box) return
  const ts = new Date().toLocaleTimeString()
  box.textContent += `\n[${ts}] ${msg}`
  box.scrollTop = box.scrollHeight
}

function normalizeEmail(raw?: string): string {
  const s = (raw || DEFAULT_EMAIL).trim()
  if (!s.includes('@')) return `${s}@hazenandsawyer.com`
  const [local, domain] = s.split('@')
  if (!domain.includes('.')) return `${local}@${domain}.com`
  return s
}

function setBanner(
  state: 'idle' | 'running' | 'completed' | 'error',
  htmlText: string,
  percent?: number
): void {
  const banner = document.getElementById('statusBanner')!
  const textEl = document.getElementById('statusText')!
  const pWrap = document.getElementById('progressWrap')!
  const pBar = document.getElementById('progressBar')!
  if (state === 'idle') {
    banner.className = 'status-banner'
    return
  }
  banner.className = `status-banner ${state}`
  textEl.innerHTML = htmlText
  if (typeof percent === 'number') {
    pWrap.style.display = 'block'
    pBar.style.width = `${Math.max(2, Math.min(100, percent))}%`
  } else {
    pWrap.style.display = 'none'
  }
}

async function discoverApiBase(): Promise<void> {
  try {
    const res = await fetch('/amplify_outputs.json', { cache: 'no-store' })
    if (res.ok) {
      const cfg = await res.json()
      const customApi = cfg?.custom?.noaa_api
      if (customApi && typeof customApi === 'string') {
        noaaApiBase = `${customApi.replace(/\/$/, '')}/noaa`
        log(`Connected to AWS Amplify NOAA API Gateway: ${noaaApiBase}`)
        return
      }
    }
  } catch {
    // Fallback to /noaa-api rewrite or local proxy
  }
}

async function noaaFetchJson(path: string, method: 'GET' | 'POST' = 'GET', payload?: unknown): Promise<any> {
  const cleanPath = path.replace(/^\//, '')
  const init: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: method === 'POST' && payload !== undefined ? JSON.stringify(payload) : undefined,
  }
  const bases = Array.from(new Set([noaaApiBase, DEFAULT_LAMBDA_PROXY, '/noaa-api']))
  let lastErr: Error | null = null
  for (const base of bases) {
    try {
      const res = await fetch(`${base}/${cleanPath}`, init)
      if (!res.ok) {
        const txt = await res.text()
        throw new Error(`NOAA API HTTP ${res.status}: ${txt.slice(0, 300)}`)
      }
      return await res.json()
    } catch (err) {
      lastErr = err instanceof Error ? err : new Error(String(err))
    }
  }
  throw lastErr || new Error('Failed to reach NOAA API proxy')
}

// ---------------------------------------------------------------------------
// 2D/3D/4D + Null Coordinate Sanitizer, CRS Reprojection & Convex Hull
// ---------------------------------------------------------------------------

function isValidNum(v: unknown): boolean {
  if (v === null || v === undefined || typeof v === 'boolean') return false
  const n = Number(v)
  return Number.isFinite(n)
}

function collect2dPoints(coords: any, out: [number, number][]): void {
  if (!Array.isArray(coords) || coords.length === 0) return
  if (!Array.isArray(coords[0])) {
    if (coords.length >= 2 && isValidNum(coords[0]) && isValidNum(coords[1])) {
      out.push([Number(coords[0]), Number(coords[1])])
    }
    return
  }
  for (const item of coords) {
    collect2dPoints(item, out)
  }
}

function detectSourceCrs(data: any, sample: [number, number], overrideCrs?: string): string {
  if (overrideCrs && overrideCrs.trim()) {
    const m = overrideCrs.toUpperCase().match(/(\d{4,6})/)
    if (m) return `EPSG:${m[1]}`
  }
  const crsObj = data?.crs
  if (crsObj && typeof crsObj === 'object') {
    const name = String(crsObj?.properties?.name || '')
    const m = name.match(/(?:EPSG[:/]+|CRS84)(\d{4,6})?/i)
    if (name.toUpperCase().includes('CRS84')) return 'EPSG:4326'
    if (m && m[1]) return `EPSG:${m[1]}`
  }
  const [x, y] = sample
  if (x >= -180 && x <= 180 && y >= -90 && y <= 90) return 'EPSG:4326'
  if (x >= 500_000 && x <= 1_100_000 && y >= 200_000 && y <= 2_000_000) return 'EPSG:2881'
  return 'EPSG:4326'
}

function reprojectPoint(pt: [number, number], fromCrs: string, toCrs: string): [number, number] {
  if (fromCrs === toCrs || (fromCrs === 'EPSG:6318' && toCrs === 'EPSG:4326') || (fromCrs === 'EPSG:4269' && toCrs === 'EPSG:4326')) {
    return [pt[0], pt[1]]
  }
  try {
    const res = proj4(fromCrs, toCrs, [pt[0], pt[1]])
    return [res[0], res[1]]
  } catch {
    return [pt[0], pt[1]]
  }
}

// Monotone chain 2D convex hull (runs in <15ms on 100,000+ points)
function computeConvexHull(points: [number, number][]): [number, number][] {
  if (points.length <= 3) return points.slice()
  const sorted = points
    .slice()
    .sort((a, b) => (a[0] === b[0] ? a[1] - b[1] : a[0] - b[0]))

  const cross = (o: [number, number], a: [number, number], b: [number, number]) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

  const lower: [number, number][] = []
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {
      lower.pop()
    }
    lower.push(p)
  }

  const upper: [number, number][] = []
  for (let i = sorted.length - 1; i >= 0; i--) {
    const p = sorted[i]
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {
      upper.pop()
    }
    upper.push(p)
  }

  upper.pop()
  lower.pop()
  return lower.concat(upper)
}

function simplifyRing(ring: [number, number][], maxVertices = 340): [number, number][] {
  if (ring.length <= maxVertices) return ring
  const step = Math.ceil(ring.length / maxVertices)
  const out: [number, number][] = []
  for (let i = 0; i < ring.length; i += step) {
    out.push(ring[i])
  }
  return out
}

export function parseAndNormalizeGeojson(rawText: string, overrideCrs?: string): AoiInfo {
  const data = JSON.parse(rawText)
  const rawGeoms: any[] = []
  if (data.type === 'FeatureCollection') {
    for (const f of data.features || []) {
      if (f && f.geometry) rawGeoms.push(f.geometry)
    }
  } else if (data.type === 'Feature' && data.geometry) {
    rawGeoms.push(data.geometry)
  } else if (data.coordinates || data.geometries) {
    rawGeoms.push(data)
  } else {
    throw new Error(`Unsupported GeoJSON type: ${data.type}`)
  }

  if (rawGeoms.length === 0) {
    throw new Error('GeoJSON contains no valid geometries.')
  }

  const allPts: [number, number][] = []
  for (const g of rawGeoms) {
    if (g.type === 'GeometryCollection') {
      for (const sub of g.geometries || []) collect2dPoints(sub.coordinates, allPts)
    } else {
      collect2dPoints(g.coordinates, allPts)
    }
  }
  if (allPts.length === 0) {
    throw new Error('Could not extract any valid 2D coordinates from GeoJSON.')
  }

  const sourceCrs = detectSourceCrs(data, allPts[0], overrideCrs)
  const pts4326 = allPts.map((pt) => reprojectPoint(pt, sourceCrs, 'EPSG:4326'))

  let ring4326: [number, number][] = []
  if (rawGeoms.length === 1 && rawGeoms[0].type === 'Polygon' && Array.isArray(rawGeoms[0].coordinates?.[0])) {
    const extPts: [number, number][] = []
    collect2dPoints(rawGeoms[0].coordinates[0], extPts)
    ring4326 = extPts.map((pt) => reprojectPoint(pt, sourceCrs, 'EPSG:4326'))
  } else {
    ring4326 = computeConvexHull(pts4326)
  }

  if (ring4326.length < 3) {
    const [cx, cy] = pts4326[0]
    const d = 0.001
    ring4326 = [
      [cx - d, cy - d],
      [cx + d, cy - d],
      [cx + d, cy + d],
      [cx - d, cy + d],
    ]
  }

  const originalVertexCount = ring4326.length
  ring4326 = simplifyRing(ring4326, 340)
  const coords2d: [number, number][] = ring4326.map(([x, y]) => [Number(x.toFixed(7)), Number(y.toFixed(7))])
  if (
    coords2d[0][0] !== coords2d[coords2d.length - 1][0] ||
    coords2d[0][1] !== coords2d[coords2d.length - 1][1]
  ) {
    coords2d.push([coords2d[0][0], coords2d[0][1]])
  }

  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity
  for (const [x, y] of coords2d) {
    if (x < minX) minX = x
    if (y < minY) minY = y
    if (x > maxX) maxX = x
    if (y > maxY) maxY = y
  }

  const [mMinX, mMinY] = reprojectPoint([minX, minY], 'EPSG:4326', 'EPSG:3857')
  const [mMaxX, mMaxY] = reprojectPoint([maxX, maxY], 'EPSG:4326', 'EPSG:3857')

  // Shoelace area in Web Mercator scaled by cos^2(lat)
  const ring3857 = coords2d.map((pt) => reprojectPoint(pt, 'EPSG:4326', 'EPSG:3857'))
  let areaMerc = 0
  for (let i = 0; i < ring3857.length - 1; i++) {
    areaMerc += ring3857[i][0] * ring3857[i + 1][1] - ring3857[i + 1][0] * ring3857[i][1]
  }
  areaMerc = Math.abs(areaMerc) / 2.0
  const midLatRad = (((minY + maxY) / 2.0) * Math.PI) / 180.0
  const areaSqMeters = areaMerc * Math.pow(Math.cos(midLatRad), 2)
  const areaSqMiles = areaSqMeters / 2_589_988.110336

  const wktPairs = coords2d.map((pt) => `${pt[0]} ${pt[1]}`).join(', ')
  const wktAoi = `SRID=4269;POLYGON((${wktPairs}))`

  const noaaRestrictedGeojson: GeoJSON.FeatureCollection = {
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        id: 1,
        properties: { FID: 1, Id: 0 },
        geometry: {
          type: 'Polygon',
          coordinates: [coords2d],
        },
      },
    ],
  }

  return {
    source_crs: sourceCrs,
    feature_count: rawGeoms.length,
    original_vertex_count: originalVertexCount,
    simplified_vertex_count: coords2d.length,
    area_sq_miles: Number(areaSqMiles.toFixed(4)),
    area_acres: Number((areaSqMiles * 640).toFixed(2)),
    bbox_4326: [Number(minX.toFixed(7)), Number(minY.toFixed(7)), Number(maxX.toFixed(7)), Number(maxY.toFixed(7))],
    bbox_3857: [Number(mMinX.toFixed(3)), Number(mMinY.toFixed(3)), Number(mMaxX.toFixed(3)), Number(mMaxY.toFixed(3))],
    wkt_aoi: wktAoi,
    ring_4326: coords2d,
    noaa_restricted_geojson: noaaRestrictedGeojson,
  }
}

// ---------------------------------------------------------------------------
// Polygon / BBox Intersection & S3 Tile Index Discovery
// ---------------------------------------------------------------------------

function bboxesIntersect(a: [number, number, number, number], b: [number, number, number, number]): boolean {
  return !(b[0] > a[2] || b[2] < a[0] || b[1] > a[3] || b[3] < a[1])
}

function pointInPolygon(pt: [number, number], ring: [number, number][]): boolean {
  let inside = false
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0],
      yi = ring[i][1]
    const xj = ring[j][0],
      yj = ring[j][1]
    const intersect = yi > pt[1] !== yj > pt[1] && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi + 1e-15) + xi
    if (intersect) inside = !inside
  }
  return inside
}

function tileIntersectsAoi(tileRing: [number, number][], tileBbox: [number, number, number, number], aoi: AoiInfo): boolean {
  if (!bboxesIntersect(tileBbox, aoi.bbox_4326)) return false
  for (const pt of tileRing) {
    if (pointInPolygon(pt, aoi.ring_4326)) return true
  }
  for (const pt of aoi.ring_4326) {
    if (pointInPolygon(pt, tileRing)) return true
  }
  return true
}

async function findIntersectingS3TilesInBrowser(bulkUrl: string, aoi: AoiInfo): Promise<TileItem[]> {
  const idxRes = await fetch(bulkUrl)
  if (!idxRes.ok) throw new Error(`Failed to fetch S3 index page: ${bulkUrl}`)
  const htmlText = await idxRes.text()

  const doc = new DOMParser().parseFromString(htmlText, 'text/html')
  const anchors = Array.from(doc.querySelectorAll('a'))
    .map((a) => a.getAttribute('href') || '')
    .filter(Boolean)

  let tileindexZipUrl: string | null = null
  let stacUrl: string | null = null
  for (const href of anchors) {
    const full = new URL(href, bulkUrl).toString()
    const lower = href.toLowerCase()
    if (lower.includes('tileindex') && lower.endsWith('.zip')) tileindexZipUrl = full
    else if (lower.includes('noaa_item_collection') && lower.endsWith('.json')) stacUrl = full
  }

  const tiles: TileItem[] = []
  if (tileindexZipUrl) {
    log(`Downloading S3 tile index shapefile: ${tileindexZipUrl}...`)
    const zipBuf = await (await fetch(tileindexZipUrl)).arrayBuffer()
    const geojson: any = await shp(zipBuf)
    const fc = Array.isArray(geojson) ? geojson[0] : geojson
    for (const feat of fc.features || []) {
      const pts: [number, number][] = []
      collect2dPoints(feat.geometry?.coordinates, pts)
      if (!pts.length) continue
      let minX = Infinity,
        minY = Infinity,
        maxX = -Infinity,
        maxY = -Infinity
      for (const [x, y] of pts) {
        if (x < minX) minX = x
        if (y < minY) minY = y
        if (x > maxX) maxX = x
        if (y > maxY) maxY = y
      }
      const bbox: [number, number, number, number] = [minX, minY, maxX, maxY]
      if (!tileIntersectsAoi(pts, bbox, aoi)) continue
      const props = feat.properties || {}
      const urlVal = String(props.url || props.URL || props.Url || props.location || props.LOCATION || '').trim()
      let fname = String(props.filename || props.FILENAME || props.name || '').trim()
      if (!fname && urlVal) {
        fname = urlVal.split('/').pop() || 'tile.tif'
      }
      if (urlVal.startsWith('http')) {
        tiles.push({
          filename: fname,
          url: urlVal,
          bbox,
          geometry: feat.geometry,
        })
      }
    }
  } else if (stacUrl) {
    log(`Downloading STAC item collection: ${stacUrl}...`)
    const stacData = await (await fetch(stacUrl)).json()
    for (const feat of stacData.features || []) {
      const pts: [number, number][] = []
      collect2dPoints(feat.geometry?.coordinates, pts)
      if (!pts.length) continue
      let minX = Infinity,
        minY = Infinity,
        maxX = -Infinity,
        maxY = -Infinity
      for (const [x, y] of pts) {
        if (x < minX) minX = x
        if (y < minY) minY = y
        if (x > maxX) maxX = x
        if (y > maxY) maxY = y
      }
      const bbox: [number, number, number, number] = [minX, minY, maxX, maxY]
      if (!tileIntersectsAoi(pts, bbox, aoi)) continue
      const assets = feat.assets || {}
      const firstAsset: any = Object.values(assets)[0]
      const href = firstAsset?.href
      if (href) {
        tiles.push({
          filename: String(href).split('/').pop() || 'tile.laz',
          url: String(href),
          bbox,
          geometry: feat.geometry,
        })
      }
    }
  }

  return tiles
}

// ---------------------------------------------------------------------------
// Local Folder Picker (File System Access API) & File Saving
// ---------------------------------------------------------------------------

async function chooseSaveFolder(): Promise<FileSystemDirectoryHandle | null> {
  if ('showDirectoryPicker' in window) {
    try {
      const handle = await (window as any).showDirectoryPicker({ mode: 'readwrite' })
      selectedDirHandle = handle
      const inputEl = document.getElementById('outputDirDisplay') as HTMLInputElement
      inputEl.value = `Local Folder: ${handle.name}`
      log(`Selected local destination folder: ${handle.name}`)
      return handle
    } catch {
      return null
    }
  } else {
    const inputEl = document.getElementById('outputDirDisplay') as HTMLInputElement
    inputEl.value = 'Browser Downloads Folder'
    return null
  }
}

async function saveBlobToFolder(filename: string, blob: Blob): Promise<void> {
  if (selectedDirHandle) {
    const fileHandle = await selectedDirHandle.getFileHandle(filename, { create: true })
    const writable = await fileHandle.createWritable()
    await writable.write(blob)
    await writable.close()
  } else {
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
  }
}

// ---------------------------------------------------------------------------
// Search Missions, Download S3 Tiles, & Submit NOAA DAV Checkout Order
// ---------------------------------------------------------------------------

async function searchMissionsForAoi(wktAoi: string): Promise<MissionItem[]> {
  const resp = await noaaFetchJson('missions', 'POST', {
    aoi: wktAoi,
    dataTypes: ['Lidar', 'DEM'],
    published: 'true',
    page: 0,
    pageSize: 50,
    sort: 'desc',
    sortField: 'year',
  })

  const rawItems = resp?.missions?.data || []
  const results: MissionItem[] = []
  for (const item of rawItems) {
    const attrs = item.attributes || item
    const links = attrs.links || attrs.Links || item.Links || item.links || []
    let bulkUrl: string | null = null
    let metadataUrl: string | null = null
    for (const lnk of links) {
      const lid = String(lnk.linkTypeId || '')
      const ltitle = String(lnk.title || lnk.linkTypeName || '').toLowerCase()
      const uri = String(lnk.uri || lnk.link || '').trim()
      if (lid === '46' || ltitle.includes('bulk download')) bulkUrl = uri
      else if (lid === '35' || lid === '39' || ltitle.includes('metadata')) metadataUrl = uri
    }
    const isS3Bulk = Boolean(
      bulkUrl &&
        (bulkUrl.toLowerCase().includes('amazonaws.com') ||
          bulkUrl.toLowerCase().endsWith('index.html') ||
          bulkUrl.toLowerCase().includes('coast.noaa.gov/htdata'))
    )
    const vdatumList = attrs.verticalDatum || []
    const vdatum = vdatumList[0]?.name || 'NAVD88'
    results.push({
      missionId: String(item.missionId || item.id || attrs.id),
      title: String(item.title || attrs.title),
      year: attrs.year || attrs.date || item.year || 'N/A',
      dataType: String(item.dataType || attrs.dataType || 'Lidar'),
      verticalDatum: vdatum,
      bulkDownloadUrl: isS3Bulk ? bulkUrl : null,
      externalPortalUrl: bulkUrl && !isS3Bulk ? bulkUrl : null,
      metadataUrl,
    })
  }
  return results
}

async function handleDownloadS3(mission: MissionItem): Promise<void> {
  if (!currentAoiInfo || !mission.bulkDownloadUrl) return
  if (!selectedDirHandle && 'showDirectoryPicker' in window) {
    const picked = await chooseSaveFolder()
    if (!picked) {
      alert('Please select a destination folder on your computer to save the downloaded LiDAR/DEM files.')
      return
    }
  }

  const maxTiles = parseInt((document.getElementById('maxTiles') as HTMLInputElement).value || '10', 10)
  const btn = document.getElementById(`btn-s3-${mission.missionId}`) as HTMLButtonElement | null
  if (btn) btn.disabled = true

  try {
    setBanner('running', `⏳ <strong>STEP 1/2:</strong> Intersecting GeoJSON AOI with NOAA S3 tile index for Mission ${mission.missionId}...`, 10)
    const tiles = await findIntersectingS3TilesInBrowser(mission.bulkDownloadUrl, currentAoiInfo)
    tilesLayer.clearLayers()
    for (const t of tiles) {
      L.geoJSON(t.geometry, {
        style: { color: '#10b981', weight: 2, fillOpacity: 0.15 },
      })
        .bindPopup(`<strong>${t.filename}</strong><br/><a href="${t.url}" target="_blank">Direct S3 Link</a>`)
        .addTo(tilesLayer)
    }

    const selected = maxTiles <= 0 ? tiles : tiles.slice(0, maxTiles)
    log(`Found ${tiles.length} intersecting tiles! Downloading ${selected.length} tile(s)...`)

    await saveBlobToFolder(
      'aoi_noaa_compatible.geojson',
      new Blob([JSON.stringify(currentAoiInfo.noaa_restricted_geojson, null, 2)], { type: 'application/geo+json' })
    )

    let totalBytes = 0
    for (let i = 0; i < selected.length; i++) {
      const tile = selected[i]
      const pct = Math.round(((i + 0.2) / Math.max(selected.length, 1)) * 100)
      setBanner(
        'running',
        `⏳ <strong>DOWNLOAD IN PROGRESS (${pct}%):</strong> Tile ${i + 1} of ${selected.length} — <code>${tile.filename}</code>`,
        pct
      )
      if (btn) btn.textContent = `⏳ Downloading (${i + 1}/${selected.length} - ${pct}%)...`
      const resp = await fetch(tile.url)
      if (!resp.ok) throw new Error(`Failed to download ${tile.url}`)
      const blob = await resp.blob()
      totalBytes += blob.size
      await saveBlobToFolder(tile.filename, blob)
      log(`  [${i + 1}/${selected.length}] Saved ${tile.filename} (${(blob.size / (1024 * 1024)).toFixed(2)} MB)`)
    }

    const totalMb = (totalBytes / (1024 * 1024)).toFixed(2)
    const folderLabel = selectedDirHandle ? selectedDirHandle.name : 'Downloads'
    setBanner(
      'completed',
      `✅ <strong>DOWNLOAD FINISHED!</strong> Saved <strong>${selected.length} file(s) (${totalMb} MB)</strong> for Mission ${mission.missionId} to folder <code>${folderLabel}</code>`,
      100
    )
    if (btn) {
      btn.textContent = `✅ Finished! (${selected.length} Tiles Saved)`
      btn.disabled = false
    }
    log(`✅ DOWNLOAD FINISHED! Saved ${selected.length} file(s) (${totalMb} MB) to ${folderLabel}.`)
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    setBanner('error', `❌ <strong>DOWNLOAD ERROR:</strong> ${msg}`)
    log(`ERROR: ${msg}`)
    if (btn) btn.disabled = false
  }
}

async function handleSubmitOrder(mission: MissionItem): Promise<void> {
  if (!currentAoiInfo) return
  if (!selectedDirHandle && 'showDirectoryPicker' in window) {
    await chooseSaveFolder()
  }
  const emailRaw = (document.getElementById('userEmail') as HTMLInputElement).value
  const userEmail = normalizeEmail(emailRaw)

  try {
    setBanner('running', `⏳ Submitting custom NOAA DAV checkout order for Mission ${mission.missionId} (${userEmail})...`, 40)
    const detailResp = await noaaFetchJson('missions', 'POST', {
      aoi: currentAoiInfo.wkt_aoi,
      published: 'true',
      missionIds: [Number(mission.missionId)],
      sort: 'asc',
      pageSize: 10,
      page: 0,
      getGeoJson: 'true',
    })
    const mDetail = detailResp?.missions?.data?.[0] || {}
    const centroid = mDetail.centroid
    let zones: any = {}
    if (centroid) {
      zones = await noaaFetchJson('search/projectionZones', 'POST', {
        centroid,
        aoi: currentAoiInfo.wkt_aoi,
      })
    }

    const sp83 = zones?.sp83 || []
    const utm = zones?.utm || []
    let projectionId = '5'
    let zonesId: string | undefined
    let horizUnitsId = '4'
    let vertUnitsId = '1'
    if (sp83.length && sp83[0].projectionZonesId) {
      projectionId = '2'
      zonesId = String(sp83[0].projectionZonesId)
      horizUnitsId = '3'
      vertUnitsId = '3'
    } else if (utm.length && utm[0].utmZone) {
      projectionId = '1'
      zonesId = String(utm[0].utmZone)
      horizUnitsId = '1'
      vertUnitsId = '1'
    }

    const [minX, minY, maxX, maxY] = currentAoiInfo.bbox_4326
    const [mercMinX, mercMinY, mercMaxX, mercMaxY] = currentAoiInfo.bbox_3857
    const jobObj: Record<string, any> = {
      label: mission.title,
      missionId: String(mission.missionId),
      minX,
      minY,
      maxX,
      maxY,
      mercMinX,
      mercMinY,
      mercMaxX,
      mercMaxY,
      horizontalDatumId: '2',
      horizontalUnitsId: horizUnitsId,
      verticalDatumId: '3',
      verticalUnitsId: vertUnitsId,
      projectionId,
      aoi: currentAoiInfo.wkt_aoi,
      vDatumLabel: 'NAVD88',
      geoidModel: GEOID18_UUID,
    }
    if (zonesId) jobObj.zonesId = zonesId

    if (mission.dataType.toLowerCase() === 'dem') {
      jobObj.dataType = 'DEM'
      jobObj.dataTypeId = 2
      jobObj.fileFormatId = '1'
    } else {
      jobObj.dataType = 'Lidar'
      jobObj.dataTypeId = 5
      jobObj.addAncillaryData = '0'
      jobObj.dataReturnsSelection = '0'
      jobObj.lidarDataClasses = String(mDetail?.attributes?.details?.lidar?.groundClasses || '2')
      jobObj.products = {
        point: { fileFormatId: '13' },
        grid: {
          fileFormatId: '8',
          binMethodId: '2',
          binUnitsId: ['1', '2', '3'].includes(horizUnitsId) ? horizUnitsId : '1',
          binSize: '3',
          fillGaps: 'true',
        },
      }
    }

    const submitResp = await noaaFetchJson('requests/request/submit', 'POST', {
      jobs: {
        tier: 'production',
        userEmail,
        organization: DEFAULT_ORG,
        surveyOptIn: 'false',
        digest: 'false',
        job: [jobObj],
      },
    })

    const requestId = submitResp?.data?.requestId || submitResp?.requestId
    const statusUrl = `https://coast.noaa.gov/dataviewer/#/admin/request/${requestId}`
    await saveBlobToFolder(
      `noaa_dav_order_${requestId || mission.missionId}.json`,
      new Blob([JSON.stringify({ requestId, userEmail, missionId: mission.missionId, statusUrl, submitResp }, null, 2)], {
        type: 'application/json',
      })
    )

    setBanner(
      'completed',
      `✅ <strong>NOAA ORDER SUBMITTED!</strong> Request ID <strong>#${requestId}</strong> (${userEmail}) — <a href="${statusUrl}" target="_blank" style="color:#a7f3d0;">Track Order Status</a>`
    )
    log(`✅ Submitted NOAA Request ID #${requestId} for ${userEmail}. Status: ${statusUrl}`)
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    setBanner('error', `❌ <strong>ORDER ERROR:</strong> ${msg}`)
    log(`ERROR: ${msg}`)
  }
}

function renderMissions(missions: MissionItem[]): void {
  const container = document.getElementById('missionsList')!
  if (!missions.length) {
    container.innerHTML = 'No published NOAA LiDAR/DEM datasets found for this AOI.'
    return
  }
  container.innerHTML = ''
  for (const m of missions) {
    const card = document.createElement('div')
    card.className = 'mission-card'
    card.innerHTML = `
      <div class="mission-title">[${m.dataType}] ${m.title}</div>
      <div class="mission-meta">
        Mission ID: <strong>${m.missionId}</strong> | Year: <strong>${m.year}</strong> | VDatum: ${m.verticalDatum || 'NAVD88'}
      </div>
      <div class="mission-actions"></div>
    `
    const actions = card.querySelector('.mission-actions')!
    if (m.bulkDownloadUrl) {
      const s3Btn = document.createElement('button')
      s3Btn.id = `btn-s3-${m.missionId}`
      s3Btn.className = 'success'
      s3Btn.textContent = 'Instant Download Tiles (S3)'
      s3Btn.onclick = () => handleDownloadS3(m)
      actions.appendChild(s3Btn)
    }
    const orderBtn = document.createElement('button')
    orderBtn.textContent = 'Submit NOAA Checkout Order (Email)'
    orderBtn.onclick = () => handleSubmitOrder(m)
    actions.appendChild(orderBtn)

    if (m.externalPortalUrl) {
      const extBtn = document.createElement('button')
      extBtn.className = 'secondary'
      extBtn.textContent = 'External Portal'
      extBtn.onclick = () => window.open(m.externalPortalUrl!, '_blank')
      actions.appendChild(extBtn)
    }
    container.appendChild(card)
  }
}

async function handleGeojsonFile(file: File): Promise<void> {
  try {
    log(`Reading ${file.name} (${(file.size / (1024 * 1024)).toFixed(2)} MB)...`)
    const rawText = await file.text()
    const sourceCrs = (document.getElementById('sourceCrs') as HTMLInputElement).value.trim()
    const aoi = parseAndNormalizeGeojson(rawText, sourceCrs || undefined)
    currentAoiInfo = aoi

    if (aoiLayer) map.removeLayer(aoiLayer)
    tilesLayer.clearLayers()
    aoiLayer = L.geoJSON(aoi.noaa_restricted_geojson, {
      style: { color: '#38bdf8', weight: 3, fillOpacity: 0.2 },
    }).addTo(map)
    map.fitBounds(aoiLayer.getBounds(), { padding: [30, 30] })

    const statsEl = document.getElementById('aoiStats')!
    statsEl.style.display = 'block'
    statsEl.innerHTML = `
      <strong>File:</strong> ${file.name} (${aoi.feature_count.toLocaleString()} features)<br/>
      <strong>Detected CRS:</strong> ${aoi.source_crs} &rarr; EPSG:4326<br/>
      <strong>Area:</strong> ${aoi.area_sq_miles} sq mi (${aoi.area_acres} acres)<br/>
      <strong>Vertices:</strong> ${aoi.original_vertex_count} (simplified to ${aoi.simplified_vertex_count})
    `

    log(`Searching NOAA Data Access Viewer for ${aoi.area_sq_miles} sq mi AOI...`)
    const missions = await searchMissionsForAoi(aoi.wkt_aoi)
    log(`Found ${missions.length} intersecting NOAA LiDAR/DEM datasets.`)
    renderMissions(missions)
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    setBanner('error', `❌ <strong>GEOJSON ERROR:</strong> ${msg}`)
    log(`ERROR: ${msg}`)
    alert(msg)
  }
}

// Wire up UI events
window.addEventListener('DOMContentLoaded', async () => {
  await discoverApiBase()

  const dz = document.getElementById('dropzone')!
  const fileInput = document.getElementById('fileInput') as HTMLInputElement
  const pickFolderBtn = document.getElementById('pickFolderBtn')!

  dz.addEventListener('click', () => fileInput.click())
  fileInput.addEventListener('change', () => {
    if (fileInput.files?.[0]) handleGeojsonFile(fileInput.files[0])
  })
  dz.addEventListener('dragover', (e) => {
    e.preventDefault()
    dz.style.borderColor = '#10b981'
  })
  dz.addEventListener('dragleave', (e) => {
    e.preventDefault()
    dz.style.borderColor = '#38bdf8'
  })
  dz.addEventListener('drop', (e) => {
    e.preventDefault()
    dz.style.borderColor = '#38bdf8'
    if (e.dataTransfer?.files?.[0]) handleGeojsonFile(e.dataTransfer.files[0])
  })

  pickFolderBtn.addEventListener('click', () => chooseSaveFolder())
})
