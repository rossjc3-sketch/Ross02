# QuickCut

Phone-friendly automatic talking-head video editor. Upload a recording, automatically assemble retained takes, review and export MP4.

Deploy using Dockerfile with persistent storage at /data and a private QUICKCUT_TOKEN secret of at least 24 characters. One replica only. No videos or credentials belong in this repository.

Automatic take selection uses transcript matching and may need correction. Removed takes remain recoverable.
