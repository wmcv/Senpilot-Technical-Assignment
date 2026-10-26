"""Verified ZIP creation from an explicit list of successful downloads."""

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile


class Packager:
    def package(self, files: tuple[Path, ...], destination: Path) -> Path:
        """Publish a verified ZIP without overwriting an existing archive.

        Empty input is rejected; callers should omit the attachment instead.
        No directory scanning is performed, so unrelated downloads are excluded.
        """
        if not files:
            raise ValueError("cannot package zero files; omit the ZIP attachment")
        entries = []
        used: set[str] = set()
        originals = {source.name.casefold() for source in files}
        for source in files:
            if not source.is_file():
                raise FileNotFoundError(f"download source is not a file: {source}")
            if source.stat().st_size == 0:
                raise ValueError(f"download source is empty: {source}")
            if source.name.startswith(".download-") or source.suffix.lower() in {
                ".part",
                ".crdownload",
                ".tmp",
            }:
                raise ValueError(f"incomplete download source: {source}")
            if source.resolve() == destination.resolve():
                raise ValueError("archive destination cannot be a source file")
            name, index = source.name, 0
            while name.casefold() in used or (index > 0 and name.casefold() in originals):
                index += 1
                name = f"{source.stem} ({index}){source.suffix}"
            used.add(name.casefold())
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").digest()
            entries.append((source, name, digest))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=destination.parent, prefix=".zip-", delete=False) as stream:
            staging = Path(stream.name)
        try:
            with ZipFile(staging, "w", compression=ZIP_DEFLATED) as archive:
                for source, name, _ in entries:
                    archive.write(source, arcname=name)
            with ZipFile(staging) as archive:
                if archive.namelist() != [name for _, name, _ in entries] or archive.testzip():
                    raise ValueError("ZIP integrity or member list verification failed")
                for _, name, expected in entries:
                    with archive.open(name) as stream:
                        if hashlib.file_digest(stream, "sha256").digest() != expected:
                            raise ValueError(f"ZIP content verification failed: {name}")
            # Same-directory hard link publishes the complete archive atomically
            # and refuses to replace a previous request's output.
            os.link(staging, destination)
            return destination
        finally:
            staging.unlink(missing_ok=True)
