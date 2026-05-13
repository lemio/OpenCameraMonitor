#!/usr/bin/env python3
"""Canon EOS live-view fullscreen preview using gphoto2."""

import argparse
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


WINDOW_NAME = "Canon EOS Preview"
SHUTTER_BUTTON = (20, 20, 190, 62)
WB_BUTTON = (210, 20, 460, 62)
READ_CHUNK_SIZE = 65536
MAX_JPEG_BUFFER = 10_000_000


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
class OverlayState:
    fps: float = 0.0
    reconnects: int = 0
    status_message: str = "Running"
    wb_pick_mode: bool = False
    shutter_requested: bool = False
    wb_requested_index: Optional[int] = None
    wb_pick_point: Optional[tuple[int, int]] = None


OVERLAY = OverlayState()
WB_STATE: Optional[WhiteBalanceState] = None
LAST_FRAME: Optional[np.ndarray] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream Canon EOS live-view and display it in fullscreen."
    )
    parser.add_argument(
        "--camera-model",
        default="Canon EOS 600D",
        help="Camera model substring to look for in gphoto2 --auto-detect output.",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="Use a normal resizable window instead of fullscreen.",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="Rotate preview in degrees.",
    )
    parser.add_argument(
        "--save-dir",
        default="captures",
        help="Directory where remote shutter photos are downloaded.",
    )
    return parser.parse_args()


def ensure_gphoto2() -> None:
    if shutil.which("gphoto2") is None:
        print("Error: gphoto2 is not installed or not in PATH.", file=sys.stderr)
        print("Install on macOS with: brew install gphoto2", file=sys.stderr)
        sys.exit(1)


