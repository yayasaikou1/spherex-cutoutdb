# DOWNLOAD_STRATEGY.md

## Access strategy summary

Use this fallback hierarchy:

1. **SIA2 discovery** through the official IRSA SIA endpoint.
2. **Official cloud/S3 access** only when `cloud_access` metadata supports the requested product access.
3. **IRSA on-prem cutout URLs** for SPHEREx Spectral Image MEF cutouts by appending `center=<ra>,<dec>&size=<size>` to the parent MEF `access_url`.
4. **Optional TAP fallback** only when SIA2 is unavailable, delayed, or explicitly requested.

The default requested product is a cutout, not a full parent image. Therefore the normal download path is SIA2 discovery followed by on-prem cutout URL download.

Do **not** invent S3 cutout endpoints. Do not reproduce SPIFF's local S3 cutout idea in this package. S3 may be used for full product access only when explicitly configured and supported by official metadata.

## SIA2 discovery

### Endpoint

```text
https://irsa.ipac.caltech.edu/SIA
```

### Collections

Use these SPHEREx QR2 collections for Level-2 spectral image discovery:

```text
spherex_qr2       # QR2 Wide Survey Spectral Image MEFs
spherex_qr2_deep  # QR2 Deep Survey Spectral Image MEFs
```

Recognize but exclude by default:

```text
spherex_qr2_cal   # QR2 calibration files, not Level-2 cutout targets
```

### Query pattern

For each active source and collection:

```text
GET https://irsa.ipac.caltech.edu/SIA?
    COLLECTION=<collection>&
    POS=circle+<ra_deg>+<dec_deg>+<radius_deg>&
    RESPONSEFORMAT=VOTABLE&
    MAXREC=<maxrec>
```

Optional SIA constraints may be added later:

```text
BAND=<em_min_m> <em_max_m>
TIME=<mjd_min> <mjd_max>
FORMAT=image/fits
DPTYPE=image
CALIB=2
```

Do not add constraints that could accidentally drop valid SPHEREx Level-2 Spectral Image MEFs unless the user configured them.

### Discovery caching

For each source and collection, store:

- query RA/Dec;
- search radius;
- collection;
- config hash;
- run ID;
- timestamp;
- number of returned products;
- raw result row hashes.

Skip rediscovery if the same source row hash, discovery config hash, collection, and search radius were already queried and the cache TTL has not expired, unless `--force-discovery` is set.

## Cloud access policy

The SIA2 table may include `cloud_access` metadata. Preserve it exactly and parse it conservatively.

### Allowed cloud use

Cloud/S3 access is allowed for:

- full parent MEF products when explicitly configured, and only when the metadata identifies an official S3 object or equivalent product access;
- future cloud cutout operations if and only if the official SIA2/cloud metadata explicitly advertises a cutout operation or URL template.

### Disallowed cloud use

The package must not:

- infer an S3 key from an on-prem URL and then synthesize cutouts locally as the default cutout mechanism;
- create undocumented S3 cutout URLs;
- crop a full S3 MEF and call it an official IRSA cutout;
- silently fall back from failed unofficial S3 cutout behavior to full image downloads.

If a user explicitly configures full-image mode, S3 can download full parent files to `data/full_products/`, but this is outside the default cutout workflow.

## Cutout URL construction

For each discovered parent MEF row:

1. require `access_url`;
2. compute effective source cutout size:
   - source-specific `cutout_size_arcsec` if present;
   - otherwise `config.cutouts.default_size_arcsec`;
3. convert size to decimal degrees:
   - `size_deg = size_arcsec / 3600`;
4. append query parameters to parent access URL:
   - `center=<ra_deg>,<dec_deg>`;
   - `size=<size_deg>`;
5. store the exact URL in `download_plan.cutout_url` and `cutouts.cutout_url_used`.

Example:

```text
https://irsa.ipac.caltech.edu/ibe/data/spherex/qr2/level2/.../parent.fits?center=156.09328159,-41.64466331&size=0.1
```

