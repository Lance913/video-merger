# Video Merger

A simple Windows tool for joining several long video files (e.g. 1-3 hour
recordings) into one, without the slow re-encode that tools like CapCut
always do on export.

- Add multiple videos (or a whole folder), arrange them in order with
  **Move Up / Move Down**
- Pick an output file
- **Start Merging** — live progress bar with exact percentage (e.g.
  `55.34%`) and an estimated time remaining that adapts as it goes

Everything runs 100% locally on your machine once built as an .exe -
your video files never get uploaded anywhere, even though the .exe itself
is built in the cloud (GitHub Actions only compiles the program; your
footage never touches it). This matters for very large files (100GB+),
since merging that much data still has to move through your own disk
either way - there's no faster shortcut than that, but at least the fast
path (matching clips) does it as a raw copy instead of CapCut's full
re-encode.

## Why it's fast

If your clips already share the same resolution and codec (very common
when they're parts of the same recording), the tool joins them with a raw
stream copy - no re-encoding at all. That's limited only by disk speed, so
merging several hours of footage typically finishes in seconds, not
minutes.

If the clips differ (different resolution/codec), the mismatched ones are
first normalized to a common resolution (letterboxed, aspect ratio
preserved) - this step re-encodes, so it's slower, but only as slow as it
has to be, and only for the files that actually need it.

## Option A: Download the ready-made .exe (no Python needed)

Once this project is pushed to GitHub, a Windows `.exe` is built
automatically:

1. Push this folder to a GitHub repository.
2. Trigger a build:
   - Push a tag, e.g. `git tag v1.0 && git push origin v1.0` (also creates a
     GitHub Release with the exe attached), **or**
   - Go to the repo's **Actions** tab -> "Build Windows exe" -> **Run workflow**.
3. Download `VideoMerger.exe` from the workflow's **Artifacts**, or from
   **Releases** if you pushed a tag.
4. Double-click `VideoMerger.exe` on Windows. Everything is bundled inside
   (including ffmpeg), so it works fully offline.

Windows SmartScreen may warn about an "unrecognized app" the first time,
since the exe isn't code-signed - click **More info -> Run anyway**.

## Option B: Run the Python script directly

Works on Windows, Mac, or Linux, for testing right away.

```bash
pip install -r requirements.txt
python video_merger.py
```

The first run downloads a small ffmpeg binary automatically (via
`imageio-ffmpeg`) and caches it - after that it also works offline.

## Building the exe yourself on a Windows PC

Run `build_windows_exe.bat` (double-click it, or run it from a terminal).
It installs dependencies, bundles ffmpeg, and produces
`dist\VideoMerger.exe`.

## Notes

- Output is always an `.mp4`.
- The order you arrange clips in the list is the order they'll appear in
  the final video.
- If clips need normalizing, the target resolution used is whichever
  resolution is most common among your selected clips (so a single
  oddly-sized intro clip won't drag the rest down/up).
