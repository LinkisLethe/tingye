"""Create a source release without user settings, notes, models or test outputs."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parent
patterns = ["app/*.py", "app/static/*", "tests/*.py", "docs/*", "*.md", "requirements*.txt", "launch.ps1", "启动听页.vbs", ".gitignore", ".gitattributes", "package_release.py"]
paths = sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})
destination = root / "听页-0.2.0.zip"
with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
    for path in paths:
        archive.write(path, "tingye/" + path.relative_to(root).as_posix())
print(destination)
print(f"{len(paths)} files, {destination.stat().st_size} bytes")
