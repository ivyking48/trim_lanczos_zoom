@echo off
call C:\Users\ivanc\miniconda3\condabin\conda.bat activate curtain_grid
cd /d C:\Users\ivanc\Documents\Projects\trim_lazcos_zoom
python trim_lanczos_zoom.py %*
echo.
echo Exit code: %errorlevel%
pause
