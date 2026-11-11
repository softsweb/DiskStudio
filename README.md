# SoftsWeb Disc Studio

A local web interface for burning pictures into the data of a DVD, with no ink and no label.
It runs the Linux command line engine of [Disc Image Studio](https://github.com/JiaFeiMiao-K-Cat/DiscImageStudio)
(GPL-3.0) and adds a browser UI on top: presets, auto tone, a realistic preview and calibration.

Video: https://www.youtube.com/@SoftsWeb

## Run it with Docker

```bash
git clone https://github.com/softsweb/DiskStudio.git
cd DiskStudio
docker compose up -d --build
```

Open http://127.0.0.1:8765 in your browser. The server only listens on your own machine.

Generated images and your saved presets are kept in `./data`.

## Make a disc

1. Pick the disc preset that matches your blank DVD.
2. Drop a picture, set the fit, then press Auto for the tone.
3. Press Generate disc image. The engine render takes about a minute and shows what the disc will look like.
4. Burn the ISO with the command shown in the page (it uses `-nopad` because the image fills the whole disc):

```bash
xorriso -as cdrecord -v dev=/dev/sr0 speed=4 -dao -nopad data/output/render-XXXX.iso
```

## Another kind of disc

Presets exist for a few known discs. For any other DVD or CD, use the calibration test disc, then save the result
as your own preset. Saved presets are stored in `data/presets.user.json`.

## Without Docker

You need Python 3 with Pillow, the release CLI in `../cli/disc-image-studio` and a clone of the upstream repo in `../repo`
(for the built-in presets). Then run `python3 server.py`. `DIS_CLI`, `DIS_PRESETS`, `DATA_DIR`, `HOST` and `PORT` can be overridden.

Not affiliated with the upstream project. This UI only calls its CLI.
