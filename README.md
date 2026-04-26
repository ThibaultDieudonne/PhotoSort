# PhotoSort

A keyboard-driven desktop app for quickly sorting photos and videos into **keep** or **discard** piles.

## Features

- Browse a folder's media files one by one (JPG, PNG, HEIC, MP4, MOV, MKV, and more)
- Press **K** to keep or **D** to discard — files are moved instantly, preserving sub-folder structure
- Navigate with **← →** (only through unprocessed files)
- Videos autoplay; images are displayed with correct EXIF rotation
- **5 GiB background preloader** — media is decoded in a background thread so display is instant
- Progress is implicit: re-opening the same folder resumes where you left off

## Output folders

Two folders are created at the root of the selected folder (if they don't already exist):

| Folder | Contents |
|---|---|
| `_<folder-name>/` | Items you kept |
| `_discarded/` | Items you discarded |

Sub-folder structure is preserved. For example, a file at `holidays/paris/img.jpg` discarded will land at `_discarded/holidays/paris/img.jpg`.

## Supported formats

**Images:** `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.webp` `.heic` `.heif`  
**Videos:** `.mp4` `.avi` `.mov` `.mkv` `.wmv` `.flv` `.m4v` `.webm` `.ts` `.mts`

HEIC/HEIF support requires `pillow-heif` (included in requirements).

## Requirements

- Python 3.10+
- Windows (tested); Linux/macOS should work with an FFmpeg Qt6 multimedia backend

## Setup

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

## Running (venv already set up)

```powershell
.venv\Scripts\Activate.ps1
python app.py
```

Or without activating:

```powershell
.venv\Scripts\python.exe app.py
```
