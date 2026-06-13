# auto-sync

beat-reactive rgb lights that follow whatever you're playing on spotify.

i got some cheap bluetooth led strips (the ones that show up as "Triones" or "QHM" in bluetooth settings) and wanted them to actually react to music instead of just cycling through presets. this script listens to your system audio, detects beats, and pushes colors over ble. spotify track info shows up in the terminal because why not.

**macos only** for the spotify part — it uses applescript to read what's playing. the audio + ble stuff should work on linux/windows too if you use `--no-spotify`.

## what you need

- python 3.10+
- a mac (for spotify integration)
- bluetooth rgb lights — tested with Triones / QHM / Magic / HappyLight style strips
- an audio source (mic, or [VB-Cable](https://vb-audio.com/Cable/) if you want system audio)

## setup

```bash
git clone https://github.com/ghostwwn/audiosync.git
cd audiosync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

pyaudio needs portaudio on mac:

```bash
brew install portaudio
```

if `pip install pyaudio` still fails, try:

```bash
pip install --global-option='build_ext' --global-option='-I/opt/homebrew/include' --global-option='-L/opt/homebrew/lib' pyaudio
```

## usage

```bash
# run it (auto-detects VB-Cable if installed, otherwise default mic)
python auto_sync.py

# see what audio inputs you have
python auto_sync.py --list-devices

# pick a specific input
python auto_sync.py --device 3

# only react to bass (60-250 hz) — good for electronic stuff
python auto_sync.py --bass-only

# skip spotify, just do the lights
python auto_sync.py --no-spotify
```

ctrl+c to stop. lights go back to warm white on connect, then cycle hues on each beat.

## how it works (roughly)

1. scans for ble lights matching known device names
2. opens a mic stream and runs a simple energy-based beat detector
3. on each beat, writes an rgb value to the light's gatt characteristic
4. polls spotify every few seconds for track name, artist, album art
5. renders a live terminal ui with rich

nothing fancy — no ml, no frequency analysis beyond optional bass filtering. just rms energy vs a rolling average with a cooldown so it doesn't spam.

## troubleshooting

| problem | thing to try |
|---------|--------------|
| no lights found | make sure they're on and not connected to your phone |
| beats feel off | try `--bass-only` or adjust `BEAT_THRESHOLD` in the script |
| spotify says "nothing playing" | open spotify and hit play. needs accessibility permissions for applescript |
| no audio detected | run `--list-devices` and pass `--device N` |
| pyaudio install fails | `brew install portaudio` first |

## config

most tunables are at the top of `auto_sync.py` — beat sensitivity, bass range, ble device names, colors, etc. i left them as constants instead of a config file because it's easier to hack on.

## license

mit — do whatever you want with it

---

built by [@ghostwwn](https://github.com/ghostwwn)