def run_gphoto2(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    release_macos_camera_lock()
    return subprocess.run(
        ["gphoto2", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def detect_camera(expected_model: str) -> None:
    try:
        result = run_gphoto2(["--auto-detect"])
    except subprocess.TimeoutExpired:
        print("Error: Timed out during camera auto-detect.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print("Error: Failed to auto-detect camera.", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    output = result.stdout
    lines = [line.strip() for line in output.splitlines() if line.strip()]

    if len(lines) <= 2:
        print("Error: No camera detected by gphoto2.", file=sys.stderr)
        print("Check USB cable, camera power, and camera mode.", file=sys.stderr)
        sys.exit(1)

    if expected_model.lower() not in output.lower():
        print(
            f"Warning: '{expected_model}' not found in detected cameras.",
            file=sys.stderr,
        )
        print("Continuing with the first detected camera.", file=sys.stderr)


def release_macos_camera_lock() -> None:
    if platform.system() != "Darwin":
        return

    subprocess.run(
        ["pkill", "-9", "ptpcamerad"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def rotate_frame(frame: np.ndarray, degrees: int) -> np.ndarray:
    if degrees == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def point_in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def parse_wb_choices(config_output: str) -> tuple[list[WhiteBalanceOption], Optional[int]]:
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
    if "auto" in l or "manual" in l:
        return None
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
        # Fallback based on simple warm/cool bias when CCT estimate is unstable.
        b, _g, r = [float(v) for v in sampled]
        target_k = 3200 if r > b else 6500
    else:
        target_k = int(max(2500, min(8500, cct)))

    best_idx = min(candidates, key=lambda item: abs(item[1] - target_k))[0]
    return best_idx


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


def white_balance_label() -> str:
    if OVERLAY.wb_pick_mode:
        return "WB: Click Pixel"

    if WB_STATE is None or WB_STATE.current_index is None:
        return "WB: Unknown"

    for option in WB_STATE.options:
        if option.index == WB_STATE.current_index:
            return f"WB: {option.label}"

    return "WB: Unknown"


def draw_button(
    frame: np.ndarray,
    rect: tuple[int, int, int, int],
    label: str,
    color: tuple[int, int, int],
) -> None:
    left, top, right, bottom = rect
    cv2.rectangle(frame, (left, top), (right, bottom), color, -1)
    cv2.rectangle(frame, (left, top), (right, bottom), (255, 255, 255), 1)
    cv2.putText(
        frame,
        label,
        (left + 10, top + 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )


def draw_overlay(frame: np.ndarray) -> np.ndarray:
    out = frame.copy()

    panel_h = 100
    overlay = out[:panel_h, :].copy()
    cv2.rectangle(overlay, (0, 0), (out.shape[1], panel_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.45, out[:panel_h, :], 0.55, 0, dst=out[:panel_h, :])

    draw_button(out, SHUTTER_BUTTON, "Shutter", (30, 30, 200))
    draw_button(out, WB_BUTTON, white_balance_label(), (30, 140, 30))

    cv2.putText(
        out,
        f"FPS: {OVERLAY.fps:.1f}",
        (480, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        out,
        f"Reconnects: {OVERLAY.reconnects}",
        (480, 62),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        out,
        OVERLAY.status_message,
        (20, 92),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    return out


def on_mouse(event: int, x: int, y: int, _flags: int, _userdata) -> None:
    if event != cv2.EVENT_LBUTTONDOWN:
        return

    if point_in_rect(x, y, SHUTTER_BUTTON):
        OVERLAY.shutter_requested = True
        OVERLAY.wb_pick_mode = False
        return

    if point_in_rect(x, y, WB_BUTTON):
        OVERLAY.wb_pick_mode = not OVERLAY.wb_pick_mode
        if OVERLAY.wb_pick_mode:
            OVERLAY.status_message = "Click a neutral pixel to set white balance"
        else:
            OVERLAY.status_message = "WB pick cancelled"
        return

    if OVERLAY.wb_pick_mode:
        OVERLAY.wb_pick_point = (x, y)
        OVERLAY.wb_pick_mode = False
        OVERLAY.status_message = f"WB sampled at ({x}, {y})"


def configure_window(windowed: bool) -> None:
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    if not windowed:
        cv2.setWindowProperty(
            WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN
        )
    cv2.setMouseCallback(WINDOW_NAME, on_mouse)


def start_live_view_process() -> subprocess.Popen:
    commands = [
        [
            "gphoto2",
            "--set-config",
            "/main/actions/viewfinder=1",
            "--set-config",
            "/main/actions/eosmoviemode=1",
            "--capture-movie",
            "--stdout",
        ],
        ["gphoto2", "--capture-movie", "--stdout"],
    ]

    for _ in range(8):
        release_macos_camera_lock()

        for command in commands:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )

            time.sleep(0.5)
            if process.poll() is None:
                return process

        time.sleep(0.2)

    raise RuntimeError(
        "Failed to start gphoto2 live-view stream. Ensure movie/live-view mode is active and no other app is using the camera."
    )


def stop_stream_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()


def reconnect_stream() -> subprocess.Popen:
    OVERLAY.status_message = "Reconnecting stream..."
    OVERLAY.reconnects += 1
    return start_live_view_process()


def trigger_shutter(save_dir: str) -> tuple[bool, str]:
    os.makedirs(save_dir, exist_ok=True)
    filename_pattern = os.path.join(save_dir, "IMG_%Y%m%d_%H%M%S.jpg")
    try:
        result = run_gphoto2(
            [
                "--capture-image-and-download",
                "--filename",
                filename_pattern,
                "--force-overwrite",
            ],
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return False, "Shutter timeout"

    if result.returncode != 0:
        return False, "Shutter failed"

    return True, f"Shot saved to {save_dir}"


def set_white_balance(choice_index: int) -> tuple[bool, str]:
    if WB_STATE is None:
        return False, "White balance unsupported"

    try:
        result = run_gphoto2(
            ["--set-config", f"{WB_STATE.path}={choice_index}"], timeout=20
        )
    except subprocess.TimeoutExpired:
        return False, "White balance timeout"

    if result.returncode != 0:
        return False, "White balance set failed"

    WB_STATE.current_index = choice_index
    return True, "White balance updated"


def cycle_white_balance() -> None:
    if WB_STATE is None or not WB_STATE.options:
        OVERLAY.status_message = "White balance unsupported"
        return

    indexes = [option.index for option in WB_STATE.options]
    if WB_STATE.current_index not in indexes:
        OVERLAY.wb_requested_index = indexes[0]
        return

    current_pos = indexes.index(WB_STATE.current_index)
    next_pos = (current_pos + 1) % len(indexes)
    OVERLAY.wb_requested_index = indexes[next_pos]


def handle_pending_wb_pick() -> None:
    global LAST_FRAME

    if OVERLAY.wb_pick_point is None or LAST_FRAME is None:
        return

    x, y = OVERLAY.wb_pick_point
    OVERLAY.wb_pick_point = None

    idx = pick_wb_index_from_pixel(LAST_FRAME, x, y)
    if idx is None:
        OVERLAY.status_message = "WB pick failed"
        return

    OVERLAY.wb_requested_index = idx


def handle_pending_camera_actions(
    process: subprocess.Popen, save_dir: str
) -> subprocess.Popen:
    shutter = OVERLAY.shutter_requested
    wb_choice = OVERLAY.wb_requested_index

    if not shutter and wb_choice is None:
        return process

    OVERLAY.shutter_requested = False
    OVERLAY.wb_requested_index = None

    stop_stream_process(process)
    release_macos_camera_lock()

    if shutter:
        success, message = trigger_shutter(save_dir)
        OVERLAY.status_message = message
        if not success:
            return reconnect_stream()

    if wb_choice is not None:
        success, message = set_white_balance(wb_choice)
        OVERLAY.status_message = message
        if not success:
            return reconnect_stream()

    return reconnect_stream()


def decode_next_frame(stream, buffer: bytearray) -> Optional[np.ndarray]:
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
        # Keep only the tail so we can resync to the next JPEG start marker.
        tail = buffer.rfind(b"\xff\xd8")
        if tail != -1:
            del buffer[:tail]
        else:
            del buffer[:-1_000_000]

    return np.array([])


def run_preview(windowed: bool, rotation: int, save_dir: str) -> None:
    global LAST_FRAME

    cv2.setUseOptimized(True)
    configure_window(windowed)

    release_macos_camera_lock()
    process = start_live_view_process()
    stream = process.stdout
    assert stream is not None

    running = True

    def stop_handler(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    jpeg_buffer = bytearray()
    last_frame_at = time.time()
    frame_count = 0
    fps_started_at = time.time()

    while running:
        frame = decode_next_frame(stream, jpeg_buffer)

        if frame is None:
            try:
                process = reconnect_stream()
                stream = process.stdout
                assert stream is not None
                jpeg_buffer.clear()
                last_frame_at = time.time()
                continue
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                break

        if frame.size > 0:
            frame = rotate_frame(frame, rotation)
            LAST_FRAME = frame
            frame_count += 1
            elapsed = time.time() - fps_started_at
            if elapsed >= 1:
                OVERLAY.fps = frame_count / elapsed
                frame_count = 0
                fps_started_at = time.time()

            display_frame = draw_overlay(frame)
            cv2.imshow(WINDOW_NAME, display_frame)
            last_frame_at = time.time()

        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord("q")):
            break
        if key == ord("s"):
            OVERLAY.shutter_requested = True
        if key == ord("w"):
            cycle_white_balance()

        handle_pending_wb_pick()

        if process.poll() is not None:
            try:
                process = reconnect_stream()
                stream = process.stdout
                assert stream is not None
                jpeg_buffer.clear()
                last_frame_at = time.time()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                break

        if OVERLAY.shutter_requested or OVERLAY.wb_requested_index is not None:
            try:
                process = handle_pending_camera_actions(process, save_dir)
                stream = process.stdout
                assert stream is not None
                jpeg_buffer.clear()
                last_frame_at = time.time()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                break

        if time.time() - last_frame_at > 5:
            try:
                process = reconnect_stream()
                stream = process.stdout
                assert stream is not None
                jpeg_buffer.clear()
                last_frame_at = time.time()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                break

    stop_stream_process(process)
    LAST_FRAME = None

    cv2.destroyAllWindows()


def main() -> None:
    args = parse_args()

    ensure_gphoto2()
    release_macos_camera_lock()
    detect_camera(args.camera_model)

    global WB_STATE
    WB_STATE = discover_white_balance()
    if WB_STATE is None:
        OVERLAY.status_message = "Running (WB not available)"

    try:
        run_preview(windowed=args.windowed, rotation=args.rotate, save_dir=args.save_dir)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
