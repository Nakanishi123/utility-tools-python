import argparse
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=str, help="フォルダのパス")

    args = ap.parse_args()
    folder_path = Path(args.folder).expanduser().resolve()
    if not folder_path.is_dir():
        raise NotADirectoryError(str(folder_path))

    for file in folder_path.iterdir():
        if not file.is_file():
            continue
        print(f"```{file.name}")
        print(file.read_text(encoding="utf-8"))
        print("```\n")


if __name__ == "__main__":
    main()
