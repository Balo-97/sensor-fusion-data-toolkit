# RadarScenes pipeline

Implemented: selective sequence and matched-camera downloads, HDF5 radar/odometry parsing, JPEG validation, scene and calibration associations, CSV export and JSON/XML metadata. Camera processing is enabled by default. This uses our `paper-inspired-local` annotator; it does not call NPL or execute Chat2Scenario on radar points.

RadarScenes provides **radar, camera and odometry, with no LiDAR**. The combined manifest records LiDAR as unavailable. Radar and LiDAR are different sensor modalities and are never substituted for one another.

## Run in PowerShell

Follow the [installation instructions](../README.md#installation) and run from the repository root. In an existing Python 3.12 virtual environment, install this adapter with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[radarscenes,test]"
```

Download one small sequence and the camera images associated with its first 25 radar measurements; annotate both:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes run --sequence sequence_158 --max-scenes 25
```

The command prints download and analysis report paths. Outputs use a new timestamped folder under `outputs/`. An explicit `--output` directory must be empty. Source data lives under `data/radarscenes/data/sequence_158/`; the repeated `data` preserves the publisher's structure.

## More measurements and sequences

A RadarScenes **scene means one measurement from one radar sensor**, not four synchronized scans or a mined driving scenario. Selections follow ascending timestamps across all four radars by default.

The downloader fetches the entire selected sequence's HDF5 and scene JSON, plus the unique camera images referenced by the selected scenes. Increasing `--max-scenes` reuses the radar file but can require additional images. Use `run` to acquire any missing images and process measurements 25–49, with a zero-based offset:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes run --sequence sequence_158 --start-scene 25 --max-scenes 25
```

Process all 804 measurements, or select 100 measurements from radar 1 only:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes run --sequence sequence_158 --max-scenes 804
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes run --sequence sequence_158 --sensor-id 1 --max-scenes 100
```

With `--sensor-id`, the offset applies **after filtering**. A selection extending past the end returns available scenes and reports their actual count; an empty selection is an error. Selected rows are held in memory, with a default limit of 1,000,000 detections. Set `--max-detections` only if sufficient memory is available.

List all 158 sequences, smallest compressed radar file first, then download others:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes list
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes download --sequences sequence_81 sequence_122
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes annotate --sequence sequence_81 --max-scenes 100
```

`list` accesses the remote ZIP index and costs about 5.6 MB. Downloads default to a 250 MB HTTP budget and 1,000 MB total extracted-size limit, including images. Change `--max-download-mb` and `--max-extracted-mb` as needed; MB means decimal megabytes. `download` accepts the same scene/sensor selection flags; its default is camera images for the first 100 scenes of each requested sequence. Several radar measurements may share one image, which is downloaded once.

Use `annotate` for an entirely offline run on previously downloaded images. Missing/corrupt images produce explicit diagnostics while preserving valid radar exports. `run` downloads missing files and repairs corrupt cached files. Use `--no-camera` on `download`, `run` or `annotate` to opt out of camera processing.

## Source and download method

Data comes directly from the [official Zenodo release, record 4559821](https://zenodo.org/records/4559821), with no account or API key needed. The ZIP is 11,128,859,838 bytes (about 11.1 GB). HTTPS Range requests fetch the ZIP directory and only these members:

- `Readme.md`, including publisher attribution and license information.
- `data/sequences.json` and `data/sensors.json`.
- Each requested sequence's `radar_data.h5` and `scenes.json`.
- `data/sequence_N/camera/<timestamp>.jpg` for the selected scenes, deduplicated by source filename.

For sequence_158, the compressed radar member is 4.61 MB; the first verified download transferred about 10.3 MB including directory/range overhead. The extracted HDF5 is 8.29 MB. This selects whole sequences, not individual remote HDF5 frames.

Completed files are checked against ZIP size and CRC32 before reuse; corrupt cached files are fetched again. Transient requests retry and extraction uses temporary `.part` files. Interrupted members restart; there is no byte-level resume within a member. Re-running `download` still reads the remote directory. `annotate` runs entirely offline.

`download_report.json` records the latest download operation, transfer bytes, member CRCs, local SHA-256 hashes and cache use. SHA-256 records local provenance, not publisher-provided per-member hashes. The release's full ZIP MD5 (`db4874b43e45faa0d635bafc58d56579`) is explicitly **unverified**, because partial downloads cannot establish it. Local ZIP mode also checks member CRCs without hashing the full archive.

If the server ignores Range, the downloader refuses a full-archive fallback. Download the official ZIP yourself and run:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes run --archive C:/data/RadarScenes.zip --sequence sequence_158 --max-scenes 25
```

Extraction checks selected paths, rejects traversal/symlink members, bounds extracted sizes and verifies CRCs. Unselected files are not extracted. Dataset terms and attribution are in the downloaded `Readme.md`; the release is labelled CC BY-NC-SA 4.0.

## Outputs and metadata

| Output | Contents |
|---|---|
| `detections.csv` | Original timestamp, sensor ID, range, azimuth, RCS, radial velocities, coordinates, UUID, track ID and class ID |
| `odometry.csv` | One associated ego-odometry row per selected scene; shared source rows may repeat |
| `scenes.json` | Scene references, original HDF5 ranges, exported CSV ranges and camera filenames |
| `sensors.json` | Original mounting poses in the recognized calibration schema |
| `camera/*.jpg` | Byte-for-byte copies of downloaded images used in the selection |
| `metadata/*.metadata.json` and `*.metadata.xml` | Five radar/export annotation pairs, with source hashes and sequence/split provenance |
| `metadata/camera/*.metadata.json` and `*.metadata.xml` | One annotation pair per unique available image: decode status, dimensions, image encoding, EXIF where available, SHA-256 and source timestamp |
| `fusion_manifest.json` and `fusion_manifest.xml` | Radar/camera associations, metadata paths, hashes, timestamps, calibration availability and per-scene radar nonfinite counts |
| `fusion_pairs.csv` | One row per radar measurement: paired image, signed time offsets, shared-image count and pair status |
| `radarscenes_report.json` | Counts, associations, data-quality findings, labels, distinct nonempty track IDs, units and calibration |

Use `exported_radar_indices` for the CSV, excluding its header. Original `radar_indices` refer to the source HDF5. UUIDs and track IDs retain their text; empty IDs remain empty. Track counts are counts of source IDs, not inferred trajectories.

Units follow the [publisher's format documentation](https://radar-scenes.com/dataset/structure/). Timestamps are microseconds from an arbitrary origin, **not Unix dates**. Sensor, car/rear-axle and sequence coordinates remain distinct. Sensor poses are 2D x/y/yaw; elevation and camera intrinsics are not invented. No transform or coordinate conversion is applied. Radial velocity is not a full object velocity vector.

Generic annotation also recognizes RadarScenes HDF5 and sensor JSON:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata annotate data/radarscenes/data/sequence_158/radar_data.h5 --output outputs/radarscenes-hdf5
.\.venv\Scripts\python.exe -m scenario_metadata annotate data/radarscenes/data/sensors.json --output outputs/radarscenes-calibration
```

HDF5 metadata summarizes at most 100,000 rows per array, reports complete shape counts separately and marks limited samples. The sequence report summarizes **all selected rows**. Export CSV sidecars retain existing 100,000-row/32-MiB summary limits. JSON statistics never contain NaN/Infinity literals.

## Validation and real data

The full suite passed 80 tests with camera support, including range responses/budgets, CRC failures, traversal, caching/repair, selection, row mappings, sensor/odometry mismatches, external HDF5 links, sampling, nonfinite data, camera deduplication, camera budgets, signed timing offsets, tolerance boundaries, missing/corrupt images and the joint CLI pipeline.

Real-data checks on 2026-09-12:

| Selection | Scenes | Detections | Result |
|---|---:|---:|---|
| sequence_158, first 25 | 25 | 2,309 | Selected rows and associations passed |
| sequence_81, first 25 | 25 | 4,198 | Selected rows and associations passed |
| sequence_158, all | 804 | 74,616 | Associations passed; nonfinite values reported |

In sequence_158, 920 detections have nonfinite values in each of `vr_compensated`, `x_seq` and `y_seq`. CSV preserves them as `nan`/`inf`/`-inf` text as applicable; summaries count them and compute finite ranges. No rows or values are filled in or dropped. The report returns `completed_with_warnings`, with CLI success because export completed. Invalid schemas, IDs, negative ranges, row bounds, overlapping selected ranges and timestamp associations fail with nonzero exit status.

Source HDF5 diagnostics can occur outside a clean selected subset; that status is reported separately. Checks establish structure and associations, not physical calibration accuracy, label correctness, complete scene-link chains or model performance.

## Studying sensor-fusion error propagation

Camera selection follows the publisher's `scenes.json` references, described as the closest camera timestamp. The pipeline does not assume simultaneous acquisition or recompute nearest matches from the full camera stream. In `fusion_pairs.csv`, `camera_minus_radar_us` is positive when the image occurs later; odometry offsets use the same sign convention. No timing cutoff is assumed by default.

To flag pairs beyond an experimental 50 ms limit, while retaining every pair:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata radarscenes run --sequence sequence_158 --max-scenes 25 --max-camera-offset-ms 50
```

This threshold is a user-selected experiment parameter, not a claim that 50 ms is acceptable for fusion. A pair's `outside_tolerance` status reports timing; missing/invalid images take precedence in `pair_status` while the separate tolerance field remains available. `completed_with_warnings` still means the export completed successfully. Consumers should check the pair statuses before using data.

The first 25 radar measurements in sequence_158 share **two 1067 × 480 images**. Observed camera offsets range from **−197.797 ms to +163.272 ms**. Thus 25 radar measurements are not 25 independent camera observations. The source HDF5 was reused; acquiring these images plus the ZIP directory transferred about 5.82 MB.

The manifest makes missing prerequisites explicit: camera intrinsics, radar-to-camera extrinsics, measurement covariances and clock uncertainty are unavailable in this adapter. Radar mounting poses alone cannot support calibrated radar-to-image projection. The publisher describes these as documentary images and reports repainting privacy regions; no repainting masks are supplied by this pipeline. See the [official camera documentation](https://radar-scenes.com/dataset/structure/).

This extension collects and validates paired inputs; it does **not** compute sensor fusion or uncertainty propagation. A quantitative experiment still needs a defined fusion algorithm, calibration, uncertainty models and evaluation ground truth. Shared frames, timing offsets and nonfinite fields are recorded so these can be controlled rather than hidden.

For LiDAR + camera (and radar) experiments, **nuScenes is a better next dataset to integrate**: its [official schema](https://github.com/nutonomy/nuscenes-devkit/blob/master/docs/schema_nuscenes.md) includes all three modalities, timestamped samples, sensor extrinsics and camera intrinsics. A nuScenes downloader/parser remains separate work; this RadarScenes command cannot acquire LiDAR that the dataset does not contain.

## Chat2Scenario relationship

```text
Official RadarScenes ZIP -> sequence + matched JPEGs -> radar/odometry/camera checks
                                                   -> CSV + JSON/XML + fusion pair manifest

Future: detections + source track IDs -> validated object trajectories
        + road/lane/neighbour context -> highD adapter -> Chat2Scenario
```

Point-level labels and object IDs help a future trajectory adapter, but do not directly supply the boxes, continuous velocities, lanes and neighbour relationships expected by our highD miner. RadarScenes support does not execute Chat2Scenario or claim to mine radar driving scenarios.
