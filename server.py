#!/usr/bin/env python3
"""Canon EOS live-view web server with modern UI."""

import argparse
import json
import math
import os
import platform
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import quote

import cv2
import numpy as np
from flask import Flask, render_template, jsonify, request, send_from_directory, abort
from flask_cors import CORS


@dataclass
class WhiteBalanceOption:
    index: int
    label: str


@dataclass
class WhiteBalanceState:
    path: str
    options: list[WhiteBalanceOption]
    current_index: Optional[int]


@dataclass
class CameraMetadata:
    battery_level: Optional[int] = None
    shutter_speed: Optional[str] = None
    iso: Optional[str] = None
    exposure_compensation: float = 0.0
    white_balance: str = "Unknown"
    drive_mode: Optional[str] = None
    self_timer_seconds: Optional[int] = None
    preview_enabled: bool = True
    status: str = "Ready"
    reconnects: int = 0
    preview_frame_timestamp: float = 0.0


app = Flask(__name__)
CORS(app)

WB_STATE: Optional[WhiteBalanceState] = None
TIMER_CONFIG_PATHS: list[str] = []
LAST_FRAME: Optional[np.ndarray] = None
LAST_FRAME_AT: float = 0.0
CAMERA_METADATA = CameraMetadata()
CAMERA_CMD_LOCK = threading.Lock()
COMMAND_CONDITION = threading.Condition()
COMMAND_IN_FLIGHT = False
PENDING_SHUTTER_COMMANDS = 0
STREAM_PAUSE_EVENT = threading.Event()  # Set when stream should pause
STREAM_PAUSE_EVENT.set()  # Start in "not paused" state
ACTIVE_STREAM_LOCK = threading.Lock()
CAPTURE_DIR = "captures"
PREVIEW_ENABLED = True

MJPEG_BOUNDARY = b"--frame"
MJPEG_HEADERS = (
    b"Content-Type: image/jpeg\r\n"
    b"Content-Length: "
)

READ_CHUNK_SIZE = 65536
MAX_JPEG_BUFFER = 10_000_000
NO_CAMERA_RETRY_DELAY = 5.0
STREAM_RETRY_DELAY = 1.0
PASSIVE_STREAM_FRAME_DELAY = 0.25
PASSIVE_STREAM_STALE_AFTER = 4.0
METADATA_REFRESH_INTERVAL = 5.0

METADATA_REFRESH_THREAD: Optional[threading.Thread] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canon EOS web preview server.")
    parser.add_argument(
        "--camera-model",
        default="Canon EOS 600D",
        help="Camera model substring.",
    )
    parser.add_argument(
        "--save-dir",
        default="captures",
        help="Directory where photos are saved.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Web server host.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="Web server port.",
    )
    return parser.parse_args()


def get_local_ip_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def get_server_port() -> int:
    port_value = request.environ.get("SERVER_PORT") or request.host.rsplit(":", 1)[-1]
    try:
        return int(port_value)
    except (TypeError, ValueError):
        return 8888


def get_share_url() -> str:
    return f"http://{get_local_ip_address()}:{get_server_port()}"


def ensure_gphoto2() -> None:
    if shutil.which("gphoto2") is None:
        print("Error: gphoto2 is not installed.", file=sys.stderr)
        sys.exit(1)


