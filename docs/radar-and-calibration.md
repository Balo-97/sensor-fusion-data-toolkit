# Radar, CSV and calibration extensions

The local annotator now has bounded CSV/TSV summaries, JSON structure inspection, Astyx radar detection parsing, Astyx calibration extraction and A2D2 frame-sidecar pose extraction. These are project extensions, not NPL software. No account or new package is needed for the implemented pilot.

RadarScenes sequence and matched-camera downloads, HDF5 parsing, scene/odometry joins, sensor poses and paired radar/image metadata are now implemented too. This extension adds optional `h5py`/NumPy dependencies. RadarScenes contains no LiDAR. See the [RadarScenes commands and validation guide](radarscenes.md).

For camera + LiDAR, [the A2D2 adapter](a2d2-fusion.md) now downloads paired PNG/NPZ files, extracts the full camera/LiDAR calibration, and writes per-point CSVs and previews. It is tested on three real frame pairs. The combined project suite now passes 103 tests.

## Reproduce the radar download and tests

From the project directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata benchmark --manifest configs/radar-samples.json --output outputs/radar-benchmark
```

This downloads three original Astyx HiRes2019 radar point clouds, three matching calibration JSON files and the license (219,449 bytes total). The public GitHub copy is fixed to commit `2310e7f35a4f2b76de85463f0dd1b0b4e50c2f69`; every file has a checked SHA-256. Original radar files are whitespace-separated TXT, not CSV. Keep their `radar_6455/` folder: the parser requires that layout and the exact `X Y Z V_r Mag` header.

The three point clouds contain 831, 855 and 890 detections. Each calibration file describes radar, LiDAR and camera sensors. The metadata preserves `T_to_ref_COS` matrices and available camera `K` matrices, and checks dimensions, finite values, homogeneous bottom rows, rotation orthonormality and determinant. These checks do not establish physical calibration accuracy. Transforms are not applied and units/sign conventions are not guessed.

Radar metadata records the relative path of its matching calibration file in `provenance.related_files`. It does not automatically join calibration values into a transformed point cloud. No sample has been passed into Chat2Scenario: detection clouds lack the required highD trajectories and lane/neighbour fields.

## CSV and JSON behavior

```powershell
.\.venv\Scripts\python.exe -m scenario_metadata annotate C:/data/my-data --output outputs/my-data
.\.venv\Scripts\python.exe -m scenario_metadata annotate C:/data/table.csv --csv-delimiter ';' --csv-no-header --output outputs/headerless
```

- CSV defaults to comma separation, UTF-8 (optional BOM), and a header in the first record. TSV uses tabs. Delimiter and header behavior can be set explicitly for `annotate`.
- Summaries contain column names, observed lexical types, empty-cell counts, numeric counts/ranges, nonfinite-value counts and ragged-row diagnostics. Numeric ranges cover the numeric entries in each column, not strings or missing cells. Duplicate headers remain separate columns identified by index.
- Scanning stops at 100,000 rows or a 32 MiB text budget; individual physical lines are capped at 1 MiB. Limited samples have `row_count: null` and `sample_limited: true`; ranges/counts refer to the scanned prefix. Checksums still cover the whole file. Malformed quoted CSV and invalid UTF-8 produce partial annotations.
- JSON is capped at 8 MiB and rejects duplicate keys and nonfinite numbers. Unrecognized JSON receives a structural summary; arbitrary `K`/`R`/`T` keys are not sufficient to assert a calibration convention.
- Astyx JSON is recognized through its `sensors` / `sensor_uid` / `calib_data` structure. A2D2 frame JSON exposes `pcld_view` vectors and original source fields; it is **not** the full A2D2 camera-intrinsic configuration. Source timestamps are preserved without a guessed epoch/unit conversion.
- JSON/XML annotations use additive schema version 1.1. Existing file/image/PCAP fields remain available.

## Recommended datasets

| Priority | Dataset | Why use it | Access and remaining implementation |
|---|---|---|---|
| Start now | [Astyx HiRes2019 public GitHub copy](https://github.com/under-the-radar/radar_dataset_astyx) | Small paired radar detections and real calibration JSON; inexpensive reproducible tests | Implemented sample download and parsers; no account. CC BY-NC-SA 4.0 notice included. Three frames do not establish temporal-mining performance. |
| Implemented temporal radar | [RadarScenes](https://radar-scenes.com/dataset/structure/) | Per-detection range, azimuth, radial velocities, RCS, semantic labels and track IDs, with sensor poses | [Official Zenodo release](https://zenodo.org/records/4559821): selected sequence downloads via HTTP ranges; HDF5 and JSON joins implemented and tested on sequences 158 and 81. No account needed. See [the guide](radarscenes.md). |
| Next for multiple sensors | [nuScenes](https://www.nuscenes.org/nuscenes?tutorial=nuscenes) | Radar PCD, cameras/LiDAR and linked `calibrated_sensor` / sensor / sample metadata | Official portal asks for registration/login. Start with the mini split, not the full release. Requires its PCD reader and token-table joins; not yet implemented or downloaded here. |

[RADIATE](https://pro.hw.ac.uk/radiate/downloads/) is an additional option if adverse-weather radar images are the priority. Its sample is linked publicly; full access uses registration and a verified organizational email. Its radar representation and YAML calibration need different parsing from point detections and JSON. See the [official format documentation](https://pro.hw.ac.uk/radiate/doc/dataset/).

The full RadarScenes archive was not downloaded; selected sequence members were fetched with HTTP ranges. Registration does not substitute for selecting the dataset's license and formats appropriate to the research.

## Download pipeline for larger releases

The implemented pilot uses: select manifest -> download or reuse verified files -> extract format-specific metadata -> preserve calibration association -> JSON/XML -> compare expected facts -> report.

RadarScenes now has a separate ZIP range downloader with byte budgets, bounded safe member extraction, CRC validation, cached-member repair and source hashes. It selects sequences for acquisition and scenes for processing; it does not verify the full-archive publisher MD5 or resume an interrupted member. The generic manifest downloader remains unchanged. nuScenes still needs an adapter and authenticated acquisition workflow.

## Effort estimates

These are approximate engineering effort estimates, assuming usable documentation and accessible data; they exclude download time and access approval.

- Current baseline: CSV, the two recognized JSON layouts and Astyx sample download/extraction are implemented and tested.
- Another documented detection format plus calibration mapping: roughly **1-3 days per dataset** for parsing, source associations and meaningful tests.
- Resumable/authenticated archive acquisition and safe subset extraction: roughly **1-2 additional days**, depending on the host.
- A dependable 2-3-dataset radar/calibration workflow: approximately **1-2 weeks** including integration and data-quality edge cases.
- Raw ADC/IQ radar signal processing, detection/tracking or conversion to Chat2Scenario trajectories: a separate, substantially larger task; it needs dataset-specific scoping before a reliable estimate.

Validation in this workspace: Astyx pilot 7/7 files; original public sample benchmark 7/7 files; 80 software tests passed with RadarScenes camera support. Real RadarScenes checks and source-data findings are in [the guide](radarscenes.md). Tests limit BLAS threads to one and use a fresh project-local temporary directory to avoid Windows paging-file and system-temp permission issues.
