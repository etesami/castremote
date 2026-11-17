# castremote

Phone-browser remote that plays videos and subtitles on a Chromecast. See `README.md` for features, setup and config.

## Layout

- `app.py`: Flask server, config loading, device discovery, media serving and API routes. Playback state is one global `session`, so it's single-user by design.
- `subs.py`: subtitle pipeline: find, extract with `ffmpeg`, decode, parse, convert to WebVTT.
- `index.html`: the whole UI (vanilla JS, no build step). Served from disk, so edits appear on reload.
- `config.json`: local and git-ignored. Add new settings to `config.example.json` and the README table too.

## Constraints

- Keep Python 3.8 compatible.
- PyChromecast is pinned to `<14`. Its API has changed between major versions, so check the code against the source before upgrading.
- `ffmpeg` and `ffprobe` are the only external tools. `mkvextract` was removed on purpose: `ffmpeg` gave the same text and line counts on 40 real MKVs. Don't add it back.

## Testing

There's no test suite. **Don't start a cast without asking: it takes over a real TV.**

- Importing `app` is safe, because discovery only starts under `__main__`.
- To test routes, use `app.test_client()` and put a fake cast object in `app.casts`. It needs `uuid`, `name`, `model_name`, `cast_info.host`, `status.volume_level`, `wait()`, `quit_app()` and a `media_controller` with `status`, `play_media()`, `block_until_active()` and `seek()`.
- To test the UI, serve the app the same way and drive it with Playwright at phone size.
- Use `public_host` in a test config to keep real LAN addresses out of URLs.

## Things that break easily

- **Subtitle CORS:** the Chromecast rejects subtitles without `Access-Control-Allow-Origin`, so `/subs/` must send it.
- **Reachable URLs:** video and subtitle URLs must be reachable from the Chromecast, so never use `localhost`. `lan_ip_towards()` picks the right interface.
- **Subtitle changes reload the video:** the default receiver can't shift or replace a loaded track, so timing and track changes reload at the current position.
- **Other audio tracks are a live remux:** the Chromecast only plays a file's first audio track, so other tracks go through `/stream/`, an `ffmpeg` pipe that can't seek. Seeks reload it starting at a keyframe found with `ffprobe`, and `session["start"]` holds where it began. Use `position()` for the place in the video, not `adjusted_current_time`, and shift subtitles by `start`.
- **Reversed punctuation:** many Persian subtitles type end punctuation first so they look right in left-to-right players. `_fix_rtl_punctuation` moves it to the end. Lines that already end in punctuation stay as they are, and a real leading `...` gets moved too (a known trade-off).
- **Windows-1256:** it has no Persian ی, so those files use Arabic ي and ك. `fix_persian` converts them, and applies to the whole file rather than per line.
- **Quiet logs:** the page polls `/api/status` every second, so request logging is off by default (`log_requests`).

## Security

The app has no authentication and listens on all interfaces. Keep these checks:

- `resolve_media()` blocks paths outside `media_dir`, including through symlinks, and hidden files.
- `/api/play` accepts only `http(s)` URLs for videos and subtitle links, which stops `file://` reads through `urllib` or `ffmpeg`.

## Public repo

Don't commit personal paths, hostnames, device names, LAN addresses or real media filenames. Screenshots must use dummy files and a fake Chromecast. Machine-specific notes go in `CLAUDE.local.md`, which is git-ignored.
