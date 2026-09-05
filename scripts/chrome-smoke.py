#!/usr/bin/env python3
"""Real-Chrome smoke for a built hems-api image (offline, no HEMS device access).

Run inside the image under test via ``scripts/smoke-image.sh``:

    docker run --rm --init --network none --security-opt seccomp=unconfined \
      --shm-size 256m -v <this file>:/tmp/hems-image-smoke.py:ro \
      --entrypoint python3 <image-ref> /tmp/hems-image-smoke.py

The mount target must NOT contain "chrome"/"chromium": ``hems_runtime`` treats
any process whose comm/cmdline matches those tokens as a residual browser
process, so a "chrome" in this script's own cmdline would fail the clean check.

The checks deliberately call the production code paths instead of
reimplementing them, so a runtime regression fails the smoke directly:

1. ``/usr/local/share/hems-api/chrome-build-info`` matches the installed
   binaries.
2. ``HEMSController(...)`` construction -- Chrome launch plus the CDP
   ``Page.addScriptToEvaluateOnNewDocument`` patch. Detects issue #299
   pattern 2 (``--disable-crashpad-for-testing`` killing the CDP session).
3. ``SeleniumRuntime._assert_descendants_in_group`` against the live Chrome
   process tree. Detects issue #299 pattern 1 (a ``chrome_crashpad_handler``
   escaping the worker tree/PGID in a way the runtime does not tolerate).
4. ``SeleniumRuntime._assert_container_is_clean`` within the runtime's own
   recovery deadline after ``close()``.

``login()`` / ``navigate_to_smart_airs()`` are never called: the smoke reaches
no HEMS device and uses dummy credentials only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time

APP_DIR = os.environ.get("HEMS_APP_DIR", "/app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

BUILD_INFO_PATH = "/usr/local/share/hems-api/chrome-build-info"
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "/usr/local/bin/chromedriver")
CHROME_PATH = "/usr/bin/google-chrome"
BUILD_INFO_KEYS = (
    "chrome_version",
    "chrome_build",
    "chromedriver_version",
    "chromedriver_url",
    "chromedriver_archive_sha256",
    "chromedriver_binary_sha256",
)
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DRIVER_URL_TEMPLATE = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    "{version}/linux64/chromedriver-linux64.zip"
)

# The smoke never reaches these; Chrome is only launched, never navigated.
DUMMY_URL = "http://127.0.0.1:9"
DUMMY_USER = "hems-smoke"
DUMMY_PASSWORD = "hems-smoke"


class SmokeError(RuntimeError):
    """A smoke check failed. Always fatal; the image must not be pushed."""


def _run(argv: list[str]) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise SmokeError(f"cannot run {argv!r}: {error}") from error
    return completed.stdout.strip()


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise SmokeError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def check_build_info() -> dict[str, str]:
    """Verify chrome-build-info against the binaries actually installed."""
    try:
        with open(BUILD_INFO_PATH, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        raise SmokeError(f"cannot read {BUILD_INFO_PATH}: {error}") from error

    if len(lines) != len(BUILD_INFO_KEYS):
        raise SmokeError(f"{BUILD_INFO_PATH} has {len(lines)} lines, expected {len(BUILD_INFO_KEYS)}")

    info: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not value.strip():
            raise SmokeError(f"malformed chrome-build-info line: {line!r}")
        if key in info:
            raise SmokeError(f"duplicate chrome-build-info key: {key}")
        info[key] = value
    missing = [key for key in BUILD_INFO_KEYS if key not in info]
    if missing:
        raise SmokeError(f"chrome-build-info is missing fields: {', '.join(missing)}")

    for key in ("chrome_version", "chromedriver_version"):
        if not VERSION_RE.match(info[key]):
            raise SmokeError(f"{key}={info[key]!r} is not a four-part version")
    for key in ("chromedriver_archive_sha256", "chromedriver_binary_sha256"):
        if not SHA256_RE.match(info[key]):
            raise SmokeError(f"{key}={info[key]!r} is not a SHA-256 digest")

    chrome_version = _run([CHROME_PATH, "--product-version"])
    if chrome_version != info["chrome_version"]:
        raise SmokeError(
            f"installed Chrome {chrome_version!r} does not match chrome_build_info {info['chrome_version']!r}"
        )
    driver_version = _run([CHROMEDRIVER_PATH, "--version"]).split()[1]
    if driver_version != info["chromedriver_version"]:
        raise SmokeError(
            f"installed ChromeDriver {driver_version!r} does not match "
            f"chrome_build_info {info['chromedriver_version']!r}"
        )

    chrome_build = info["chrome_version"].rsplit(".", 1)[0]
    driver_build = info["chromedriver_version"].rsplit(".", 1)[0]
    if info["chrome_build"] != chrome_build or info["chrome_build"] != driver_build:
        raise SmokeError(
            f"chrome_build={info['chrome_build']!r} does not match Chrome {chrome_build!r} "
            f"and ChromeDriver {driver_build!r}"
        )

    expected_url = DRIVER_URL_TEMPLATE.format(version=info["chromedriver_version"])
    if info["chromedriver_url"] != expected_url:
        raise SmokeError(f"chromedriver_url={info['chromedriver_url']!r} does not match {expected_url!r}")

    binary_sha256 = _sha256(CHROMEDRIVER_PATH)
    if binary_sha256 != info["chromedriver_binary_sha256"]:
        raise SmokeError(
            f"{CHROMEDRIVER_PATH} SHA-256 {binary_sha256} does not match "
            f"chrome_build_info {info['chromedriver_binary_sha256']}"
        )
    return info


def _crashpad_handlers(runtime_module) -> list[dict[str, object]]:
    return [
        {"pid": process.pid, "ppid": process.ppid, "pgrp": process.pgrp, "exe": process.exe}
        for process in runtime_module.SeleniumRuntime._list_processes()
        if process.comm == "chrome_crashpad"
    ]


def check_real_chrome() -> dict[str, object]:
    """Launch Chrome exactly like the worker does and assert the runtime invariants."""
    import hems_runtime
    from hems_control import HEMSController

    # The worker runs as its own process group leader (start_new_session=True).
    # Mirror that so _assert_descendants_in_group sees the production topology.
    if os.getpid() != os.getpgrp():
        os.setpgrp()
    pid, pgid = os.getpid(), os.getpgrp()
    if pid == 1:
        raise SmokeError(
            "smoke is PID 1; run the container with --init so a detached "
            "chrome_crashpad_handler reparents outside the smoke process tree"
        )

    started = time.monotonic()
    controller = HEMSController(DUMMY_URL, DUMMY_USER, DUMMY_PASSWORD, headless=True)
    launch_seconds = round(time.monotonic() - started, 3)
    try:
        hems_runtime.SeleniumRuntime._assert_descendants_in_group(pid, pgid)
        handlers = _crashpad_handlers(hems_runtime)
    finally:
        controller.close()

    runtime = hems_runtime.SeleniumRuntime()
    clean_started = time.monotonic()
    if not runtime._wait_for_container_clean(clean_started + hems_runtime.RECOVERY_SECONDS):
        runtime._assert_container_is_clean()
        raise SmokeError("container did not become clean within the runtime recovery deadline")
    return {
        "chrome_launch_seconds": launch_seconds,
        "container_clean_seconds": round(time.monotonic() - clean_started, 3),
        "crashpad_handlers": handlers,
    }


def main() -> int:
    try:
        info = check_build_info()
        chrome = check_real_chrome()
    except Exception as error:
        print(f"FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    summary = {
        "result": "pass",
        "chrome_version": info["chrome_version"],
        "chromedriver_version": info["chromedriver_version"],
        "chromedriver_binary_sha256": info["chromedriver_binary_sha256"],
        **chrome,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
