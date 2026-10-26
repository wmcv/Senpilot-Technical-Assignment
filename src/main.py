"""Offline request parsing and independent UARB retrieval CLI."""

import argparse
import os
import signal
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from src.agent import Agent, format_summary, run_polling
from src.config import Config
from src.email_client import EmailClient, GmailError
from src.models import DocumentType, ProcessingResult
from src.packager import Packager
from src.parser import OpenRouterFallback, configure_interpretation_logging, interpret_request
from src.uarb import UARBClient


@contextmanager
def _graceful_termination():
    """Unwind normal cleanup on SIGTERM, including during redeployment."""
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _retrieve(args: argparse.Namespace) -> None:
    with UARBClient(headless=not args.headed) as client:
        metadata = client.get_matter(args.matter.upper())
        if not args.zip:
            print(f"Matter: {metadata.matter_id}\nTitle: {metadata.title or 'Unknown'}")
            for label, value in (
                ("Status", metadata.status),
                ("Category", metadata.category),
                ("Type", metadata.matter_type),
                ("Outcome", metadata.outcome),
            ):
                print(f"{label}: {value or 'Unknown'}")
            for label, value in metadata.filing_dates.items():
                print(f"{label}: {value.isoformat()}")
            for category, count in metadata.counts.items():
                print(f"{category.value}: {count}")
        args.output.mkdir(parents=True, exist_ok=True)
        workspace = (
            TemporaryDirectory(prefix=f"{metadata.matter_id}-", dir=args.output)
            if args.zip
            else nullcontext(str(args.output))
        )
        with workspace as temporary:
            result = client.download_documents(
                metadata.matter_id,
                DocumentType(args.type),
                Path(temporary),
                limit=args.limit,
            )
            archive = (
                Packager().package(
                    result.files,
                    args.output / f"{Path(temporary).name}.zip",
                )
                if args.zip and result.files
                else None
            )
            for path in result.files:
                print(f"Downloaded file: {path.name}" if args.zip else f"Saved: {path}")
            for error in result.errors:
                print(f"Failed [{error.document}]: {error.error}", file=sys.stderr)
            print(f"Downloaded: {len(result.files)}")
            if args.zip:
                processing = ProcessingResult(
                    len(result.files),
                    metadata.counts[DocumentType(args.type)],
                    metadata,
                    result.errors,
                )
                print(
                    format_summary(
                        processing,
                        DocumentType(args.type),
                        archive_attached=False,
                    )
                )
                print(f"ZIP: {archive}" if archive else "ZIP: none")
        if result.errors:
            raise SystemExit(1)


def _gmail(args: argparse.Namespace) -> None:
    config = Config.from_env()
    email = EmailClient(config)
    account = email.authenticate(interactive=args.gmail_auth)
    if args.gmail_auth:
        print(f"Gmail authenticated and mailbox verified: {account}")
        return
    if args.gmail_check:
        print(
            f"Gmail read check succeeded: {len(email.receive())} new unprocessed messages. "
            "No mail sent."
        )
        return
    interval = config.validate_gmail()
    fallback = OpenRouterFallback(config)
    if not fallback.enabled:
        print(
            "OpenRouter fallback disabled: configure both OPENROUTER_API_KEY and "
            "OPENROUTER_MODEL. Deterministic clarification remains available.",
            file=sys.stderr,
        )
    with UARBClient(headless=not args.headed) as client:
        count = run_polling(
            Agent(client, Packager(), email, store=email.store, fallback=fallback),
            args.output,
            interval,
            once=args.poll_once,
        )
    if args.poll_once:
        print(f"Polling completed: {count} requests handled.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a request or retrieve UARB documents")
    parser.add_argument("request", nargs="?", help='For example: "M12205 Other Documents"')
    parser.add_argument("--matter", help="Matter ID, e.g. M12205")
    parser.add_argument("--type", choices=[category.value for category in DocumentType])
    parser.add_argument("--limit", type=int, default=10, choices=range(0, 11), metavar="0..10")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("SENPILOT_OUTPUT_DIR", "downloads")),
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser")
    parser.add_argument(
        "--zip", action="store_true", help="Create a ZIP and print an email summary"
    )
    gmail = parser.add_mutually_exclusive_group()
    gmail.add_argument("--gmail-auth", action="store_true", help="Authorize Gmail without sending")
    gmail.add_argument(
        "--gmail-check", action="store_true", help="List new mail counts without sending"
    )
    gmail.add_argument("--poll", action="store_true", help="Run the serial Gmail worker")
    gmail.add_argument(
        "--poll-once", action="store_true", help="Process one batch of new Gmail requests"
    )
    args = parser.parse_args()
    if args.gmail_auth or args.gmail_check or args.poll or args.poll_once:
        configure_interpretation_logging()
        if args.request or args.matter or args.type or args.zip:
            parser.error("Gmail commands cannot be combined with retrieval or parsing arguments")
        try:
            with _graceful_termination():
                _gmail(args)
        except KeyboardInterrupt:
            print("Gmail worker stopped.")
        except GmailError as error:
            parser.exit(1, f"{error}\n")
        except Exception:
            parser.exit(
                1, "Gmail command failed; check configuration, authorization, or local state.\n"
            )
        return
    if args.zip and not (args.matter and args.type):
        parser.error("--zip requires --matter and --type")
    if args.matter or args.type:
        if not args.matter or not args.type or args.request:
            parser.error("retrieval requires --matter and --type, without a positional request")
        try:
            _retrieve(args)
        except Exception as error:
            parser.exit(1, f"UARB retrieval failed: {error}\n")
        return
    if not args.request:
        parser.error("provide a request or --matter and --type")
    configure_interpretation_logging()
    result = interpret_request(args.request)
    if result.missing_fields:
        print(result.clarification)
    else:
        request = result.require_request()
        print(f"Matter: {request.matter_id}\nDocument type: {request.document_type.value}")


if __name__ == "__main__":
    main()