Implementation details:

- Use `urllib.parse` to append query parameters.
- If the parent URL already has query parameters, preserve them and add new ones.
- Use enough decimal precision to avoid center drift, e.g. RA/Dec `%.10f`, size `%.10g`.
- Do not append a `d` unit suffix unless the official documentation changes.
- Record center and size separately in DB, not only inside the URL string.

## Planning strategy

The planner creates one desired cutout per `(source, parent product, cutout size, center, access method)`.

### Product signature

Use a stable product signature containing:

```text
collection
obs_publisher_did or access_url
parent_filename
observation_id
planning_period
detector_id
processing_version
processing_date
```

### Cutout key

Hash canonical JSON containing:

```text
source_id
product_signature
cutout_ra_deg
cutout_dec_deg
cutout_size_arcsec
access_method
```

### Plan actions

| Action | Meaning |
|---|---|
| `download` | No valid local cutout exists. |
| `skip_valid` | Existing file matches cutout key and validates. |
| `validate_existing` | File exists but validation is missing or stale. |
| `redownload_invalid` | Existing file failed validation and config permits redownload. |
| `redownload_processing_update` | New processing version/date found. |
| `skip_duplicate` | Duplicate plan row already covers desired cutout. |
| `fail_no_access_url` | Discovery row has no usable parent URL. |
| `fail_no_official_cloud_cutout` | Cloud cutout requested but not officially advertised. |

## Download strategy

### HTTP streaming

Use `requests.Session` or `httpx.Client` with:

- connection timeout;
- read timeout;
- bounded chunk size;
- retry policy;
- user-agent;
- optional per-host rate limiting;
- progress reporting.

The production scheduler flattens `download_plan` rows into file-level work
items. Target/source grouping is retained for summaries and downstream
photometry context, but it is not the network scheduling unit. Network attempts
run in a bounded thread pool with one `requests.Session` per worker. Project-level
retry/requeue logic owns backoff, so adapter-level retries are disabled.
Per-host request-start pacing is controlled by
`download.per_host_rate_limit_per_second`; use `0` only for trusted local or
non-IRSA endpoints. Active same-host downloads are bounded by
`download.per_host_max_concurrency` when set, otherwise by the global worker
count.

For large batches, an HTTP response can keep a worker occupied by trickling data
well below useful throughput. `download.min_download_rate_bytes_per_second`
enables a low-speed watchdog: if a transfer stays below that rate for
`download.low_speed_time_sec`, the attempt is aborted and handled by the normal
retry/requeue scheduler. The default value `0` disables this watchdog.

Write to:

```text
data/partial/<cutout_key>.fits.part
```

After bytes are complete:

1. compute SHA256;
2. optionally run preliminary FITS open on the `.part` file;
3. move to final path atomically with `Path.replace()`;
4. run full validation on final path;
5. update `cutouts` table.

### Retries

Retry these HTTP statuses by default:

```text
408, 429, 500, 502, 503, 504
```

Retry these exception classes/categories:

```text
ConnectionError
Timeout
ChunkedEncodingError
IncompleteRead
RemoteDisconnected
ProtocolError
temporary DNS failures
```

Suggested retry schedule:

```text
attempts: 6
backoff seconds: 1, 2, 5, 15, 45, 120
jitter: 0-1 second
```

For `429`, honor the `Retry-After` header when present.

### Partial-download recovery

Rules:

- A final `.fits` path is never created until a complete byte stream has been written.
- `.part` files are safe to delete.
- Successful downloads and validation outcomes are recorded per file as soon as
  each file completes. The current SQLite schema does not persist in-progress
  partial byte counts.
- If the server supports byte ranges, a future implementation may resume; MVP may restart cutout downloads because cutouts are smaller than full MEFs.
- `spxcutdb clean-partials` removes stale `.part` files older than a configurable age.
- Invalid completed FITS files should be moved to `data/quarantine/` unless `--delete-invalid` is explicitly requested.

