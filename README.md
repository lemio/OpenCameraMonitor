# Camera Live Preview Web UI

https://github.com/user-attachments/assets/c24f03fb-2077-449e-8bb6-7d2f51fb5b7f

Simple web interface for USB-connected cameras using `gphoto2`.

Canon EOS 600D is tested in this repository, but other `gphoto2`-compatible cameras may also work.

## What It Does

- Live view in the browser
- Trigger photo capture from the browser
- Save captured images to the `captures/` folder

## Requirements

- Python 3.8+
- `gphoto2`
- A USB-connected camera supported by `gphoto2`

Install `gphoto2` on macOS:

```bash
brew install gphoto2
```

## Setup

```bash
cd CanonEos
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
bash start_web.sh
```

Open:

- `http://localhost:5000`

## Main Controls

- **Live Preview Toggle**: enable/disable live view
- **Shutter Button**: take a picture

Captured files are stored in:

- `captures/`

## Notes About Compatibility

- Canon EOS cameras are tested.
- Other cameras can work if `gphoto2 --auto-detect` finds them and live view/capture commands are supported by the device.

Quick check:

```bash
gphoto2 --auto-detect
```

## Optional Server Arguments

```bash
python server.py --port 5000 --save-dir captures
```

- `--save-dir` controls where captured files are written.
