"""Synchronous Playwright adapter for the public FileMaker WebDirect database."""

import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
    Locator,
    Page,
    Playwright,
    expect,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from src.models import (
    DocumentError,
    DocumentType,
    DownloadResult,
    MatterMetadata,
    validate_document_type,
    validate_matter_id,
)

URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
CATEGORY_LABELS = {category: category.value for category in DocumentType}
CATEGORY_LABELS[DocumentType.RECORDINGS] = "(?:Recordings|Audio Files)"


def _save_download(download: Download, destination: Path) -> Path:
    """Validate before publishing; exclusive creation prevents collision overwrites."""
    name = download.suggested_filename
    if not name or name in {".", ".."} or any(c in name for c in "/\\\x00"):
        raise ValueError("unsafe filename returned by server")
    destination.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination, prefix=".download-", delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        download.save_as(staging)
        failure = download.failure()
        if failure:
            raise OSError(f"browser download failed: {failure}")
        if staging.stat().st_size == 0:
            raise ValueError("downloaded file is empty")
        with staging.open("rb") as source:
            if Path(name).suffix.lower() == ".pdf" and source.read(5) != b"%PDF-":
                raise ValueError("downloaded PDF has an invalid signature")
        original = Path(name)
        index = 0
        while True:
            candidate = destination / (
                name if index == 0 else f"{original.stem} ({index}){original.suffix}"
            )
            try:
                target = candidate.open("xb")
            except FileExistsError:
                index += 1
                continue
            try:
                with target, staging.open("rb") as source:
                    shutil.copyfileobj(source, target)
            except BaseException:
                candidate.unlink(missing_ok=True)
                raise
            return candidate
    finally:
        staging.unlink(missing_ok=True)


