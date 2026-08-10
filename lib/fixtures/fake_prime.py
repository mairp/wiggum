#!/usr/bin/env python3
"""Deterministic fake Prime process controls for integration tests."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

FIXTURE_DIR = Path(__file__).with_name("prime-v3")
LAUNCHER = Path(__file__).with_name("fake-prime-launcher.sh")


def fixture_path(name: str) -> Path:
    """Return a fixture path while preventing traversal outside prime-v3."""
    candidate = (FIXTURE_DIR / name).resolve()
    if candidate.parent != FIXTURE_DIR.resolve():
        raise ValueError(f"fixture must be a basename under {FIXTURE_DIR}")
    return candidate


def stock_command(extra_args: Sequence[str] = ()) -> list[str]:
    """Build a stock prime-agent-compatible fake command."""
    return [str(LAUNCHER), *extra_args]


def fleet_command(variant: str, extra_args: Sequence[str] = ()) -> list[str]:
    """Build a `prime <variant>`-compatible fake command."""
    if not variant or variant.startswith("-") or "/" in variant:
        raise ValueError("variant must be a non-option path-safe name")
    return [str(LAUNCHER), variant, *extra_args]


def control_env(
    fixture: str = "stock-success.jsonl",
    *,
    exit_code: int = 0,
    delay_seconds: float = 0,
    signal_name: str | None = None,
    capture_dir: Path | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return environment controls for output, timeout, signal, and status tests."""
    fixture_path(fixture)
    env = dict(base or os.environ)
    env.update(
        {
            "FAKE_PRIME_FIXTURE": fixture,
            "FAKE_PRIME_EXIT_CODE": str(exit_code),
            "FAKE_PRIME_DELAY_SECONDS": str(delay_seconds),
        }
    )
    if signal_name is None:
        env.pop("FAKE_PRIME_SIGNAL", None)
    else:
        parse_signal(signal_name)
        env["FAKE_PRIME_SIGNAL"] = signal_name
    if capture_dir is not None:
        env["FAKE_PRIME_CAPTURE_DIR"] = str(capture_dir)
    return env


def parse_signal(value: str) -> signal.Signals:
    name = value.upper()
    if not name.startswith("SIG"):
        name = f"SIG{name}"
    member = getattr(signal, name, None)
    if not isinstance(member, signal.Signals):
        raise ValueError(f"unsupported signal: {value}")
    return member


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=os.getenv("FAKE_PRIME_FIXTURE", "stock-success.jsonl"))
    parser.add_argument("--exit-code", type=int, default=int(os.getenv("FAKE_PRIME_EXIT_CODE", "0")))
    parser.add_argument("--delay-seconds", type=float, default=float(os.getenv("FAKE_PRIME_DELAY_SECONDS", "0")))
    parser.add_argument("--signal", default=os.getenv("FAKE_PRIME_SIGNAL"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.delay_seconds < 0:
        raise SystemExit("--delay-seconds must be non-negative")
    if not 0 <= args.exit_code <= 255:
        raise SystemExit("--exit-code must be between 0 and 255")

    stream = fixture_path(args.fixture).read_bytes()
    sys.stdout.buffer.write(stream)
    sys.stdout.buffer.flush()
    if args.delay_seconds:
        time.sleep(args.delay_seconds)
    if args.signal:
        os.kill(os.getpid(), parse_signal(args.signal))
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
