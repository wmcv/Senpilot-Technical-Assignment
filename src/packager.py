"""Verified ZIP creation from an explicit list of successful downloads."""

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from src.models import DocumentError


@dataclass(frozen=True)
class ZipBatch:
    archive: Path
    filenames: tuple[str, ...]


class Packager:
    @staticmethod
    def validate(source: Path) -> None:
        if not source.is_file():
            raise FileNotFoundError("download source is not a file")
        if not source.stat().st_size:
            raise ValueError("download source is empty")
        if source.name.startswith(".download-") or source.suffix.lower() in {
            ".part",
            ".crdownload",
            ".tmp",
        }:
            raise ValueError("incomplete download source")
        if source.suffix.lower() == ".pdf":
            with source.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("invalid PDF signature")

    def batches(
        self,
        files: tuple[Path, ...],
        directory: Path,
        fits: Callable[[Path], bool],
    ) -> tuple[tuple[ZipBatch, ...], tuple[DocumentError, ...]]:
        """Greedy displayed-order batches, measured using the caller's MIME envelope."""
        directory.mkdir(parents=True, exist_ok=True)
        batches, errors = [], []
        current: tuple[Path, ...] = ()
        # Probe filename is longer than every published filename (at most ten parts).
        candidate = directory / "documents-candidate.zip"

        def fits_files(sources):
            candidate.unlink(missing_ok=True)
            self.package(sources, candidate)
            return fits(candidate)

        def publish(sources):
            path = directory / f"documents-{len(batches) + 1}.zip"
            self.package(sources, path)
            batches.append(ZipBatch(path, tuple(source.name for source in sources)))

        try:
            for source in files:
                try:
                    self.validate(source)
                except (ValueError, OSError) as error:
                    # Validation errors contain only fixed descriptions, never paths.
                    reason = str(error) if isinstance(error, ValueError) else "source unavailable"
                    errors.append(DocumentError(source.name, reason))
                    continue
                if fits_files((*current, source)):
                    current = (*current, source)
                    continue
                if current:
                    publish(current)
                    current = ()
                if fits_files((source,)):
                    current = (source,)
                else:
                    errors.append(DocumentError(source.name, "exceeds safe encoded message limit"))
            if current:
                publish(current)
        finally:
            candidate.unlink(missing_ok=True)
        return tuple(batches), tuple(errors)

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
            self.validate(source)
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