def run_gphoto2(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return run_gphoto2_command(args, timeout=timeout)


def run_gphoto2_command(
    args: list[str],
    timeout: int = 15,
    priority: str = "normal",
    require_exclusive_camera: bool = False,
) -> subprocess.CompletedProcess:
    global COMMAND_IN_FLIGHT, PENDING_SHUTTER_COMMANDS

    is_shutter = priority == "shutter"
    with COMMAND_CONDITION:
        if is_shutter:
            PENDING_SHUTTER_COMMANDS += 1
        while COMMAND_IN_FLIGHT or (not is_shutter and PENDING_SHUTTER_COMMANDS > 0):
            COMMAND_CONDITION.wait()
        if is_shutter:
            PENDING_SHUTTER_COMMANDS -= 1
        COMMAND_IN_FLIGHT = True

    try:
        with CAMERA_CMD_LOCK:
            if require_exclusive_camera:
                stop_live_view_processes()
            release_macos_camera_lock()
            return subprocess.run(
                ["gphoto2", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    finally:
        with COMMAND_CONDITION:
            COMMAND_IN_FLIGHT = False
            COMMAND_CONDITION.notify_all()


def is_no_camera_error(message: Optional[str]) -> bool:
    return bool(message and "No camera found" in message)


def set_camera_disconnected() -> None:
    CAMERA_METADATA.status = "Camera disconnected"
    CAMERA_METADATA.battery_level = None
    CAMERA_METADATA.shutter_speed = None
    CAMERA_METADATA.iso = None
    CAMERA_METADATA.drive_mode = None
    CAMERA_METADATA.self_timer_seconds = None


def release_macos_camera_lock(settle_seconds: float = 0.15) -> None:
    if platform.system() != "Darwin":
        return
    # Only release macOS PTPCamera lock; do not kill gphoto2 here.
    # Killing gphoto2 interrupts live-view streaming and causes /stream failures.
    for _ in range(2):
        subprocess.run(
            ["pkill", "-9", "ptpcamerad"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(settle_seconds)


def stop_live_view_processes() -> None:
    """Stop active gphoto2 live-view processes so exclusive capture can run."""
    for proc in ["gphoto2", "ptpcamerad", "PTPCamera"]:
        subprocess.run(
            ["pkill", "-9", proc],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    release_macos_camera_lock(settle_seconds=0.15)


def run_shutter_capture() -> subprocess.CompletedProcess:
    """Run shutter capture with retry for transient USB-claim conflicts."""
    global STREAM_PAUSE_EVENT
    
    # Pause the stream to avoid USB contention.
    print("[Shutter] Pausing stream for capture", file=sys.stderr)
    STREAM_PAUSE_EVENT.clear()
    time.sleep(0.3)
    
    last_result: Optional[subprocess.CompletedProcess] = None
    try:
        for attempt in range(3):
            result = run_gphoto2_command(
                [
                    "--capture-image-and-download",
                    "--filename",
                    os.path.join(CAPTURE_DIR, "IMG_%Y%m%d_%H%M%S.%C"),
                    "--force-overwrite",
                ],
                timeout=45,
                priority="shutter",
                require_exclusive_camera=True,
            )
            last_result = result
            if result.returncode == 0:
                print("[Shutter] Capture successful", file=sys.stderr)
                return result

            stderr_text = (result.stderr or result.stdout or "")
            if "Could not claim the USB device" not in stderr_text:
                break

            print(f"[Shutter] USB claim failed, retrying ({attempt + 1}/3)", file=sys.stderr)
            time.sleep(0.4)

        return last_result if last_result is not None else subprocess.CompletedProcess([], 1)
    finally:
        # Resume the stream
        print("[Shutter] Resuming stream after capture", file=sys.stderr)
        if PREVIEW_ENABLED:
            STREAM_PAUSE_EVENT.set()


def detect_camera(expected_model: str) -> bool:
    try:
        result = run_gphoto2(["--auto-detect"])
    except subprocess.TimeoutExpired:
        print("Error: Timed out during camera detection.", file=sys.stderr)
        set_camera_disconnected()
        return False

    if result.returncode != 0:
        print("Error: Failed to detect camera.", file=sys.stderr)
        set_camera_disconnected()
        return False

    if expected_model.lower() not in result.stdout.lower():
        print(f"Warning: '{expected_model}' not found.", file=sys.stderr)
    return True


def parse_wb_choices(
    config_output: str,
) -> tuple[list[WhiteBalanceOption], Optional[int]]:
    options: list[WhiteBalanceOption] = []
    current_label: Optional[str] = None

    for line in config_output.splitlines():
        choice_match = re.match(r"Choice:\s*(\d+)\s*(.+)", line.strip())
        if choice_match:
            options.append(
                WhiteBalanceOption(
                    index=int(choice_match.group(1)),
                    label=choice_match.group(2).strip(),
                )
            )
            continue

        current_match = re.match(r"Current:\s*(.+)", line.strip())
        if current_match:
            current_label = current_match.group(1).strip()

    current_index: Optional[int] = None
    if current_label is not None:
        for option in options:
            if option.label.lower() == current_label.lower():
                current_index = option.index
                break

    return options, current_index


def discover_white_balance() -> Optional[WhiteBalanceState]:
    try:
        result = run_gphoto2(["--list-config"])
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    candidates: list[str] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if "whitebalance" not in path.lower():
            continue
        if "adjust" in path.lower() or "color" in path.lower():
            continue
        candidates.append(path)

    fallback_paths = [
        "/main/imgsettings/whitebalance",
        "/main/settings/whitebalance",
        "/main/capturesettings/whitebalance",
    ]
    for fallback in fallback_paths:
        if fallback not in candidates:
            candidates.append(fallback)

    for path in candidates:
        try:
            config = run_gphoto2(["--get-config", path])
        except subprocess.TimeoutExpired:
            continue

        if config.returncode != 0:
            continue

        options, current_index = parse_wb_choices(config.stdout)
        if not options:
            continue

        return WhiteBalanceState(path=path, options=options, current_index=current_index)

    return None


def discover_timer_config_paths() -> list[str]:
    try:
        result = run_gphoto2(["--list-config"])
    except subprocess.TimeoutExpired:
        return []

    if result.returncode != 0:
        return []

    candidates: list[str] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        lower_path = path.lower()
        if "drivemode" in lower_path or "selftimer" in lower_path or "capturemode" in lower_path:
            candidates.append(path)

    fallback_paths = [
        "/main/capturesettings/drivemode",
        "/main/imgsettings/drivemode",
        "/main/settings/drivemode",
        "/main/capturesettings/capturemode",
        "/main/imgsettings/capturemode",
        "/main/settings/selftimer",
        "/main/capturesettings/selftimer",
        "/main/imgsettings/selftimer",
        "/main/capturesettings/delay",
        "/main/settings/delay",
        "/main/capturesettings/drive",
    ]
    for fallback in fallback_paths:
        if fallback not in candidates:
            candidates.append(fallback)

    return candidates


def parse_config_current_value(config_output: str) -> Optional[str]:
    for line in config_output.splitlines():
        match = re.match(r"Current:\s*(.+)", line.strip())
        if match:
            return match.group(1).strip()
    return None


def parse_config_choices(config_output: str) -> tuple[list[tuple[int, str]], Optional[str]]:
    choices: list[tuple[int, str]] = []
    current_label: Optional[str] = None

    for line in config_output.splitlines():
        choice_match = re.match(r"Choice:\s*(\d+)\s*(.+)", line.strip())
        if choice_match:
            choices.append((int(choice_match.group(1)), choice_match.group(2).strip()))
            continue

        current_match = re.match(r"Current:\s*(.+)", line.strip())
        if current_match:
            current_label = current_match.group(1).strip()

    return choices, current_label


def parse_self_timer_seconds(current_value: Optional[str]) -> Optional[int]:
    if current_value is None:
        return None

    lower_value = current_value.lower()
    match = re.search(r"(\d+)\s*(?:sec|second|s)\b", lower_value)
    if match:
        return int(match.group(1))

    match = re.search(r"(\d+)", lower_value)
    if match:
        return int(match.group(1))

    return None


def infer_self_timer_seconds(
    path: str,
    config_output: str,
) -> tuple[Optional[str], Optional[int]]:
    choices, current_label = parse_config_choices(config_output)
    path_lower = path.lower()
    is_timer_path = (
        "drivemode" in path_lower
        or "selftimer" in path_lower
        or "capturemode" in path_lower
    )

    if current_label is None and not choices:
        return None, None

    resolved_label = current_label
    if resolved_label is None and choices:
        current_value = parse_config_current_value(config_output)
        if current_value is not None and current_value.isdigit():
            for index, label in choices:
                if index == int(current_value):
                    resolved_label = label
                    break

    if resolved_label is None:
        resolved_label = parse_config_current_value(config_output)

    if resolved_label is None:
        return None, None

    timer_seconds = parse_self_timer_seconds(resolved_label)
    if timer_seconds is None and is_timer_path:
        timer_seconds = parse_self_timer_seconds(f"{path} {resolved_label}")

    return resolved_label, timer_seconds


def update_camera_metadata() -> None:
    """Fetch current camera status from gphoto2."""
    global CAMERA_METADATA, WB_STATE, TIMER_CONFIG_PATHS

    if not TIMER_CONFIG_PATHS:
        TIMER_CONFIG_PATHS = discover_timer_config_paths()

    if WB_STATE is not None and WB_STATE.current_index is not None:
        for option in WB_STATE.options:
            if option.index == WB_STATE.current_index:
                CAMERA_METADATA.white_balance = option.label
                break

    CAMERA_METADATA.drive_mode = None
    CAMERA_METADATA.self_timer_seconds = None

    for path in TIMER_CONFIG_PATHS:
        try:
            config = run_gphoto2(["--get-config", path], timeout=3)
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            continue

        if config.returncode != 0:
            continue

        drive_mode, timer_seconds = infer_self_timer_seconds(path, config.stdout)
        if drive_mode is None:
            continue

        CAMERA_METADATA.drive_mode = drive_mode
        CAMERA_METADATA.self_timer_seconds = timer_seconds
        break

    shutter_paths = [
        "/main/capturesettings/shutterspeed",
        "/main/imgsettings/shutterspeed",
        "/main/settings/shutterspeed",
    ]
    for path in shutter_paths:
        try:
            config = run_gphoto2_command(["--get-config", path], timeout=3)
        except Exception:
            continue
        if config.returncode != 0:
            continue
        value = parse_config_current_value(config.stdout)
        if value:
            CAMERA_METADATA.shutter_speed = value
            break

    iso_paths = [
        "/main/imgsettings/iso",
        "/main/capturesettings/iso",
        "/main/settings/iso",
    ]
    for path in iso_paths:
        try:
            config = run_gphoto2_command(["--get-config", path], timeout=3)
        except Exception:
            continue
        if config.returncode != 0:
            continue
        value = parse_config_current_value(config.stdout)
        if value:
            CAMERA_METADATA.iso = value
            break

    try:
        result = run_gphoto2_command(
            ["--get-config", "/main/status/batterylevel"], timeout=3
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = re.match(r"Current:\s*(\d+)", line.strip())
                if match:
                    CAMERA_METADATA.battery_level = int(match.group(1))
                    if CAMERA_METADATA.status != "Capturing":
                        CAMERA_METADATA.status = "Ready"
                        if PREVIEW_ENABLED:
                            STREAM_PAUSE_EVENT.set()
                    break
        elif is_no_camera_error(result.stderr or result.stdout):
            set_camera_disconnected()
    except Exception:
        pass


def metadata_refresh_loop() -> None:
    while True:
        try:
            if not ACTIVE_STREAM_LOCK.locked():
                update_camera_metadata()
        except Exception:
            pass
        time.sleep(METADATA_REFRESH_INTERVAL)


def disable_camera_viewfinder() -> None:
    """Disable viewfinder and movie mode on camera to save power."""
    try:
        stop_live_view_processes()
        release_macos_camera_lock()
        time.sleep(0.5)
        run_gphoto2(["--set-config", "/main/actions/viewfinder=0"], timeout=5)
        run_gphoto2(["--set-config", "/main/actions/eosmoviemode=0"], timeout=5)
        print("[Preview] Disabled camera viewfinder", file=sys.stderr)
    except Exception as e:
        print(f"[Preview] Warning: Failed to disable viewfinder: {e}", file=sys.stderr)


def enable_camera_viewfinder() -> None:
    """Enable viewfinder and movie mode on camera for live preview."""
    try:
        time.sleep(0.5)
        run_gphoto2(["--set-config", "/main/actions/viewfinder=1"], timeout=5)
        run_gphoto2(["--set-config", "/main/actions/eosmoviemode=1"], timeout=5)
        print("[Preview] Enabled camera viewfinder", file=sys.stderr)
    except Exception as e:
        print(f"[Preview] Warning: Failed to enable viewfinder: {e}", file=sys.stderr)


def start_live_view_process() -> subprocess.Popen:
    last_error = None
    for attempt in range(8):
        stop_live_view_processes()
        release_macos_camera_lock()
        
        # Extra delay to let USB settle, especially important on retries
        time.sleep(2.5 if attempt > 0 else 1.5)

        for viewfinder_enabled in [True, False]:
            args = ["--capture-movie", "--stdout"]
            if viewfinder_enabled:
                args = [
                    "--set-config",
                    "/main/actions/viewfinder=1",
                    "--set-config",
                    "/main/actions/eosmoviemode=1",
                ] + args

            try:
                process = subprocess.Popen(
                    ["gphoto2"] + args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )

                time.sleep(0.5)
                if process.poll() is None:
                    print(f"[Stream] Started gphoto2 process successfully (attempt {attempt+1})", file=sys.stderr)
                    if CAMERA_METADATA.status != "Capturing":
                        CAMERA_METADATA.status = "Ready"
                    return process
                else:
                    # Process exited immediately, get error
                    stderr = process.stderr.read().decode('utf-8', errors='ignore')
                    last_error = f"Process exited: {stderr[:200]}"
            except Exception as e:
                last_error = str(e)

    time.sleep(2.0)

    error_msg = f"Failed to start live-view stream. Last error: {last_error}"
    print(f"[Stream] {error_msg}", file=sys.stderr)
    raise RuntimeError(error_msg)


def decode_next_frame(stream, buffer: bytearray) -> Optional[np.ndarray]:
    ready, _, _ = select.select([stream], [], [], 1.0)
    if not ready:
        return np.array([])

    chunk = stream.read(READ_CHUNK_SIZE)
    if not chunk:
        return None

    buffer.extend(chunk)

    start = 0
    latest_start = -1
    latest_end = -1

    while True:
        soi = buffer.find(b"\xff\xd8", start)
        if soi == -1:
            break

        eoi = buffer.find(b"\xff\xd9", soi + 2)
        if eoi == -1:
            break

        latest_start = soi
        latest_end = eoi + 2
        start = latest_end

    if latest_start != -1:
        jpg_bytes = bytes(buffer[latest_start:latest_end])
        del buffer[:latest_end]

        frame = cv2.imdecode(np.frombuffer(jpg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is not None:
            return frame

    if len(buffer) > MAX_JPEG_BUFFER:
        tail = buffer.rfind(b"\xff\xd8")
        if tail != -1:
            del buffer[:tail]
        else:
            del buffer[:-1_000_000]

    return np.array([])


def generate_mjpeg():
    """Generator for MJPEG stream."""
    global LAST_FRAME, LAST_FRAME_AT

    print("[Stream] Starting MJPEG generator", file=sys.stderr)
    is_primary_stream = ACTIVE_STREAM_LOCK.acquire(blocking=False)
    process: Optional[subprocess.Popen] = None
    frame_count = 0
    restart_count = 0
    retry_delay = STREAM_RETRY_DELAY

    try:
        if not is_primary_stream:
            print("[Stream] Reusing active live-view owner", file=sys.stderr)
            no_frame_count = 0
            while True:
                if LAST_FRAME is not None:
                    no_frame_count = 0
                    if LAST_FRAME_AT > 0 and (time.time() - LAST_FRAME_AT) > PASSIVE_STREAM_STALE_AFTER:
                        print("[Stream] Passive stream stale, forcing reconnect", file=sys.stderr)
                        return
                    _, jpg = cv2.imencode(".jpg", LAST_FRAME, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    jpg_bytes = jpg.tobytes()
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpg_bytes)).encode() + b"\r\n\r\n"
                        + jpg_bytes + b"\r\n"
                    )
                else:
                    no_frame_count += 1
                    if no_frame_count > 40:  # ~10 seconds with PASSIVE_STREAM_FRAME_DELAY=0.25
                        print("[Stream] Passive stream has no frames after 10s, forcing reconnect", file=sys.stderr)
                        return
                time.sleep(PASSIVE_STREAM_FRAME_DELAY)

        while True:
            # Wait if stream is paused (e.g., during capture)
            if not STREAM_PAUSE_EVENT.is_set():
                print("[Stream] Paused for operation, stopping process", file=sys.stderr)
                if process is not None:
                    try:
                        process.terminate()
                        process.wait(timeout=2)
                    except Exception:
                        pass
                    process = None
                STREAM_PAUSE_EVENT.wait()  # Block until pause is cleared
                print("[Stream] Resumed after operation", file=sys.stderr)
            
            if not PREVIEW_ENABLED:
                time.sleep(PASSIVE_STREAM_FRAME_DELAY)
                continue

            try:
                process = start_live_view_process()
                stream = process.stdout
                jpeg_buffer = bytearray()
                last_frame_at = time.time()

                while True:
                    # Check if pause was requested
                    if not STREAM_PAUSE_EVENT.is_set():
                        print("[Stream] Pause requested, breaking inner loop", file=sys.stderr)
                        break
                    
                    frame = decode_next_frame(stream, jpeg_buffer)

                    if frame is None:
                        print("[Stream] Got None frame, restarting process", file=sys.stderr)
                        break

                    if frame.size > 0:
                        frame_count += 1
                        LAST_FRAME_AT = time.time()
                        CAMERA_METADATA.preview_frame_timestamp = LAST_FRAME_AT
                        if frame_count % 10 == 0:
                            print(f"[Stream] Yielding frame #{frame_count}", file=sys.stderr)
                        LAST_FRAME = frame.copy()
                        if CAMERA_METADATA.status != "Capturing":
                            CAMERA_METADATA.status = "Ready"
                        _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        jpg_bytes = jpg.tobytes()

                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(jpg_bytes)).encode() + b"\r\n\r\n"
                            + jpg_bytes + b"\r\n"
                        )
                        last_frame_at = time.time()
                    else:
                        if len(jpeg_buffer) < 100:
                            continue

                    if time.time() - last_frame_at > 5:
                        print(
                            f"[Stream] No frames for 5 seconds, restarting (restart #{restart_count + 1})",
                            file=sys.stderr,
                        )
                        break

            except Exception as exc:
                if is_no_camera_error(str(exc)):
                    set_camera_disconnected()
                    retry_delay = NO_CAMERA_RETRY_DELAY
                else:
                    retry_delay = STREAM_RETRY_DELAY
                print(f"[Stream] Restart loop error: {exc}", file=sys.stderr)
            finally:
                if process is not None:
                    try:
                        process.terminate()
                        process.wait(timeout=2)
                    except Exception:
                        pass
                    process = None

            restart_count += 1
            CAMERA_METADATA.reconnects = restart_count
            time.sleep(retry_delay)

    except GeneratorExit:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                pass
    finally:
        if is_primary_stream:
            ACTIVE_STREAM_LOCK.release()


def estimate_cct_from_bgr(bgr: np.ndarray) -> Optional[float]:
    b = float(bgr[0]) / 255.0
    g = float(bgr[1]) / 255.0
    r = float(bgr[2]) / 255.0

    if r <= 0 and g <= 0 and b <= 0:
        return None

    def srgb_to_linear(v: float) -> float:
        if v <= 0.04045:
            return v / 12.92
        return ((v + 0.055) / 1.055) ** 2.4

    r_lin = srgb_to_linear(r)
    g_lin = srgb_to_linear(g)
    b_lin = srgb_to_linear(b)

    x = 0.4124564 * r_lin + 0.3575761 * g_lin + 0.1804375 * b_lin
    y = 0.2126729 * r_lin + 0.7151522 * g_lin + 0.0721750 * b_lin
    z = 0.0193339 * r_lin + 0.1191920 * g_lin + 0.9503041 * b_lin

    denom = x + y + z
    if denom <= 1e-8:
        return None

    chroma_x = x / denom
    chroma_y = y / denom
    if abs(0.1858 - chroma_y) < 1e-8:
        return None

    n = (chroma_x - 0.3320) / (0.1858 - chroma_y)
    cct = 449.0 * (n ** 3) + 3525.0 * (n ** 2) + 6823.3 * n + 5520.33
    if math.isnan(cct) or cct < 1500 or cct > 20000:
        return None
    return cct


def label_to_nominal_kelvin(label: str) -> Optional[int]:
    l = label.lower()
    if "tungsten" in l or "incandescent" in l:
        return 3200
    if "fluorescent" in l:
        return 4200
    if "daylight" in l or "sun" in l:
        return 5200
    if "flash" in l:
        return 6000
    if "cloud" in l:
        return 6500
    if "shadow" in l or "shade" in l:
        return 7500
    return None


def pick_wb_index_from_pixel(frame: np.ndarray, x: int, y: int) -> Optional[int]:
    if WB_STATE is None or not WB_STATE.options:
        return None

    h, w = frame.shape[:2]
    if x < 0 or y < 0 or x >= w or y >= h:
        return None

    sampled = frame[y, x]
    cct = estimate_cct_from_bgr(sampled)

    candidates: list[tuple[int, int]] = []
    for option in WB_STATE.options:
        kelvin = label_to_nominal_kelvin(option.label)
        if kelvin is not None:
            candidates.append((option.index, kelvin))

    if not candidates:
        return WB_STATE.current_index

    if cct is None:
        b, _g, r = [float(v) for v in sampled]
        target_k = 3200 if r > b else 6500
    else:
        target_k = int(max(2500, min(8500, cct)))

    best_idx = min(candidates, key=lambda item: abs(item[1] - target_k))[0]
    return best_idx


# Web routes


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory('.', 'manifest.json', mimetype='application/manifest+json')


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory('.', 'service-worker.js', mimetype='application/javascript')


@app.route("/stream")
def stream():
    return app.response_class(
        generate_mjpeg(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/metadata")
def get_metadata():
    CAMERA_METADATA.preview_enabled = PREVIEW_ENABLED
    return jsonify(asdict(CAMERA_METADATA))


@app.route("/api/share-info")
def api_share_info():
    return jsonify(
        {
            "url": get_share_url(),
            "host": get_local_ip_address(),
            "port": get_server_port(),
        }
    )


@app.route("/api/preview", methods=["GET", "POST"])
def api_preview():
    global PREVIEW_ENABLED

    if request.method == "GET":
        return jsonify({"enabled": PREVIEW_ENABLED})

    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled", True))
    PREVIEW_ENABLED = enabled
    CAMERA_METADATA.preview_enabled = PREVIEW_ENABLED

    if PREVIEW_ENABLED:
        enable_camera_viewfinder()
        STREAM_PAUSE_EVENT.set()
    else:
        disable_camera_viewfinder()
        STREAM_PAUSE_EVENT.clear()
        stop_live_view_processes()

    return jsonify({"status": "ok", "enabled": PREVIEW_ENABLED})


@app.route("/api/settings/refresh", methods=["POST"])
def api_refresh_settings():
    CAMERA_METADATA.status = "Refreshing settings"
    was_enabled = PREVIEW_ENABLED

    STREAM_PAUSE_EVENT.clear()
    time.sleep(0.2)
    try:
        update_camera_metadata()
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        if was_enabled:
            STREAM_PAUSE_EVENT.set()
            if CAMERA_METADATA.status != "Capturing":
                CAMERA_METADATA.status = "Ready"


@app.route("/api/shutter", methods=["POST"])
def api_shutter():
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    CAMERA_METADATA.status = "Capturing"
    try:
        result = run_shutter_capture()
        if result.returncode == 0:
            latest = find_latest_capture_filename()
            capture_url = f"/captures/{quote(latest)}?t={int(time.time())}" if latest else None
            return jsonify(
                {
                    "status": "ok",
                    "message": "Photo captured",
                    "capture_url": capture_url,
                }
            )
        else:
            stderr = (result.stderr or result.stdout or "Unknown gphoto2 error").strip()
            detail = stderr.splitlines()[-1] if stderr else "Unknown gphoto2 error"
            return jsonify({"status": "error", "message": f"Shutter failed: {detail}"}), 500
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        CAMERA_METADATA.status = "Ready"


def find_latest_capture_filename() -> Optional[str]:
    if not os.path.isdir(CAPTURE_DIR):
        return None

    candidates: list[tuple[float, str]] = []
    for name in os.listdir(CAPTURE_DIR):
        full_path = os.path.join(CAPTURE_DIR, name)
        if not is_web_renderable_image(full_path):
            continue
        try:
            candidates.append((os.path.getmtime(full_path), name))
        except OSError:
            continue

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


@app.route("/captures/<path:filename>")
def get_capture(filename: str):
    full_path = os.path.join(CAPTURE_DIR, filename)
    if not os.path.isfile(full_path):
        abort(404)
    if not is_web_renderable_image(full_path):
        abort(415)
    return send_from_directory(CAPTURE_DIR, filename)


@app.route("/captures")
@app.route("/captures/")
def captures_index():
    if not os.path.isdir(CAPTURE_DIR):
        return jsonify({"files": []})

    capture_files = []
    for name in sorted(os.listdir(CAPTURE_DIR), reverse=True):
        full_path = os.path.join(CAPTURE_DIR, name)
        if os.path.isfile(full_path) and is_web_renderable_image(full_path):
            capture_files.append(f"/captures/{quote(name)}")

    return jsonify({"files": capture_files})


def is_web_renderable_image(path: str) -> bool:
    """Return True only for image bytes browsers can render directly."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
    except OSError:
        return False

    if header.startswith(b"\xff\xd8\xff"):
        return True  # JPEG
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True  # PNG
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True  # WEBP
    return False


@app.route("/api/wb-pick", methods=["POST"])
def api_wb_pick():
    global LAST_FRAME

    if LAST_FRAME is None:
        return jsonify({"status": "error", "message": "No frame available"}), 400

    data = request.json
    x = data.get("x", 0)
    y = data.get("y", 0)

    idx = pick_wb_index_from_pixel(LAST_FRAME, x, y)
    if idx is None:
        return jsonify({"status": "error", "message": "WB pick failed"}), 400

    if WB_STATE is None:
        return jsonify({"status": "error", "message": "WB unsupported"}), 400

    try:
        result = run_gphoto2(
            ["--set-config", f"{WB_STATE.path}={idx}"], timeout=20
        )
        if result.returncode == 0:
            WB_STATE.current_index = idx
            update_camera_metadata()
            return jsonify({"status": "ok", "message": "White balance updated"})
        else:
            return jsonify({"status": "error", "message": "WB set failed"}), 500
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/wb-options")
def get_wb_options():
    if WB_STATE is None:
        return jsonify([])
    return jsonify([asdict(opt) for opt in WB_STATE.options])


def initialize_camera_state(expected_model: str) -> None:
    global WB_STATE, TIMER_CONFIG_PATHS

    CAMERA_METADATA.status = "Initializing camera"
    try:
        if not detect_camera(expected_model):
            return

        WB_STATE = discover_white_balance()
        TIMER_CONFIG_PATHS = discover_timer_config_paths()
        update_camera_metadata()
        CAMERA_METADATA.preview_enabled = PREVIEW_ENABLED
        if PREVIEW_ENABLED:
            STREAM_PAUSE_EVENT.set()
    except Exception as exc:
        print(f"[Init] Camera initialization error: {exc}", file=sys.stderr)
        set_camera_disconnected()


def main():
    args = parse_args()

    ensure_gphoto2()
    release_macos_camera_lock()

    global CAPTURE_DIR
    CAPTURE_DIR = os.path.abspath(args.save_dir)
    os.makedirs(CAPTURE_DIR, exist_ok=True)

    init_thread = threading.Thread(
        target=initialize_camera_state,
        args=(args.camera_model,),
        daemon=True,
    )
    init_thread.start()

    global METADATA_REFRESH_THREAD
    METADATA_REFRESH_THREAD = threading.Thread(target=metadata_refresh_loop, daemon=True)
    METADATA_REFRESH_THREAD.start()

    print(f"Starting web server at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