class UARBClient:
    """One lazy browser session per client. Use as a context manager or call close()."""

    def __init__(self, *, headless: bool = True, timeout_ms: int = 30000) -> None:
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._selected = False
        self._metadata: MatterMetadata | None = None

    def __enter__(self) -> "UARBClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None
            self._metadata = None

    def _session(self) -> Page:
        if self._page is None:
            try:
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(headless=self.headless)
                self._context = self._browser.new_context(
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 1000},
                )
                self._page = self._context.new_page()
                self._page.set_default_timeout(self.timeout_ms)
            except Exception:
                self.close()
                raise
        return self._page

    def get_matter(self, matter_id: str) -> MatterMetadata:
        validate_matter_id(matter_id)
        page = self._session()
        self._metadata = None
        self._selected = False
        page.goto(URL, wait_until="domcontentloaded")
        search = (
            page.locator(".fm-textarea")
            .filter(
                has=page.get_by_text("eg M01234", exact=True),
            )
            .locator(".text")
        )
        search.click()
        page.locator('[contenteditable="true"]').fill(matter_id)
        page.locator('[contenteditable="true"]').press("Tab")
        # FileMaker object numbers were inspected on the live search layout.
        page.locator('button[id^="b0p0o258i0"]').click()
        matter = page.locator('[id^="b0p0o286i0"] .text')
        not_found = page.get_by_role("dialog", name="No Records Found", exact=True)
        expect(matter.or_(not_found)).to_be_visible(timeout=self.timeout_ms)
        if not_found.count():
            raise LookupError(f"No UARB matter found for {matter_id}")
        expect(matter).to_have_text(matter_id, timeout=self.timeout_ms)

        def field(number: int) -> str | None:
            value = page.locator(f'[id^="b0p0o{number}i0"]').inner_text().strip()
            return value or None

        dates = {}
        for number, label in ((292, "Date Received"), (294, "Decision Date")):
            value = field(number)
            if value:
                dates[label] = datetime.strptime(value, "%m/%d/%Y").date()
        counts = {}
        for category in DocumentType:
            text = self._category_button(page, category).inner_text()
            counts[category] = int(text.rsplit("-", 1)[1].strip().replace(",", ""))
        self._metadata = MatterMetadata(
            matter_id=matter_id,
            title=field(290),
            status=field(289),
            category=field(298),
            matter_type=field(287),
            filing_dates=dates,
            outcome=field(295),
            counts=counts,
        )
        return self._metadata

    @staticmethod
    def _category_button(page: Page, category: DocumentType) -> Locator:
        return page.get_by_role(
            "button",
            name=re.compile(rf"^{CATEGORY_LABELS[category]}\s*-\s*[\d,]+$"),
        )

    def _select_category(self, category: DocumentType, count: int) -> None:
        page = self._session()
        self._category_button(page, category).click()
        # The server replaces the matter layout with a virtualized document grid.
        # Wait for its actual row count, rather than reading a stale previous grid.
        page.locator(f'table[role="grid"][aria-rowcount="{count}"]').wait_for(state="attached")
        page.locator('.iwps_body_pid_1[id*="o0i1i0"]').wait_for()
        self._selected = True

    def _row(self, index: int, count: int) -> Locator:
        page = self._session()
        scroller = page.locator(".v-grid-scroller-vertical")
        # WebDirect materializes only nearby rows; scrolling by row height makes
        # later documents available even when earlier downloads fail.
        scroller.evaluate(
            "(e, args) => e.scrollTop = (args.index - 1) * e.scrollHeight / args.count",
            {"index": index, "count": count},
        )
        row = page.locator(f'.iwps_body_pid_1[id*="o0i{index}i0"]')
        row.wait_for(state="attached")
        return row

    def _download_one(self, row: Locator, destination: Path) -> Path:
        page = self._session()
        # The observed site uses an in-page Download Files dialog. A page event
        # also handles installations that show that dialog in a separate window.
        popups: list[Page] = []

        def on_popup(popup: Page) -> None:
            popups.append(popup)

        page.on("popup", on_popup)
        window = page
        try:
            row.get_by_role("button", name="GO GET IT", exact=True).click()
            deadline = time.monotonic() + self.timeout_ms / 1000
            while True:
                window = popups[0] if popups else page
                button = window.locator(".fm-download-button").first
                try:
                    button.wait_for(timeout=min(250, self.timeout_ms))
                    break
                except PlaywrightTimeoutError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Download Files dialog did not open") from None
            with window.expect_download(timeout=self.timeout_ms) as event:
                button.click()
            return _save_download(event.value, destination)
        finally:
            page.remove_listener("popup", on_popup)
            for popup in popups:
                if not popup.is_closed():
                    popup.close()
            dialog = page.get_by_role("dialog", name="Download Files")
            if dialog.count():
                dialog.get_by_role("button", name="Close", exact=True).click()
                dialog.wait_for(state="hidden")

    def download_documents(
        self,
        matter_id: str,
        document_type: DocumentType,
        destination: Path,
        *,
        limit: int = 10,
    ) -> DownloadResult:
        validate_matter_id(matter_id)
        validate_document_type(document_type)
        if document_type is None:
            raise ValueError("document_type is required")
        if type(limit) is not int or not 0 <= limit <= 10:
            raise ValueError("limit must be an integer between zero and ten")
        if limit == 0:
            return DownloadResult()
        metadata = self._metadata
        if metadata is None or metadata.matter_id != matter_id or self._selected:
            metadata = self.get_matter(matter_id)
        count = metadata.counts[document_type]
        if count == 0:
            return DownloadResult()
        self._select_category(document_type, count)
        files, errors = [], []
        for index in range(1, count + 1):
            identifier = f"{document_type.value} row {index}"
            for attempt in range(2):
                try:
                    row = self._row(index, count)
                    identifier = row.inner_text().strip().replace("\n", " ")
                    files.append(self._download_one(row, destination))
                    break
                except Exception as error:
                    if attempt == 1:
                        errors.append(DocumentError(identifier, str(error)))
            if len(files) >= limit:
                break
        return DownloadResult(tuple(files), tuple(errors))
