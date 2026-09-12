# A2D2 camera and LiDAR metadata

We selected **A2D2** for paired camera/LiDAR experiments. Its sensor suite has six cameras and five Velodyne VLP-16 units; it also supplies vehicle-bus data. The [dataset site](https://a2d2-dataset.github.io/) and [AWS Open Data registry](https://registry.opendata.aws/aev-a2d2/) provide individual-file access without an account. The [paper](https://arxiv.org/abs/2004.06320) describes the sensor setup and calibration.

Our new adapter downloads a small subset, validates the camera-to-point-cloud references, extracts metadata from both modalities and the full calibration JSON, and exports LiDAR points to CSV. It uses the same `paper-inspired-local` implementation as the other experiments; it does not invoke NPL or run a sensor-fusion algorithm.

## Run from a clone

Follow the [installation instructions](../README.md#installation), then run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata a2d2 run
```

For an existing Python 3.12 virtual environment, install this adapter with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[a2d2,test]"
```

Defaults: sequence `20180810_150607`, camera view `front_center`, frame IDs **60, 61 and 62**. Each point cloud can contain returns from several physical LiDAR units. This run selects one camera and observes all five LiDAR units: two sensor modalities, six physical sensor devices represented in the selected data. It does not download all six camera streams or vehicle-bus signals.

The first download fetched **12 files / 13,156,094 bytes**: three PNGs, three camera JSONs, three point-cloud NPZs, `cams_lidars.json`, README and license. Rerunning checks remote object metadata and reuses intact local files. `run` creates a new timestamped output directory; an explicit `--output` directory must be empty.

Download and process the next three frame IDs:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata a2d2 run --start-frame 63 --max-frames 3
```

Or download once and annotate offline:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata a2d2 download --start-frame 60 --max-frames 3
.\.venv\Scripts\python.exe -m scenario_metadata a2d2 annotate --start-frame 60 --max-frames 3
```

Use `--sequence YYYYMMDD_HHMMSS` and `--camera front_center|front_left|front_right|side_left|side_right|rear_center` for another selection in the sequential `camera_lidar` split. The adapter requests exact consecutive frame IDs, not the first N files in a directory. Availability and starting IDs can differ by view; a missing file fails explicitly. Matching frame numbers across two camera views must not be assumed to mean matching times. The live validation reported below covers the default front-center selection.

/tranDownloads default to a 100 MB total selectionsfer budget and 25 MB per file. Increase `--max-download-mb` and `--max-file-mb` if needed. The selection-size guard includes cached files; transfer counters record actual downloaded payload bytes. Up to 1,000 frame IDs may be requested per run; the byte limits normally constrain large selections earlier.

## View LiDAR as CSV

**CSV export is on by default.** Each frame produces:

- `points/000000060.csv`: every source point, in original row order.
- `points/000000060.preview.csv`: the first 100 points for quick inspection.
- `metadata/points/000000060.csv.metadata.json` and `.xml`: CSV structure/statistics, units and source NPZ hash.

To generate the files used by the Python example below, choose a new empty output directory:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata a2d2 run --output outputs/a2d2-fusion-csv
```

The verified frame counts are 18,199, 18,035 and 18,297 points for frames 60–62. Each preview contains 100 rows. Generated data is excluded from the repository.

| Columns | Meaning |
|---|---|
| `source_point_index` | Zero-based row in the original NPZ |
| `x`, `y`, `z` | Metres in the registered camera-view frame |
| `reflectance` | Original intensity values; no radiometric calibration added |
| `lidar_id` | Physical LiDAR ID; resolve its name using the camera JSON's `lidar_ids` |
| `timestamp` | Original per-point timestamp, preserved as an integer |
| `rectime` | Original recording/receive time, if supplied; preserved separately |
| `row`, `col` | Publisher's projected pixel coordinates in the undistorted image |
| `distance`, `depth` | Original distance/depth values in metres |
| `valid`, `boundary`, `azimuth` | Additional source fields where supplied, retained without filtering |

No points are dropped, rounded for display, resampled or transformed. `nan`/`inf` values, if present, remain explicit in CSV; JSON statistics count them and use finite values for ranges. CSV integers preserve the source microsecond timestamps. When opening in spreadsheet software, import long timestamp columns as **text** to retain all digits. Python can read them exactly with the standard library:

```python
import csv

with open("outputs/a2d2-fusion-csv/points/000000060.csv", newline="") as stream:
    for point in csv.DictReader(stream):
        sensor = int(point["lidar_id"])
        timestamp = int(point["timestamp"])
        x, y, z = (float(point[axis]) for axis in ("x", "y", "z"))
        # Add your analysis here. Coordinates are already in the camera-view frame.
```

Use `--no-point-csv` to omit full CSVs and previews while keeping the original NPZs and metadata.

## Extracted metadata and paired output

| Output | What it contains |
|---|---|
| `inputs/<frame>/` | Byte-for-byte copies of the camera PNG, camera JSON and LiDAR NPZ |
| `cams_lidars.json` | Original camera/LiDAR/vehicle calibration configuration |
| `metadata/<frame>/` | JSON/XML annotations for each source artifact: SHA-256, file properties and format-specific information |
| `metadata/cams_lidars.json.metadata.*` | All six camera intrinsics, distortion, resolution, time-delay fields and poses; five LiDAR poses and the vehicle view |
| `points/` and `metadata/points/` | Full point tables, previews and full-CSV metadata |
| `fusion_pairs.csv` | One summary row per image/point-cloud pair, including point count and recorded-time bounds |
| `fusion_manifest.json` / `.xml` | Source hashes, association checks, per-sensor counts, timing fields, pixel bounds and metadata links |
| `a2d2_report.json` | Compact run status and coverage |

The point-cloud parser supports both tutorial-style keys (`points`, `timestamp`, etc.) and the sequential files' prefixed keys (`pcloud_points`, `pcloud_attr.timestamp`, etc.). Metadata records the mapping. NPZ loading rejects object/pickle arrays, duplicate fields, incorrect dimensions and mismatched lengths. Both compressed input and total uncompressed array data are limited to 128 MiB, with at most one million points per file.

The generic `annotate` command also recognizes these NPZs and `cams_lidars.json`. Its LiDAR summaries cover at most 100,000 points and explicitly mark sampling. The joint A2D2 manifest summarizes all points in each selected file. Generic CSV metadata retains its existing 100,000-row/32-MiB limit; full point CSV exports retain all points within the NPZ input limits.

Associations require matching sequence/view/frame plus the explicit camera JSON `image_png` and `pcld_npz` references. Physical sensor IDs must map to calibration entries. Image dimensions are compared with calibration resolution. We validate finite calibration values, matrix dimensions and orthonormal pose axes; these checks do not establish physical accuracy.

## Timing and fusion-study limitations

The inspected sequential files contain two distinct time fields. For frame 60, `rectime - timestamp` ranges from **37,000,100 to 37,001,554 microseconds**. The pipeline preserves both and does not silently subtract 37 seconds, assume a UTC/TAI conversion, or substitute one clock for the other.

`rectime_minus_camera_us` is explicitly a comparison of recorded/receive times with the camera's stored `cam_tstamp`. It is **not a calibrated acquisition-latency measurement**. The full calibration also supplies a front-center `tstamp_delay` of **25,000 microseconds**, recorded but not applied. The delay sign and acquisition/transport/clock effects must be established before quantitative timing-error propagation.

| Frame | Point rows | Observed LiDAR units | Recorded-time minus camera range |
|---|---:|---:|---|
| 60 | 18,199 | 5 | −120.287 to +74.806 ms |
| 61 | 18,035 | 5 | −106.189 to +65.573 ms |
| 62 | 18,297 | 5 | −124.410 to +76.834 ms |

These point clouds are already processed and registered to camera views. Applying a raw LiDAR-to-camera transform to their XYZ coordinates again would use the wrong starting frame. Publisher `row`/`col` values refer to **undistorted** images; we preserve the original PNGs and do not claim to validate a pixel overlay on them. Adjacent clouds/views can reuse measurements, so 54,531 point rows is not a count of unique independent returns. Calibration matrices and registered pixels are provided data, not independent ground truth for evaluating those same calibration parameters.

A2D2 images have publisher-applied privacy blurring. The adapter does not supply measurement covariances, calibration uncertainty, clock uncertainty or object-level camera/LiDAR correspondences. It prepares traceable input for fusion/error-propagation work; the fusion model, perturbation experiments and independent evaluation remain to be implemented. Vehicle-bus data is available in the dataset but not downloaded here. A2D2 contains no radar stream in this sensor suite.

## Provenance and validation

The downloader reads HTTPS S3 object metadata, requires a single-part MD5-style ETag, uses `If-Match` during download, and verifies body MD5 and size. It records local SHA-256 for every file. It retries transient GET failures and repairs damaged cached files; interrupted files restart. The separate 193 MB publisher `CHECKSUMS.txt` was not downloaded or verified. ETags are an integrity check, not an authenticated publisher signature or a frozen release identifier. Retain the download report's hashes when reproducing an experiment against this mutable mirror.

The full software suite passes **103 tests**. The three real pairs downloaded and annotated successfully; CSV row counts, row order, XYZ floating-point values, integer timestamps, reflectance and sensor IDs were independently compared with the original NPZ arrays. These are extraction/association checks, not validation of a fusion algorithm or NPL's internal tool.

Dataset license and attribution are preserved in the downloaded README and `LICENSE.txt`. See the [official A2D2 tutorial](https://audi-autonomous-driving-dataset.s3.eu-central-1.amazonaws.com/tutorial.ipynb) for camera matrices, view coordinates and image/point-cloud conventions.
