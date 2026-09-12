"""Generate a SYNTHETIC highD-format fixture; this is not a public dataset."""
import argparse
import csv
from pathlib import Path


def create_demo_tracks(output: Path, frames: int = 101) -> Path:
    if frames < 3:
        raise ValueError("At least three frames are required")
    columns = ["frame", "id", "x", "y", "width", "height", "xVelocity", "yVelocity",
               "xAcceleration", "yAcceleration", "frontSightDistance", "backSightDistance",
               "dhw", "thw", "ttc", "precedingXVelocity", "precedingId", "followingId",
               "leftPrecedingId", "leftAlongsideId", "leftFollowingId", "rightPrecedingId",
               "rightAlongsideId", "rightFollowingId", "laneId"]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for actor in (1, 2):
            for frame in range(1, frames + 1):
                row = dict.fromkeys(columns, 0)
                row.update(frame=frame, id=actor, x=10 * (frame - 1) / 25 + (actor - 1) * 25,
                           y=8, width=4.5, height=1.8, xVelocity=10, laneId=3,
                           frontSightDistance=100, backSightDistance=100)
                if actor == 1:
                    row.update(precedingId=2, precedingXVelocity=10, dhw=20.5, thw=2.05)
                else:
                    row.update(followingId=1)
                writer.writerow(row)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(create_demo_tracks(args.output).resolve())