### Atomic move

Download path:

```text
data/partial/<cutout_key>.fits.part
```

Final path:

```text
data/cutouts/<source>/<collection>/<planning_period>/<processing_version>/D<detector>/<filename>.fits
```

Use same filesystem for partial and final paths when possible to make `Path.replace()` atomic.

## Validation after download

For every newly downloaded cutout:

1. verify file exists and has nonzero size;
2. compute SHA256;
3. open with `astropy.io.fits.open(memmap=False)`;
4. run FITS verification;
5. confirm required HDUs;
6. confirm IMAGE/FLAGS/VARIANCE are present and shape-compatible; record ZODI
   shape when present, but do not require or use ZODI for science photometry;
7. confirm PSF HDU exists and record shape/header metadata;
8. confirm WCS-WAVE HDU exists;
9. confirm spatial WCS can be instantiated;
10. confirm spectral WCS/WCS-WAVE metadata can be instantiated or at least summarized;
11. extract detector, observation, planning period, processing version/date, bandpass/wavelength metadata when available;
12. write `validation_results` and update `cutouts`.

## Incremental update logic

### Catalog updates

When the source catalog changes:

- upsert existing source IDs;
- mark removed sources as inactive, not deleted;
- keep existing cutouts for inactive sources unless `clean` command explicitly archives/deletes them;
- rediscover only sources with changed row hashes, new sources, or forced discovery.

### New SPHEREx coverage

Because SPHEREx data are released over time, discovery should support periodic re-runs. New parent products for existing sources become new `discovery_products`, new `source_product_matches`, and planned cutouts.

### Processing updates

When a parent product appears with a newer processing version or date:

- do not overwrite old records;
- plan a new cutout;
- after validation, mark old cutout as superseded according to policy;
- include processing version events in summaries.

### Existing valid files

A file is considered reusable only if:

- final local path exists;
- DB cutout key matches current plan;
- file size matches DB or is freshly checked;
- SHA256 matches DB or is freshly recomputed;
- validation status is acceptable;
- no relevant config change invalidates validation criteria.

## TAP fallback

TAP fallback is optional and disabled by default.

Allowed triggers:

- SIA2 request fails after retries;
- SIA2 returns a known service-unavailable response;
- user passes `--tap-fallback`;
- config allows fallback for weekly ingestion delay.

TAP fallback should:

- query official IRSA TAP tables only;
- return parent URLs or cutout URLs consistent with official documentation;
- record that discovery source was `tap_fallback`;
- never replace SIA2 as the normal discovery path in default config.

## Rate limiting and concurrency

Recommended defaults:

```text
SIA2 discovery concurrency: 2-4 source queries
Download concurrency: 4-8 cutouts
Validation concurrency: 2-4 files
Per-host request rate: <= 3 requests/sec
```

For thousands of sources:

- process sources incrementally;
- commit DB transactions per source or per small batch;
- bound in-memory discovery tables;
- use SQLite WAL;
- export Parquet for large manifests;
- avoid nested progress bars that create excessive terminal output.

## Failure handling

Every failure writes a `failures` row.

Failure phases:

```text
catalog
discovery
planning
download
validation
export
summary
```

Failure statuses:

```text
retryable
nonretryable
open
resolved
ignored
```

`retry-failed` should select failures where:

- phase is `download` or `validation`;
- status is `retryable` or `open`;
- the associated source/product/plan is still active.

## SPIFF-derived practical lessons to keep

Use SPIFF only as a practical reference for:

- a CLI-oriented Level-2 workflow;
- local FITS directory and download manifest ideas;
- transient TAP/SIA/DataLink/FITS download failure handling;
- retry and rerun behavior;
- user-visible progress messages.

Do not copy or implement SPIFF's fitting, PSF extraction, result compilation, autotyping, binned spectra, science plotting, or local S3 cutout behavior.
