# QuickCut

Phone-friendly automatic talking-head video editor. Upload a recording, automatically assemble retained takes, review and export MP4.

Python 3.12, FastAPI, FFmpeg and faster-whisper. Deploy using Dockerfile with a persistent volume at /data and a random QUICKCUT_TOKEN secret of at least 24 characters. One replica only.

No video footage or credentials belong in this repository. Automatic take selection is heuristic and may need correction. Removed takes remain recoverable.
