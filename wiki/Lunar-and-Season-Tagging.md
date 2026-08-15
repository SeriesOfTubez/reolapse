# Lunar & Season Tagging

## Lunar event detection

With `events.lunar_enabled: true`, ReoLapse computes real moon events — full
moon, blue moon, harvest moon, and lunar eclipses — using
[Skyfield](https://rhodesmill.org/skyfield/) and a local JPL ephemeris
(`de421.bsp`, ~17 MB, downloaded once into `data/ephemeris/`). **No location
is required**: a full moon happens at the same instant everywhere on Earth,
so the phase-based tags (`full-moon`, `blue-moon`, `harvest-moon`) work with
nothing else configured. This does need a CPU meeting the x86-64-v2 baseline
(see [Requirements](https://github.com/SeriesOfTubez/reolapse#requirements)) —
on an under-specified VM it fails silently, logging `event source failed: NumPy
was built with baseline optimizations...` while the rest of ReoLapse keeps
working normally.

Eclipses are a little more subtle. The eclipse itself is also a geocentric
event, but *visibility* is not — only the hemisphere facing the Moon at that
moment can see it. If a location is configured (shared with the weather
settings), an eclipse is only tagged `blood-moon` (total) or `lunar-eclipse`
(partial) when the Moon was actually above your horizon for it; without a
location, every eclipse is tagged unconditionally since there's nothing to
check visibility against.

Lunar tags are metadata only by default — they don't trigger burst capture.
Add them to `events_video.tags` if you want an automatic clip, e.g.
`2026-03-03_blood-moon.mp4`.

## Season tagging

With `events.season_enabled: true`, every frame and video gets tagged with
its astronomical season (`spring`/`summer`/`fall`/`winter`), computed from
the real equinox/solstice instants each year via Skyfield — not a fixed
calendar approximation. No location is required: it defaults to Northern
Hemisphere seasons, which is right for most current users; set a location if
you're south of the equator so the tags flip correctly (July is winter
there, not summer).

Frames get it the same way as weather/lunar tags — a JPEG comment. Videos
get it as MP4 metadata (`season=summer`), readable with `ffprobe -show_format`
or exiftool. If you're curious why that needed a nonstandard `ffmpeg` flag:
the mov/mp4 muxer only writes a fixed whitelist of "known" keys (`comment`,
`artist`, …) by default and silently drops anything else, including custom
keys like `season` — `-movflags use_metadata_tags` turns that off.
