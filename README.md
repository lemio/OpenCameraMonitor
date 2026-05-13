# Canon EOS 600D Live Preview

A modern web-based live preview tool for Canon EOS 600D with iOS-style UI and remote camera control.

## Features

- **Modern Web UI**: Clean, responsive interface with iOS-style shutter button
- **Live Preview**: Real-time camera preview stream
- **Remote Shutter**: Capture photos directly from the browser
- **Pixel-based White Balance**: Click any pixel in the preview to auto-adjust white balance (Magic Lantern style)
- **Camera Metadata**: Real-time display of battery level, shutter speed, ISO, white balance
- **Exposure Visualization**: Visual exposure dial (-5 to +5 EV)
- **Auto-Reconnect**: Automatically reconnects if the stream drops
- **Vector Icons**: Crisp, scalable interface elements

## Prerequisites

1. Install `gphoto2` (macOS):

   ```bash
   brew install gphoto2
   ```

2. Put your Canon EOS 600D in movie/live-view mode and connect over USB.

3. Disable camera auto power-off to avoid the stream stopping.

## ⚠️ Important: Canon EOS Utility

**The Canon EOS Utility application MUST be closed before running this tool.** On macOS, EOS Utility holds an exclusive USB lock that prevents gphoto2 from accessing the camera. 

The `start_web.sh` script automatically terminates EOS Utility and other conflicting processes, but if you manually run `python server.py`, please ensure:
- ❌ Close Canon EOS Utility completely
- ❌ Close any other Canon software
- ✅ The camera will be accessible when this app starts

If you see "Could not claim the USB device" or "No camera found" errors, check that EOS Utility is not running:
```bash
pkill -9 "EOS Utility"
```

## Installation

```bash
# Clone or navigate to the project directory
cd CanonEos

# Create virtual environment (automatic on first run)
python3 -m venv .venv

# Install dependencies
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

### Web Interface (Recommended)

```bash
bash start_web.sh
```

Then open your browser to: **http://localhost:5000**

### Command Line Arguments

```bash
python server.py --host 127.0.0.1 --port 5000
```

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

## Usage

### Taking a Photo
Click the large circular **shutter button** at the bottom center of the screen. Photos are saved to the `captures/` directory.

### Setting White Balance
1. Click the **☀️ WB** button in the bottom-left corner
2. Click any neutral (gray/white) pixel in the preview
3. The app automatically selects the closest white balance preset

### Reading Camera Status
The top bar displays:
- **🔋 Battery**: Battery percentage
- **⚡ Shutter Speed**: Current shutter speed setting
- **🎚️ ISO**: Current ISO value
- **☀️ WB**: Current white balance preset

### Exposure Dial
The exposure visualization shows the exposure compensation range from -5 to +5 stops, with real-time indicator.

## File Structure

```
CanonEos/
├── server.py              # Flask backend server
├── requirements.txt       # Python dependencies
├── start_web.sh          # Launcher script for web UI
├── start_preview.sh      # Launcher script for CLI
├── templates/
│   └── index.html        # Web UI template
├── static/
│   ├── style.css         # Modern CSS styling
│   └── script.js         # Client-side JavaScript
└── captures/             # Downloaded photos (created on first use)
```

## Troubleshooting

### Camera Not Detected
```bash
gphoto2 --auto-detect
```

If no camera appears, check:
- **Verify EOS Utility is closed** – This is the most common cause on macOS:
  ```bash
  pkill -9 "EOS Utility"
  ```
  Then restart the application.

### macOS PTPCamera Lock
On macOS, the system PTPCamera service may claim the camera. The app auto-releases it, but if you see USB errors:
```bash
pkill -9 ptpcamerad
```

### Stream Drops
The app automatically reconnects when the stream drops. If reconnection fails repeatedly:
- Check the USB connection
- Restart the camera
- Restart the app

### Low Frame Rate
The app streams at approximately 14-15 FPS depending on camera USB mode and system load. This is the limit of gphoto2's MJPEG stream from the Canon EOS 600D.

## API Endpoints

The Flask backend exposes these REST endpoints:

- `GET /` – Main web UI
- `GET /stream` – MJPEG camera stream
- `GET /api/metadata` – Current camera metadata (JSON)
- `POST /api/shutter` – Trigger remote shutter
- `POST /api/wb-pick` – Set white balance from pixel (x, y)
- `GET /api/wb-options` – List available white balance presets

## Performance

- **Preview FPS**: ~14 FPS (camera hardware limit)
- **Memory Usage**: ~100-200 MB
- **CPU Usage**: Low (hardware-accelerated video decode)
- **Latency**: ~200-300ms from scene to preview

## Compatibility

- **Camera**: Canon EOS 600D (tested)
- **OS**: macOS (primary), Linux/Windows with gphoto2
- **Browser**: Modern browsers (Chrome, Safari, Firefox, Edge)
- **Python**: 3.8+

## Advanced

### Custom Save Directory
```bash
python server.py --save-dir /path/to/photos
```

### Listen on All Interfaces
```bash
python server.py --host 0.0.0.0 --port 5000
```

Then access from another machine at: `http://<your-ip>:5000`

### Development Mode
```bash
FLASK_ENV=development python server.py
```

## CLI Tool (Legacy)

The original CLI tool with OpenCV is still available:
```bash
bash start_preview.sh
```

## Controls
- `q` or `Esc` to quit
- Click `Shutter` button to capture and download a photo
- Click `White Balance` button, then click a neutral pixel in the image to pick WB
- Press `s` for shutter (keyboard shortcut)
- Press `w` to cycle white balance options (keyboard shortcut)

## Useful CLI Options

```bash
# Show in a normal resizable window instead of fullscreen
python canon_eos_preview.py --windowed

# Rotate preview if camera orientation is not correct
python canon_eos_preview.py --rotate 90

# Save remote captures to a custom folder
python canon_eos_preview.py --save-dir my-captures
```

## Troubleshooting (CLI)

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
