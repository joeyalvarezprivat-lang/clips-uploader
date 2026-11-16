# ClipsUploader

Plant Kurzclips von ClipsForTheUnemployed auf YouTube Shorts, Instagram Reels und Facebook Reels über die offiziellen Schnittstellen.

- `cloud_sync.py run` läuft alle ~10 Minuten per GitHub Actions (`.github/workflows/upload.yml`).
- Warteschlange und Videos liegen im privaten Dropbox-App-Ordner, Zugangsdaten nur im Repository-Secret `ZUGANG`.
- Hier im Repository liegen keine Videos und keine Schlüssel.
