"""Resolve a hand-pasted download folder path.

The page normalizes paths itself -- quotes, `file://`, `~`, separators -- because
validating through a subprocess made saving take seconds. Environment variables
are the one thing it cannot expand, so `%USERPROFILE%`-style paths come here, to
the same `download.clean_dir` that decides where a file actually lands.
"""

import os

import download


def main(dest_dir: str = "", create: str = ""):
    folder = download.clean_dir(dest_dir) or download.DOWNLOAD_DIR
    if create:
        # the "Open folder" button views this in the explorer, so make it real
        # first -- an empty folder reads better than the explorer's not-found page
        os.makedirs(folder, exist_ok=True)
    return {"dir": folder.replace("\\", "/")}


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(main(dest_dir=sys.argv[1] if len(sys.argv) > 1 else "")))
