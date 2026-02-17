# Wan2GP Timeline Editor Plugin

## 1) Project title (working)

**Wan2GP Timeline Editor Plugin** — a multi-track, NLE-style timeline (Premiere-like) embedded directly inside Wan2GP/WanGP.

## 2) Project overview (short)

This project adds a **dedicated “Timeline Editor” tab** to Wan2GP, turning the generator UI into a **video editing workspace** with a **multi-track timeline**, a **media bin**, an **inspector panel**, and **export presets** (e.g., vertical 9:16). It leverages Wan2GP’s existing ecosystem (Wan / LTX Video / Qwen Image Edit, etc.) and produces final renders using **FFmpeg filtergraphs** for cuts, transitions, compositing, and audio mixing. ([GitHub][1])

## 3) Why it fits Wan2GP (compatibility statement)

Wan2GP already:

1. acts as an “all-in-one” AI video generator supporting **LTX Video** and **Qwen Image** among others, ([GitHub][1])
2. supports a **plugin system** intended to add new top tabs that can share data with the main video generator. ([GitHub][1])

This plugin uses that plugin model to provide a full editor “surface” without forking the app.

## 4) Core features (what the plugin delivers)

1. **Premiere-style multi-track timeline**
   - Video tracks (V1..Vn) and audio tracks (A1..An)
   - Drag/drop, trim (in/out), snapping, ripple (phase 2)
2. **Media Bin**
   - Pulls Wan2GP outputs automatically + supports importing external clips
   - Thumbnails, duration/fps/resolution metadata (via ffprobe/FFmpeg)
3. **Inspector**
   - Clip transform (scale/position), opacity, speed
   - Audio gain/mute/pan
4. **Preview**
   - Proxy renders for smooth scrubbing
   - Playhead + timecode
5. **Export**
   - One-click render to MP4 (H.264/H.265 presets), including vertical formats
   - Optional: embed the project JSON (EDL) into metadata for reproducibility

## 5) Technical approach (how it works)

### 5.1 UI timeline component (Gradio)

Use a Gradio custom component built on **vis.js Timeline** (via `gradio-vistimeline`) to implement:

- **Groups** = tracks, **Items** = clips
- Built-in edit events (drag/resize/select)
- Optional JS access via `elem_id` for advanced behaviors (snapping, shortcuts) ([PyPI][2])

### 5.2 Render engine (FFmpeg filtergraph)

The editor compiles the project EDL into an FFmpeg `filter_complex`:

1. **Cuts & sequences**
   - `trim/setpts` per clip + `concat` per track
2. **Transitions**
   - Video crossfades with `xfade` (`transition`, `duration`, `offset`) ([ffmpeg-graph.site][3])
   - Audio crossfades with `acrossfade` ([patches.ffmpeg.org][4])
3. **Compositing (multi-video tracks)**
   - `overlay` chains, gated by timeline expressions (“timeline editing” capability) ([patches.ffmpeg.org][4])
4. **Audio mixing (multi-audio tracks)**
   - `amix` (optionally normalized/weighted) ([patches.ffmpeg.org][4])

**Constraint handled by design:** `xfade` requires matching resolution/pixel format/frame rate/timebase, so the pipeline normalizes inputs (scale/fps/format) before transitions. ([ffmpeg-graph.site][3])

## 6) Roadmap (deliverable milestones)

1. **MVP (1–2 tracks)**
   - Single video track + single audio track
   - Cuts only (concat), export MP4, basic preview
2. **Transitions**
   - `xfade` + `acrossfade`, offset math, preset transitions
3. **True multi-track**
   - Overlay compositing + `amix`
4. **Editor “feel”**
   - Snapping, ripple edit, keyboard shortcuts, proxy caching

## 7) One-paragraph README / pitch version

**Wan2GP Timeline Editor Plugin** adds a full NLE-style editing workspace to Wan2GP by introducing a multi-track timeline tab (Premiere-like UI), a media bin that ingests generated clips, an inspector for clip transforms/audio controls, and one-click exports. The timeline is implemented with a Gradio vis.js component for interactive drag/trim editing, while final renders are compiled into FFmpeg filtergraphs (cuts, crossfades, overlays, and audio mixing) to generate production-ready MP4 outputs. It is designed to sit natively inside Wan2GP’s plugin system and complement its built-in models (e.g., LTX Video and Qwen Image editing). ([GitHub][1])

---

## Proposition to continue (precise)

Tell me the **target use case** and this description can be tailored accordingly:

1. **Open-source README** (technical, features + install),
2. **Product pitch** (benefits, differentiators), or
3. **Grant/investor brief** (market + roadmap + risks).

Reply with **1/2/3** + the intended audience (devs / creators / studio).

[1]: https://github.com/deepbeepmeep/Wan2GP?utm_source=chatgpt.com "GitHub - deepbeepmeep/Wan2GP: A fast AI Video Generator for the GPU Poor. Supports Wan 2.1/2.2, Qwen Image, Hunyuan Video, LTX Video and Flux."
[2]: https://pypi.org/project/gradio-vistimeline/?utm_source=chatgpt.com "gradio-vistimeline · PyPI"
[3]: https://ffmpeg-graph.site/filters/xfade/?utm_source=chatgpt.com "ffmpeg xfade filter - Technical Reference"
[4]: https://patches.ffmpeg.org/ffmpeg-filters.html?utm_source=chatgpt.com "FFmpeg Filters Documentation"
