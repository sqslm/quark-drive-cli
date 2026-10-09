@echo off
rem Development launcher only; it is NOT shipped in the installer payload.
rem The installer instead places the released launcher next to Extension:
rem   <PotPlayer>\Extension\quark-potplayer.exe    windowed onefile, forwards %*
rem   <PotPlayer>\Extension\quark-potplayer.path   records the component path
rem which runs "%LOCALAPPDATA%\QuarkPotPlayer\app\QuarkPotPlayer.exe" %* and prints
rem exactly one JSON line {base_url, token} to stdout and never a Cookie.
rem The AngelScript extensions prefer the .exe and fall back to a quark-potplayer.cmd,
rem always calling it as: quark-potplayer.exe --ensure-service
rem Override the interpreter with QUARK_POTPLAYER_PYTHON; default is python on PATH.
setlocal
if not defined QUARK_POTPLAYER_PYTHON set "QUARK_POTPLAYER_PYTHON=python"
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
"%QUARK_POTPLAYER_PYTHON%" -m quark_cli.potplayer %*
