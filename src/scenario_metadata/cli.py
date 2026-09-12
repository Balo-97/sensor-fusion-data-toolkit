from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from .downloads import download_dataset, load_manifest, selected_datasets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Public-data metadata benchmark and Chat2Scenario pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [("datasets", "List public sample datasets"),
                            ("download", "Download and verify public samples"),
                            ("benchmark", "Download public samples and check metadata correctness")]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--manifest", type=Path)
        sub.add_argument("--datasets", nargs="+")
        if name != "datasets":
            sub.add_argument("--data-dir", type=Path, default=Path("data"))
            sub.add_argument("--max-file-mb", type=float, default=100)
        if name == "benchmark":
            sub.add_argument("--output", type=Path, default=Path("outputs/benchmark"))
    annotate = commands.add_parser("annotate", help="Write JSON and XML file metadata sidecars")
    annotate.add_argument("path", type=Path)
    annotate.add_argument("--output", type=Path, default=Path("outputs/annotations"))
    annotate.add_argument("--csv-delimiter", default=",", help="CSV separator; default comma (TSV uses tab)")
    annotate.add_argument("--csv-no-header", action="store_true", help="Treat the first CSV/TSV row as data")
    bootstrap = commands.add_parser("bootstrap", help="Fetch checksum-pinned Chat2Scenario Python sources")
    bootstrap.add_argument("--repo", type=Path, default=Path("vendor/Chat2Scenario"))
    for name, help_text in [("mine", "Mine highD trajectories with upstream Chat2Scenario, then annotate"),
                            ("demo", "Run upstream mining on a clearly labelled synthetic highD fixture")]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--repo", type=Path, default=Path("vendor/Chat2Scenario"))
        sub.add_argument("--output", type=Path)
        sub.add_argument("--max-scenarios", type=int, default=20)
        sub.add_argument("--python", dest="python_executable")
        if name == "mine":
            sub.add_argument("--tracks", type=Path, required=True)
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument("--scenario", help="Natural-language scenario (requires OPENAI_API_KEY and --model)")
            group.add_argument("--structured-scenario", type=Path, help="Preclassified JSON; skip only the LLM step")
            sub.add_argument("--model", help="Explicit OpenAI model name for natural-language interpretation")
            sub.add_argument("--road-file", type=Path)
    a2d2 = commands.add_parser("a2d2", help="Download and annotate paired A2D2 camera/LiDAR frames")
    a2d2_commands = a2d2.add_subparsers(dest="a2d2_command", required=True)
    for name in ("download", "annotate", "run"):
        from .a2d2_download import CAMERAS
        sub = a2d2_commands.add_parser(name)
        sub.add_argument("--data-dir", type=Path, default=Path("data/a2d2-fusion"))
        sub.add_argument("--sequence", default="20180810_150607")
        sub.add_argument("--camera", choices=CAMERAS, default="front_center")
        sub.add_argument("--start-frame", type=int, default=60)
        sub.add_argument("--max-frames", type=int, default=3)
        if name in ("download", "run"):
            sub.add_argument("--max-download-mb", type=float, default=100)
            sub.add_argument("--max-file-mb", type=float, default=25)
        if name in ("annotate", "run"):
            sub.add_argument("--output", type=Path)
            sub.add_argument("--no-point-csv", action="store_true", help="Keep source NPZ and summaries without per-point CSV exports")
    radar = commands.add_parser("radarscenes", help="Download RadarScenes sequences and annotate selected radar measurements")
    radar_commands = radar.add_subparsers(dest="radar_command", required=True)
    for name in ("list", "download", "annotate", "run"):
        sub = radar_commands.add_parser(name)
        sub.add_argument("--data-dir", type=Path, default=Path("data/radarscenes"))
        if name in ("list", "download", "run"):
            sub.add_argument("--archive", type=Path, help="Read a locally downloaded official RadarScenes.zip instead of HTTPS ranges")
            sub.add_argument("--max-download-mb", type=float, default=250, help="HTTP byte budget including ZIP directory")
        if name in ("download", "run"):
            sub.add_argument("--max-extracted-mb", type=float, default=1000)
        if name == "download":
            sub.add_argument("--sequences", nargs="+", default=["sequence_158"])
        if name in ("download", "annotate", "run"):
            sub.add_argument("--no-camera", action="store_true", help="Opt out of matched-camera download and annotation")
            sub.add_argument("--start-scene", type=int, default=0, help="Zero-based offset after optional sensor filtering")
            sub.add_argument("--max-scenes", type=int, default=100)
            sub.add_argument("--sensor-id", type=int, choices=(1, 2, 3, 4))
        if name in ("annotate", "run"):
            sub.add_argument("--sequence", default="sequence_158")
            sub.add_argument("--max-camera-offset-ms", type=float, help="Flag pairs beyond this absolute time offset; default reports offsets without a cutoff")
            sub.add_argument("--max-detections", type=int, default=1_000_000)
            sub.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "a2d2":
            from .a2d2_download import download_a2d2
            options = {"sequence": args.sequence, "camera": args.camera, "start_frame": args.start_frame, "max_frames": args.max_frames}
            if args.a2d2_command in ("run", "annotate"):
                from .a2d2 import annotate_a2d2
                from .a2d2_metadata import numpy
                numpy()
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                output = args.output or Path("outputs") / f"a2d2-{args.sequence}-{args.camera}-{stamp}"
                if output.resolve().is_relative_to(args.data_dir.resolve()) or output.exists() and any(output.iterdir()):
                    raise ValueError("Choose an empty output directory outside the source dataset")
            if args.a2d2_command in ("download", "run"):
                download = download_a2d2(args.data_dir, **options,
                    budget=int(args.max_download_mb * 1_000_000), max_file_bytes=int(args.max_file_mb * 1_000_000))
                print(f"Verified {len(download['files'])} files; {download['transferred_bytes']/1_000_000:.2f} MB transferred.")
            if args.a2d2_command in ("run", "annotate"):
                report = annotate_a2d2(args.data_dir, output, **options, export_points=not args.no_point_csv)
                print(f"{report['status'].upper()}: {report['paired_frames']} paired camera/LiDAR frames, "
                    f"{report['total_point_rows']} point rows, {len(report['observed_physical_lidar_sensors'])} physical LiDAR units observed.")
                print("Time fields are preserved separately; no clock or camera-delay correction applied.")
                print((output / "fusion_manifest.json").resolve())
            else:
                print((args.data_dir / "download_report.json").resolve())
            return 0
        if args.command == "radarscenes":
            from .radarscenes_download import download_sequences, list_sequences
            if args.radar_command == "list":
                for entry in list_sequences(args.archive, int(args.max_download_mb * 1_000_000)):
                    print(f"{entry['sequence']:14} {entry['category']:10} {entry['n_scenes']:6} scenes  "
                          f"{entry['compressed_radar_bytes']/1_000_000:6.2f} MB compressed radar")
                return 0
            if args.radar_command in ("run", "annotate"):
                from .radarscenes import annotate_sequence, dependencies
                dependencies()
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                output = args.output or Path("outputs") / f"radarscenes-{args.sequence}-{stamp}"
                if output.exists() and any(output.iterdir()):
                    raise ValueError("Choose a new empty output directory")
            if args.radar_command in ("download", "run"):
                sequences = args.sequences if args.radar_command == "download" else [args.sequence]
                report = download_sequences(args.data_dir, sequences, archive=args.archive,
                    budget=int(args.max_download_mb * 1_000_000), max_extracted_bytes=int(args.max_extracted_mb * 1_000_000),
                    include_camera=not args.no_camera, start_scene=args.start_scene,
                    max_scenes=args.max_scenes, sensor_id=args.sensor_id)
                print(f"Verified {len(report['files'])} files; {report['transferred_bytes']/1_000_000:.2f} MB transferred.")
                print((args.data_dir / "download_report.json").resolve())
            if args.radar_command in ("run", "annotate"):
                report = annotate_sequence(args.data_dir, args.sequence, output, start_scene=args.start_scene,
                    max_scenes=args.max_scenes, sensor_id=args.sensor_id, max_detections=args.max_detections,
                    include_camera=not args.no_camera, max_camera_offset_ms=args.max_camera_offset_ms)
                print(f"{report['status'].upper()}: {report['selected_scenes']} scenes, {report['radar']['rows_sampled']} detections; "
                      f"{report['remaining_scenes']} scenes remain after this selection.")
                for warning in report["data_quality_warnings"]:
                    print(f"Data quality: {warning}")
                if report["camera"] is not None:
                    print(f"Camera: {report['camera']['unique_images_valid']} valid unique images for "
                          f"{report['camera']['scenes_with_valid_images']} scenes. LiDAR: not provided by RadarScenes.")
                if report["source_hdf5_annotation_status"] != "ok":
                    print("The source HDF5 summary has diagnostics; see metadata/radar_data.h5.metadata.json.")
                print((output / "radarscenes_report.json").resolve())
            return 0
        if args.command == "datasets":
            for dataset in selected_datasets(load_manifest(args.manifest), args.datasets):
                size = sum(f["size_bytes"] for f in dataset["files"]) / 1_000_000
                print(f"{dataset['id']:12} {size:6.2f} MB  {dataset['title']}")
                print(f"  {dataset['scope']}")
            return 0
        if args.command == "download":
            for dataset in selected_datasets(load_manifest(args.manifest), args.datasets):
                files = download_dataset(dataset, args.data_dir, int(args.max_file_mb * 1_000_000))
                print(f"{dataset['id']}: {len(files)} verified files")
            return 0
        if args.command == "benchmark":
            from .benchmark import run_benchmark
            report = run_benchmark(args.data_dir, args.output, args.datasets, args.manifest,
                                   int(args.max_file_mb * 1_000_000))
            print(f"{report['status'].upper()}: {report['datasets_passed']}/{report['datasets_total']} datasets, "
                  f"{report['files_passed']}/{report['files_total']} files")
            print("Backend: paper-inspired-local; original NPL application/HCP not tested.")
            print((args.output / "benchmark_report.json").resolve())
            return 0 if report["status"] == "passed" else 1
        if args.command == "annotate":
            from .pipeline import save_annotation
            source = args.path.resolve()
            if not source.exists():
                raise FileNotFoundError(source)
            if source.is_dir() and args.output.resolve().is_relative_to(source):
                raise ValueError("Output must be outside the source directory to avoid annotating its own sidecars")
            files = sorted(p for p in source.rglob("*") if p.is_file()) if source.is_dir() else [source]
            if not files:
                raise ValueError("Source contains no files")
            for file in files:
                relative = file.relative_to(source) if source.is_dir() else Path(file.name)
                result = save_annotation(file, args.output / relative, csv_delimiter=args.csv_delimiter,
                                         csv_header=not args.csv_no_header)
                print(result["json"])
            return 0
        if args.command == "bootstrap":
            from .chat2scenario import bootstrap_chat2scenario
            print(bootstrap_chat2scenario(args.repo))
            return 0
        if args.command in {"mine", "demo"}:
            from .chat2scenario import run_chat2scenario, create_demo_tracks, FOLLOWING_SCENARIO
            from .pipeline import annotate_mining_result
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            output = args.output or Path("outputs") / f"{args.command}-{stamp}"
            if output.exists() and any(output.iterdir()):
                raise ValueError("Choose a new empty output directory for this run")
            if args.command == "demo":
                tracks = create_demo_tracks(output / "input" / "synthetic_tracks.csv")
                structured = FOLLOWING_SCENARIO
            else:
                tracks = args.tracks
                structured = (json.loads(args.structured_scenario.read_text(encoding="utf-8"))
                              if args.structured_scenario else None)
            manifest = run_chat2scenario(args.repo, tracks, output / "mining",
                scenario_description=getattr(args, "scenario", None), structured_scenario=structured,
                model=getattr(args, "model", None), max_scenarios=args.max_scenarios,
                python_executable=args.python_executable, road_file=getattr(args, "road_file", None),
                synthetic=args.command == "demo")
            report = annotate_mining_result(manifest, output)
            print(f"{report['status']}: {len(report['annotations'])} annotated artifacts")
            if args.command == "demo":
                print("Synthetic highD fixture; actual upstream mining, LLM interpretation bypassed.")
            print((output / "pipeline_report.json").resolve())
            return 0
    except (OSError, ValueError, RuntimeError, ImportError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1
