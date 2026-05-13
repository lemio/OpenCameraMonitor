# Canon EOS 600D Fullscreen Preview

This tool connects to a Canon EOS 600D over USB and opens a fullscreen live preview window.

It uses:
- `gphoto2` for camera live-view stream
- `OpenCV` for rendering the fullscreen preview

Features:
- Fullscreen live preview
- On-screen status overlay with FPS and reconnect counter
- Auto-reconnect when the stream drops
- Clickable remote `Shutter` button
- Clickable pixel-based `White Balance` picker button (Magic Lantern style workflow)

## Prerequisites

1. Install `gphoto2` (macOS):

   ```bash
   brew install gphoto2
   ```

2. Put your Canon EOS 600D in movie/live-view mode and connect over USB.

3. Disable camera auto power-off to avoid the stream stopping.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
pkill -9 ptpcamerad 2>/dev/null || true
python canon_eos_preview.py
```

One-command launcher:

```bash
bash start_preview.sh
```

Controls:
- `q` to quit
- `Esc` to quit
- Click `Shutter` button to capture and download a photo
- Click `White Balance` button, then click a neutral pixel in the image to pick WB
- Press `s` for shutter (keyboard shortcut)
- Press `w` to cycle white balance options (keyboard shortcut)

## Useful options

```bash
# Show in a normal resizable window instead of fullscreen
python canon_eos_preview.py --windowed

# Rotate preview if camera orientation is not correct
python canon_eos_preview.py --rotate 90

# Save remote captures to a custom folder
python canon_eos_preview.py --save-dir my-captures
```

## Troubleshooting

- If no camera is detected, run:

  ```bash
  gphoto2 --auto-detect
  ```

- If the stream opens and closes immediately, verify the camera is in movie/live-view mode.
- If another app has the camera open, close that app first.
- On macOS, `ptpcamerad` may auto-claim the camera. Release it before launching:

  ```bash
  pkill -9 ptpcamerad 2>/dev/null || true
  ```

- Remote shutter photos are downloaded into `captures/` by default.
