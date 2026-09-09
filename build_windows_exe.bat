@echo off
REM Run this on an actual Windows machine to build VideoMerger.exe locally.
REM Requires Python 3.9+ installed and on PATH (https://python.org).

python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller

for /f "delims=" %%i in ('python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"') do set FFMPEG_PATH=%%i
copy "%FFMPEG_PATH%" ffmpeg.exe

pyinstaller --onefile --windowed --name VideoMerger --add-binary "ffmpeg.exe;." video_merger.py

echo.
echo Done. Your app is at dist\VideoMerger.exe
pause
