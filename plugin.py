from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import gradio as gr
from PIL import Image

from shared.utils.plugins import WAN2GPPlugin


# =========================
# FFprobe helper (audio)
# =========================
def _which_ffprobe() -> str:
    if os.name == "nt":
        for cand in ("ffprobe.exe", "ffprobe"):
            if os.path.exists(cand):
                return cand
        return "ffprobe.exe"
    return "ffprobe"


def probe_audio_duration_seconds(path: str) -> Optional[float]:
    ffprobe = _which_ffprobe()
    cmd = [ffprobe, "-v", "error", "-show_format", "-of", "json", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(p.stdout)
        fmt = data.get("format", {}) if isinstance(data, dict) else {}
        dur = fmt.get("duration")
        return float(dur) if dur is not None else None
    except Exception:
        return None


def pil_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    if fmt.upper() == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/{fmt.lower()};base64,{b64}"


# =========================
# Project model
# =========================
@dataclass
class MediaItem:
    id: str
    name: str
    path: str
    kind: str  # "video" | "image" | "audio"
    fps: Optional[float] = None
    frames: Optional[int] = None
    duration_s: Optional[float] = None


@dataclass
class Clip:
    id: str
    media_id: str
    track_id: str  # V1,V2,V3,A1,A2,A3
    start_f: int
    in_f: int
    out_f: int  # exclusive
    kind: str    # "video" | "image" | "audio"


@dataclass
class Project:
    fps: float
    px_per_frame: float
    playhead_f: int
    selected_clip_id: Optional[str]
    media: List[MediaItem]
    clips: List[Clip]


def default_project() -> Project:
    # With fps=25 and px_per_frame=2 => 50 px per second
    return Project(
        fps=25.0,
        px_per_frame=2.0,
        playhead_f=0,
        selected_clip_id=None,
        media=[],
        clips=[],
    )


def dumps_project(p: Project) -> str:
    return json.dumps(asdict(p), ensure_ascii=False)


def loads_project(raw: str) -> Project:
    d = json.loads(raw) if raw else {}
    media = [MediaItem(**m) for m in d.get("media", [])]
    clips = [Clip(**c) for c in d.get("clips", [])]
    return Project(
        fps=float(d.get("fps", 25.0)),
        px_per_frame=float(d.get("px_per_frame", 2.0)),
        playhead_f=int(d.get("playhead_f", 0)),
        selected_clip_id=d.get("selected_clip_id"),
        media=media,
        clips=clips,
    )


def find_media(p: Project, media_id: str) -> Optional[MediaItem]:
    for m in p.media:
        if m.id == media_id:
            return m
    return None


def find_clip(p: Project, clip_id: Optional[str]) -> Optional[Clip]:
    if not clip_id:
        return None
    for c in p.clips:
        if c.id == clip_id:
            return c
    return None


def clip_duration_frames(c: Clip) -> int:
    return max(1, c.out_f - c.in_f)


def clip_covers_frame(c: Clip, frame: int) -> bool:
    dur = clip_duration_frames(c)
    return c.start_f <= frame < (c.start_f + dur)


def track_priority(track_id: str) -> int:
    # V3 > V2 > V1, audio ignored for preview
    if track_id.startswith("V"):
        try:
            return int(track_id[1:])
        except Exception:
            return 0
    return -999


def _get_preview_image(plugin: "TimelineEditorPlugin", p: Project) -> Optional[Image.Image]:
    """Helper to get the actual PIL Image for the current playhead frame"""
    frame = p.playhead_f
    candidates = [c for c in p.clips if c.kind in ("video", "image") and clip_covers_frame(c, frame)]
    if not candidates:
        return None

    candidates.sort(key=lambda c: track_priority(c.track_id), reverse=True)
    top = candidates[0]
    m = find_media(p, top.media_id)
    if not m:
        return None

    try:
        if top.kind == "image":
            return Image.open(m.path).convert("RGB")

        # video
        media_frame = top.in_f + (frame - top.start_f)
        get_frame = getattr(plugin, "get_video_frame", None)
        if callable(get_frame):
            pil_img = get_frame(m.path, int(media_frame), return_PIL=True)
            if isinstance(pil_img, Image.Image):
                return pil_img
    except Exception:
        pass
    return None


def compute_preview_uri(plugin: "TimelineEditorPlugin", p: Project) -> str:
    """
    Real preview: choose topmost (highest V-track) video/image clip under playhead and render a frame/image.
    """
    img = _get_preview_image(plugin, p)
    if img:
        return pil_to_data_uri(img, "PNG")
    return ""


# =========================
# Plugin
# =========================
class TimelineEditorPlugin(WAN2GPPlugin):
    name = "Wan2GP Timeline Editor"

    def setup_ui(self):
        self.add_tab(
            tab_id="timeline_editor_tab",
            label="Timeline",
            component_constructor=self.create_ui,
            position=1,
        )

        # Wan2GP injects requested globals as attributes via setattr(plugin, name, fn)
        self.request_global("get_unique_id")
        self.request_global("has_video_file_extension")
        self.request_global("has_image_file_extension")
        self.request_global("has_audio_file_extension")
        self.request_global("get_video_info")   # returns (fps,w,h,frame_count)
        self.request_global("get_video_frame")  # supports return_PIL=True

        self.request_component("state")

    def create_ui(self):
        mount_container = "<div id='nle-mount'></div>"

        # HTML body definition
        UI_BODY_HTML = r"""
<main class="flex-1 flex flex-col min-h-0">
    <!-- TOP HALF -->
    <div class="flex h-[55%] min-h-0 border-b panel-border">

        <!-- TOP LEFT: Effect Controls -->
        <div class="w-[28%] flex flex-col panel-bg border-r panel-border">
            <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2a2a] bg-[#1a1a1a]">
                <div class="flex gap-4">
                    <span class="tab-active font-medium cursor-pointer">Effect Controls <i class="ph ph-list ml-1 text-gray-500"></i></span>
                </div>
            </div>
            <div class="flex-1 p-3 flex flex-col gap-2 overflow-auto" id="effect-panel">
                <span class="text-gray-500">(Select a clip to view FFmpeg parameters)</span>
            </div>
        </div>

        <!-- TOP RIGHT: Program Monitor -->
        <div class="flex-1 flex flex-col panel-bg">
            <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2a2a] bg-[#1a1a1a]">
                <span class="text-gray-400 font-medium">Program: Sequence 01 <i class="ph ph-list ml-1 text-gray-500"></i></span>
            </div>

            <div class="flex-1 bg-black flex items-center justify-center relative overflow-hidden group">
                <img
                    id="program-preview"
                    src=""
                    alt="Video Preview"
                    class="max-w-full max-h-full object-contain pointer-events-none opacity-0 transition-opacity duration-200"
                    style="filter: sepia(40%) hue-rotate(-10deg) saturate(150%) contrast(120%);">
                <div class="absolute inset-0 bg-orange-900/20 mix-blend-overlay"></div>

                <!-- Timecode Overlay -->
                <div class="absolute top-4 right-4 text-white/50 font-mono text-xl tracking-widest drop-shadow-md" id="preview-timecode">
                    00:00:00:00
                </div>
            </div>

            <div class="h-12 bg-[#1e1e1e] flex flex-col px-3 justify-center shrink-0 border-t panel-border">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <span class="text-[#2d8ceb] font-mono" id="main-timecode">00:00:00:00</span>
                        <span class="text-gray-400 bg-[#2a2a2a] px-2 py-0.5 rounded text-xxs flex items-center gap-1 cursor-pointer hover:text-white">
                            Fit <i class="ph ph-caret-down"></i>
                        </span>
                    </div>

                    <div class="flex items-center gap-4 text-gray-400 text-lg">
                        <i class="ph ph-skip-back hover:text-white cursor-pointer" id="btn-home"></i>
                        <i class="ph-fill ph-play hover:text-white cursor-pointer text-xl" id="btn-play"></i>
                        <i class="ph ph-skip-forward hover:text-white cursor-pointer" id="btn-end"></i>
                        <i class="ph ph-camera hover:text-white cursor-pointer" id="btn-screenshot"></i>
                    </div>

                    <div class="flex items-center gap-3 text-gray-400">
                        <span class="text-xxs">1/2</span>
                        <i class="ph ph-wrench hover:text-white cursor-pointer"></i>
                        <span class="font-mono" id="sequence-duration">00:00:00:00</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- BOTTOM HALF -->
    <div class="flex flex-1 min-h-0">

        <!-- BOTTOM LEFT: Media Explorer -->
        <div class="w-[28%] flex flex-col panel-bg border-r panel-border" id="media-panel">
            <div class="flex items-center gap-4 px-3 py-2 border-b border-[#2a2a2a] bg-[#1a1a1a]">
                <span class="tab-active font-medium cursor-pointer">Project <i class="ph ph-list ml-1 text-gray-500"></i></span>
                <span class="text-gray-400 cursor-pointer" id="tab-explorer">Explorer</span>
            </div>

            <div class="p-2 flex justify-between items-center border-b border-[#2a2a2a]">
                <div class="flex gap-2">
                    <i class="ph ph-magnifying-glass text-gray-400"></i>
                </div>
                <div class="flex gap-2 text-gray-400">
                    <i class="ph ph-list cursor-pointer hover:text-white"></i>
                    <i class="ph ph-grid-four cursor-pointer text-white"></i>
                    <span class="text-xxs ml-2" id="media-count">0 item(s)</span>
                </div>
            </div>

            <div class="flex-1 p-2 flex gap-2 overflow-auto items-start content-start flex-wrap relative transition-colors duration-200" id="media-pool">
                <div class="absolute inset-0 flex items-center justify-center text-gray-600 pointer-events-none border-2 border-transparent z-0" id="drag-overlay">
                    <div class="text-center flex flex-col items-center">
                        <i class="ph ph-download-simple text-3xl mb-2"></i>
                        <span>Drop files here</span>
                    </div>
                </div>
            </div>

            <div class="h-8 border-t border-[#2a2a2a] flex items-center px-2 gap-3 text-gray-400 text-lg">
                <i class="ph ph-magnifying-glass hover:text-white cursor-pointer text-sm"></i>
                <i class="ph ph-folder hover:text-white cursor-pointer text-sm"></i>
                <i class="ph ph-file-plus hover:text-white cursor-pointer text-sm" id="btn-import"></i>
                <i class="ph ph-trash hover:text-white cursor-pointer text-sm ml-auto"></i>
            </div>
        </div>

        <!-- TOOLBAR -->
        <div class="w-10 flex flex-col items-center py-2 panel-bg border-r panel-border gap-3 text-gray-400 shrink-0" id="tools-panel">
            <i class="ph-fill ph-cursor text-white hover:text-white cursor-pointer tool-active" data-tool="selection" title="Selection Tool (V)"></i>
            <i class="ph ph-arrows-right hover:text-white cursor-pointer" data-tool="track"></i>
            <i class="ph ph-scissors hover:text-white cursor-pointer" data-tool="ripple"></i>
            <i class="ph-fill ph-knife hover:text-white cursor-pointer" data-tool="razor" title="Razor Tool (C)"></i>
            <i class="ph ph-corners-out hover:text-white cursor-pointer" data-tool="slip"></i>
            <i class="ph-fill ph-pen-nib hover:text-white cursor-pointer" data-tool="pen"></i>
            <i class="ph-fill ph-hand-palm hover:text-white cursor-pointer" data-tool="hand"></i>
            <i class="ph ph-text-t hover:text-white cursor-pointer" data-tool="text"></i>
        </div>

        <!-- BOTTOM RIGHT: Timeline -->
        <div class="flex-1 flex flex-col panel-bg relative overflow-hidden">

            <!-- Timeline Header (Ruler) -->
            <div class="h-8 border-b border-[#2a2a2a] flex relative pl-40 bg-[#1e1e1e]" id="timeline-header">
                <div class="absolute left-0 top-0 w-40 h-full border-r border-[#2a2a2a] flex items-center px-2 justify-between z-30 bg-[#1e1e1e]">
                    <span class="text-[#2d8ceb] font-mono text-xs" id="ruler-tc">00:00:00:00</span>
                    <div class="flex gap-1 text-gray-400">
                        <i class="ph ph-wrench text-xs"></i>
                        <i class="ph ph-magnet text-xs text-blue-500"></i>
                    </div>
                </div>

                <div class="flex-1 relative overflow-hidden flex items-end cursor-text" id="time-ruler">
                    <div class="w-[2000px] flex justify-between px-2 text-[9px] text-gray-500 font-mono pb-0.5 select-none pointer-events-none" id="ruler-marks">
                    </div>
                    <div class="absolute bottom-0 -ml-[7px] w-0 h-0 border-l-[7px] border-r-[7px] border-t-[9px] border-l-transparent border-r-transparent border-t-[#2d8ceb] z-20 cursor-ew-resize" id="playhead-head" style="left: 100px;"></div>
                </div>
            </div>

            <!-- Tracks Area -->
            <div class="flex-1 flex overflow-auto relative bg-[#181818]" id="timeline-container">
                <div class="absolute top-0 bottom-0 w-[1px] bg-[#2d8ceb] z-40 pointer-events-none" id="playhead-line" style="left: 260px;"></div>
                <div class="razor-line" id="razor-guide"></div>

                <!-- Track headers -->
                <div class="w-40 shrink-0 bg-[#252525] flex flex-col border-r border-[#2a2a2a] z-30 sticky left-0">
                    <div class="h-8 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">V3</div></div>
                    <div class="h-8 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">V2</div></div>
                    <div class="h-8 border-b border-[#111] flex items-center px-2 gap-2 text-gray-200 bg-[#353b48]">
                        <div class="w-5 h-5 bg-[#2d8ceb] rounded-sm flex items-center justify-center text-[10px] font-bold text-white">V1</div>
                        <div class="w-5 text-center text-[10px] font-bold">V1</div>
                    </div>
                    <div class="h-2 bg-[#1a1a1a] border-b border-[#111]"></div>
                    <div class="h-10 border-b border-[#111] flex items-center px-2 gap-2 text-gray-200 bg-[#353b48]">
                        <div class="w-5 h-5 bg-[#2d8ceb] rounded-sm flex items-center justify-center text-[10px] font-bold text-white">A1</div>
                        <div class="w-5 text-center text-[10px] font-bold">A1</div>
                    </div>
                    <div class="h-10 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">A2</div></div>
                    <div class="h-10 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">A3</div></div>
                    <div class="flex-1 bg-[#1a1a1a]"></div>
                </div>

                <!-- Track content -->
                <div class="flex flex-col min-w-[2000px] relative w-full" id="tracks-content">
                    <div class="h-8 border-b border-[#252525] relative track" data-track="V3"></div>
                    <div class="h-8 border-b border-[#252525] relative track" data-track="V2"></div>
                    <div class="h-8 border-b border-[#2a2a2a] bg-[#2a2a2a]/20 relative track" data-track="V1" id="track-V1"></div>

                    <div class="h-2"></div>

                    <div class="h-10 border-b border-[#2a2a2a] bg-[#2a2a2a]/20 relative track" data-track="A1" id="track-A1"></div>
                    <div class="h-10 border-b border-[#252525] relative track" data-track="A2"></div>
                    <div class="h-10 border-b border-[#252525] relative track" data-track="A3"></div>
                </div>
            </div>

            <div class="h-4 bg-[#1a1a1a] border-t border-[#2a2a2a] flex items-center px-40">
                <div class="w-1/3 h-2 bg-[#444] rounded-full mx-2 cursor-pointer hover:bg-[#555]"></div>
            </div>
        </div>

        <!-- Audio meters -->
        <div class="w-12 panel-bg border-l panel-border flex flex-col pb-4 shrink-0">
            <div class="flex-1 flex justify-center gap-1 pt-6 pb-2 px-1 relative">
                <div class="absolute inset-y-0 right-1 py-6 flex flex-col justify-between text-[8px] text-gray-500 font-mono text-right z-10 pointer-events-none">
                    <span>0</span><span>-12</span><span>-24</span><span>-36</span><span>-48</span>
                </div>
                <div class="w-2.5 bg-[#111] rounded-t-sm border border-[#222] relative overflow-hidden flex flex-col justify-end"><div class="w-full h-[65%] audio-meter" id="meter-l"></div></div>
                <div class="w-2.5 bg-[#111] rounded-t-sm border border-[#222] relative overflow-hidden flex flex-col justify-end"><div class="w-full h-[60%] audio-meter" id="meter-r"></div></div>
            </div>
        </div>

    </div>
</main>

<style>
  /* Base */
  #app-body {
    font-family: 'Inter', sans-serif;
    background-color: #111111;
    color: #d1d5db;
    user-select: none;
  }

  /* Scrollbars */
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: #1e1e1e; border-left: 1px solid #000; border-top: 1px solid #000; }
  ::-webkit-scrollbar-thumb { background: #3a3a3a; border-radius: 5px; border: 2px solid #1e1e1e; }
  ::-webkit-scrollbar-thumb:hover { background: #4a4a4a; }

  /* UI base */
  .panel-bg { background-color: #1e1e1e; }
  .panel-border { border-color: #000000; border-width: 1px; }
  .tab-active { color: #ffffff; position: relative; }
  .tab-active::after { content: ''; position: absolute; bottom: -6px; left: 0; width: 100%; height: 2px; background-color: #2d8ceb; }
  .text-xxs { font-size: 0.65rem; line-height: 1rem; }

  /* Audio meter */
  .audio-meter {
    background: linear-gradient(to top, #00ff00 0%, #00ff00 75%, #ffff00 75%, #ffff00 90%, #ff0000 90%, #ff0000 100%);
  }

  /* Drag highlight */
  .drag-over { background-color: #2a2a2a !important; border: 2px dashed #2d8ceb !important; }

  /* Clips & tools */
  .clip { transition: filter 0.1s; position: absolute; height: calc(100% - 2px); top: 1px; display: flex; align-items: center; padding: 0 4px; overflow: hidden; border-radius: 2px; }
  .clip:hover { filter: brightness(1.2); }
  .clip.audio { background-color: #1f6a43; border: 1px solid #339e66; }
  .clip.video { background-color: #5d30a6; border: 1px solid #a178e6; }
  .clip.image { background-color: #1d4ed8; border: 1px solid #60a5fa; }

  .tool-active { color: #2d8ceb !important; }

  /* Cursors */
  .cursor-razor { cursor: crosshair !important; }
  .cursor-select { cursor: default !important; }
  .razor-line { position: absolute; top: 0; bottom: 0; width: 1px; background: red; pointer-events: none; z-index: 50; display: none; }

  /* Hide preview timestamp overlay without changing UI markup */
  #preview-timecode { display: none !important; }

  /* Remove Explorer tab without changing UI layout */
  #tab-explorer { display: none !important; }

  /* Completely hide Gradio bridges without removing them from DOM */
  #nle-bridge-host { position: fixed; left: -10000px; top: -10000px; width: 1px; height: 1px; overflow: hidden; opacity: 0; pointer-events: none; }
</style>
"""

        # Secure JS injection
        ui_body_js = json.dumps(UI_BODY_HTML)

        js = rf"""
function() {{
  const UI_BODY_HTML = {ui_body_js};

  // ---- utilities ----
  function $(sel, root=document) {{ return root.querySelector(sel); }}

  async function loadCssOnce(href, id) {{
    if (document.getElementById(id)) return;
    const link = document.createElement("link");
    link.id = id;
    link.rel = "stylesheet";
    link.href = href;
    document.head.appendChild(link);
  }}

  async function loadScriptOnce(src, id) {{
    if (document.getElementById(id)) return;
    await new Promise((resolve, reject) => {{
      const s = document.createElement("script");
      s.id = id;
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    }});
  }}

  function safeParse(s) {{
    try {{ return JSON.parse(s || "{{}}"); }} catch(e) {{ return null; }}
  }}

  function sendCmd(cmdEl, obj) {{
    cmdEl.value = JSON.stringify(obj);
    cmdEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
  }}

  function frameToTimecode(frame, fps) {{
    const f = Math.max(0, Math.round(frame));
    const fpsI = Math.max(1, Math.round(fps));
    const ff = f % fpsI;
    const totalSeconds = Math.floor(f / fpsI);
    const ss = totalSeconds % 60;
    const totalMinutes = Math.floor(totalSeconds / 60);
    const mm = totalMinutes % 60;
    const hh = Math.floor(totalMinutes / 60);
    const pad2 = (n) => String(n).padStart(2, "0");
    return `${{pad2(hh)}}:${{pad2(mm)}}:${{pad2(ss)}}:${{pad2(ff)}}`;
  }}

  // ---- mount + assets + init ----
  async function ensureAssets() {{
    await loadCssOnce(
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap",
      "nle-inter-font"
    );
    await loadScriptOnce("https://cdn.tailwindcss.com", "nle-tailwind-v3");
    await loadCssOnce(
      "https://cdn.jsdelivr.net/npm/@phosphor-icons/web@2.1.2/src/regular/style.css",
      "nle-phosphor-regular"
    );
    await loadCssOnce(
      "https://cdn.jsdelivr.net/npm/@phosphor-icons/web@2.1.2/src/fill/style.css",
      "nle-phosphor-fill"
    );
  }}

  function mountUI() {{
    const mount = document.getElementById("nle-mount");
    if (!mount) return false;
    if (mount.dataset.mounted === "1") return true;

    mount.innerHTML = UI_BODY_HTML;
    mount.dataset.mounted = "1";
    return true;
  }}

  function appInit() {{
    const projEl = $("#te-project-json textarea") || $("#te-project-json input") || $("#te-project-json");
    const cmdEl  = $("#te-cmd-json textarea") || $("#te-cmd-json input") || $("#te-cmd-json");
    const prevEl = $("#te-preview-uri textarea") || $("#te-preview-uri input") || $("#te-preview-uri");
    if (!projEl || !cmdEl || !prevEl) return;

    const body = $("#app-body") || document.body;
    const toolsPanel = $("#tools-panel");
    const tracksContent = $("#tracks-content");
    const ruler = $("#time-ruler");
    const playheadHead = $("#playhead-head");
    const playheadLine = $("#playhead-line");
    const mediaPool = $("#media-pool");
    const dragOverlay = $("#drag-overlay");
    const razorGuide = $("#razor-guide");
    const effectPanel = $("#effect-panel");
    const mainTimecode = $("#main-timecode");
    const rulerTimecode = $("#ruler-tc");
    const programPreview = $("#program-preview");
    const btnImport = $("#btn-import");
    const hiddenFileInput = $("#nle-upload input[type='file']") || $("#nle-upload input");

    // Transport buttons
    const btnHome = $("#btn-home");
    const btnPlay = $("#btn-play");
    const btnEnd = $("#btn-end");
    const btnScreenshot = $("#btn-screenshot");

    const ui = {{
      activeTool: "selection",
      dragging: null,
    }};

    // Playback state
    let playing = false;
    let rafId = null;
    let lastTs = performance.now();
    let accumMs = 0;
    let lastBackendSyncTs = 0;

    function getMaxEndFrame(p) {{
      let maxEnd = 0;
      (p.clips || []).forEach(c => {{
        const dur = Math.max(1, (c.out_f - c.in_f));
        maxEnd = Math.max(maxEnd, (c.start_f || 0) + dur);
      }});
      return maxEnd;
    }}

    function playbackLoop(ts) {{
      if (!playing) return;
      rafId = requestAnimationFrame(playbackLoop);

      const dt = ts - lastTs;
      lastTs = ts;
      // Cap dt so returning to tab doesn't jump forward crazily
      accumMs += Math.min(dt, 100);

      const p = safeParse(projEl.value);
      if (!p) return;

      const fps = p.fps || 25.0;
      const frameMs = 1000 / fps;
      const maxEnd = getMaxEndFrame(p);
      let updated = false;

      while (accumMs >= frameMs) {{
        accumMs -= frameMs;
        if (p.playhead_f < maxEnd) {{
          p.playhead_f++;
          updated = true;
        }} else {{
          playing = false;
          if (btnPlay) btnPlay.classList.replace("ph-pause", "ph-play");
          break;
        }}
      }}

      if (updated) {{
        const newRaw = JSON.stringify(p);
        projEl.value = newRaw;
        lastProjRaw = newRaw; // Prevent interval from doing a full DOM rebuild of the timeline

        const tc = frameToTimecode(p.playhead_f, fps);
        if (mainTimecode) mainTimecode.innerText = tc;
        if (rulerTimecode) rulerTimecode.innerText = tc;

        const ppf = p.px_per_frame || 2.0;
        const playX = Math.max(0, Math.round(p.playhead_f * ppf));
        if (playheadHead) playheadHead.style.left = `${{playX}}px`;
        if (playheadLine) playheadLine.style.left = `${{playX + 160}}px`;

        if (ts - lastBackendSyncTs > 120) {{
          lastBackendSyncTs = ts;
          sendCmd(cmdEl, {{ type: "SET_PLAYHEAD", frame: p.playhead_f }});
        }}
      }}
    }}

    function togglePlay() {{
      playing = !playing;
      if (playing) {{
        if (btnPlay) btnPlay.classList.replace("ph-play", "ph-pause");
        const p = safeParse(projEl.value);
        const maxEnd = p ? getMaxEndFrame(p) : 0;
        if (p && p.playhead_f >= maxEnd) {{
          p.playhead_f = 0;
          const newRaw = JSON.stringify(p);
          projEl.value = newRaw;
          lastProjRaw = newRaw;
        }}
        lastTs = performance.now();
        accumMs = 0;
        rafId = requestAnimationFrame(playbackLoop);
      }} else {{
        if (btnPlay) btnPlay.classList.replace("ph-pause", "ph-play");
        if (rafId) cancelAnimationFrame(rafId);
        const p = safeParse(projEl.value);
        if (p) sendCmd(cmdEl, {{ type: "SET_PLAYHEAD", frame: p.playhead_f }});
      }}
    }}

    if (btnPlay) btnPlay.addEventListener("click", () => togglePlay());

    if (btnHome) {{
      btnHome.addEventListener("click", () => {{
        if (playing) togglePlay();
        sendCmd(cmdEl, {{ type: "SET_PLAYHEAD", frame: 0 }});
      }});
    }}

    if (btnEnd) {{
      btnEnd.addEventListener("click", () => {{
        if (playing) togglePlay();
        const p = safeParse(projEl.value);
        if (p) sendCmd(cmdEl, {{ type: "SET_PLAYHEAD", frame: getMaxEndFrame(p) }});
      }});
    }}

    if (btnScreenshot) {{
      btnScreenshot.addEventListener("click", () => {{
        sendCmd(cmdEl, {{ type: "SCREENSHOT" }});
        const toast = document.createElement("div");
        toast.innerText = "Screenshot captured! (Downloading...)";
        toast.className = "absolute top-4 left-1/2 -translate-x-1/2 bg-[#2d8ceb]/90 text-white px-3 py-1 rounded text-xs shadow-lg z-50 transition-opacity duration-500 pointer-events-none";
        const pm = $("#program-preview")?.parentElement;
        if (pm) {{
          pm.appendChild(toast);
          setTimeout(() => toast.style.opacity = "0", 2000);
          setTimeout(() => toast.remove(), 2500);
        }}
      }});
    }}

    function setCursor() {{
      if (!body) return;
      if (ui.activeTool === "razor") {{
        body.classList.remove("cursor-select");
        body.classList.add("cursor-razor");
      }} else {{
        body.classList.remove("cursor-razor");
        body.classList.add("cursor-select");
        if (razorGuide) razorGuide.style.display = "none";
      }}
    }}

    function renderRulerMarks() {{
      const marks = $("#ruler-marks");
      if (!marks) return;
      if (marks.dataset.built === "1") return;
      for (let i = 0; i < 30; i++) {{
        const span = document.createElement("span");
        span.innerText = `00:00:${{String(i).padStart(2,"0")}}:00`;
        marks.appendChild(span);
      }}
      marks.dataset.built = "1";
    }}

    function renderMediaPool(p) {{
      if (!mediaPool || !dragOverlay) return;
      mediaPool.innerHTML = "";
      mediaPool.appendChild(dragOverlay);

      const media = p.media || [];
      const mc = $("#media-count");
      if (mc) mc.innerText = `${{media.length}} item(s)`;

      if (media.length > 0) dragOverlay.style.display = "none";
      else dragOverlay.style.display = "flex";

      media.forEach(item => {{
        const el = document.createElement("div");
        el.className = "w-24 flex flex-col gap-1 cursor-pointer p-1 rounded-sm hover:bg-[#2a2a2a] group";
        el.draggable = true;
        el.dataset.mediaId = item.id;

        const isAudio = item.kind === "audio";
        const isImage = item.kind === "image";
        const icon = isAudio ? "ph-speaker-high" : isImage ? "ph-image" : "ph-film-strip";
        const color = isAudio ? "text-[#339e66]" : "text-[#2d8ceb]";
        const dur = (item.duration_s != null) ? `${{Number(item.duration_s).toFixed(1)}}s` : "00:00";

        el.innerHTML = `
          <div class="relative w-full h-14 bg-black flex items-center justify-center overflow-hidden rounded-sm border border-[#333] group-hover:border-[#555]">
            <i class="ph ${{icon}} text-2xl ${{color}} opacity-50"></i>
            <div class="absolute bottom-0 right-0 bg-black/80 px-1 text-[9px] font-mono flex items-center gap-1 text-gray-300">
              ${{dur}}
            </div>
          </div>
          <span class="text-[9px] text-gray-300 truncate px-1" title="${{item.name}}">${{item.name}}</span>
        `;

        el.addEventListener("dragstart", (e) => {{
          e.dataTransfer.setData("application/x-wan2gp-media-id", item.id);
          e.dataTransfer.setData("text/plain", item.id);
          e.dataTransfer.effectAllowed = "copy";
        }});

        mediaPool.appendChild(el);
      }});
    }}

    function renderTimeline(p) {{
      renderRulerMarks();

      const fps = p.fps || 25.0;
      const ppf = p.px_per_frame || 2.0;

      const tc = frameToTimecode(p.playhead_f || 0, fps);
      if (mainTimecode) mainTimecode.innerText = tc;
      if (rulerTimecode) rulerTimecode.innerText = tc;

      const seqDurEl = $("#sequence-duration");
      if (seqDurEl) {{
        seqDurEl.innerText = frameToTimecode(getMaxEndFrame(p), fps);
      }}

      const playX = Math.max(0, Math.round((p.playhead_f || 0) * ppf));
      if (playheadHead) playheadHead.style.left = `${{playX}}px`;
      if (playheadLine) playheadLine.style.left = `${{playX + 160}}px`;

      document.querySelectorAll(".track").forEach(track => track.innerHTML = "");

      const clips = p.clips || [];
      clips.forEach(c => {{
        const track = document.querySelector(`.track[data-track="${{c.track_id}}"]`);
        if (!track) return;

        const durF = Math.max(1, (c.out_f - c.in_f));
        const leftPx = Math.max(0, Math.round((c.start_f || 0) * ppf));
        const widthPx = Math.max(6, Math.round(durF * ppf));

        const clipEl = document.createElement("div");
        clipEl.className = `clip ${{c.kind}} z-10`;
        clipEl.style.left = `${{leftPx}}px`;
        clipEl.style.width = `${{widthPx}}px`;
        clipEl.dataset.clipId = c.id;

        clipEl.innerHTML = `
          <span class="text-white text-[10px] truncate whitespace-nowrap drop-shadow-md pointer-events-none select-none px-1">
            ${{(findMediaName(p, c.media_id) || c.id)}}
          </span>
        `;

        clipEl.addEventListener("mousedown", (e) => {{
          if (playing) togglePlay();
          e.stopPropagation();
          sendCmd(cmdEl, {{ type: "SELECT_CLIP", clip_id: c.id }});

          if (ui.activeTool === "selection") {{
            ui.dragging = {{
              clipId: c.id,
              startX: e.clientX,
              startLeftPx: leftPx,
            }};
            clipEl.style.zIndex = "50";
          }} else if (ui.activeTool === "razor") {{
            const rect = clipEl.getBoundingClientRect();
            const cutPx = e.clientX - rect.left;
            if (cutPx < 5 || cutPx > widthPx - 5) return;
            const cutOffF = Math.max(1, Math.min(durF - 1, Math.round(cutPx / ppf)));
            sendCmd(cmdEl, {{ type: "RAZOR_CUT", clip_id: c.id, cut_offset_f: cutOffF }});
            if (razorGuide) razorGuide.style.display = "none";
          }}
        }});

        clipEl.addEventListener("mousemove", (e) => {{
          if (ui.activeTool !== "razor") return;
          if (!razorGuide || !tracksContent) return;
          const tracksRect = tracksContent.getBoundingClientRect();
          const relX = e.clientX - tracksRect.left;
          razorGuide.style.display = "block";
          razorGuide.style.left = `${{relX}}px`;
        }});

        clipEl.addEventListener("mouseleave", () => {{
          if (razorGuide) razorGuide.style.display = "none";
        }});

        track.appendChild(clipEl);
      }});

      // Enable drop media onto tracks
      document.querySelectorAll(".track").forEach(track => {{
        track.ondragover = (e) => {{
          e.preventDefault();
          track.classList.add("drag-over");
        }};
        track.ondragleave = (e) => {{
          e.preventDefault();
          track.classList.remove("drag-over");
        }};
        track.ondrop = (e) => {{
          e.preventDefault();
          track.classList.remove("drag-over");
          
          const mediaId = e.dataTransfer.getData("application/x-wan2gp-media-id") || e.dataTransfer.getData("text/plain");
          if (!mediaId) return;

          const timelineContainer = $("#timeline-container");
          const scrollLeft = timelineContainer ? timelineContainer.scrollLeft : 0;
          const rect = track.getBoundingClientRect();
          
          const x = (e.clientX - rect.left) + scrollLeft;
          const startF = Math.max(0, Math.round(x / ppf));
          const trackId = track.dataset.track || "V1";
          
          sendCmd(cmdEl, {{ type: "ADD_CLIP", media_id: mediaId, track_id: trackId, start_f: startF }});
        }};
      }});
    }}

    function findMediaName(p, mediaId) {{
      const m = (p.media || []).find(x => x.id === mediaId);
      return m ? m.name : "";
    }}

    function renderEffectControls(p) {{
      if (!effectPanel) return;
      const c = (p.clips || []).find(x => x.id === p.selected_clip_id);
      if (!c) {{
        effectPanel.innerHTML = `<span class="text-gray-500">(Select a clip to view FFmpeg parameters)</span>`;
        return;
      }}
      const m = (p.media || []).find(x => x.id === c.media_id);
      const name = m ? m.name : c.id;
      effectPanel.innerHTML = `
        <div class="text-white font-medium mb-2 border-b border-[#333] pb-1">${{name}}</div>
        <div class="flex flex-col gap-1 mb-3">
          <div class="flex justify-between text-gray-400"><span>Scale</span> <span class="text-blue-400">100.0</span></div>
          <div class="flex justify-between text-gray-400"><span>Position</span> <span class="text-blue-400">960.0, 540.0</span></div>
          <div class="flex justify-between text-gray-400"><span>Opacity</span> <span class="text-blue-400">100%</span></div>
        </div>
        <div class="text-gray-500 mt-2 text-[9px] font-mono bg-[#111] p-2 rounded border border-[#222]">
          > ffmpeg -i input -vf "scale=iw*1:ih*1" output
        </div>
      `;
    }}

    if (toolsPanel) {{
      toolsPanel.addEventListener("click", (e) => {{
        const icon = e.target.closest("i");
        if (!icon) return;
        const tool = icon.dataset.tool;
        if (tool !== "selection" && tool !== "razor") return;

        ui.activeTool = tool;
        toolsPanel.querySelectorAll("i").forEach(i => i.classList.remove("tool-active", "text-white"));
        icon.classList.add("tool-active", "text-white");
        setCursor();
      }});
    }}

    if (ruler) {{
      ruler.addEventListener("mousedown", (e) => {{
        if (playing) togglePlay();
        const p = safeParse(projEl.value);
        if (!p) return;
        const rect = ruler.getBoundingClientRect();
        const ppf = p.px_per_frame || 2.0;

        const setFromX = (x) => {{
          const frame = Math.max(0, Math.round(x / ppf));
          sendCmd(cmdEl, {{ type: "SET_PLAYHEAD", frame: frame }});
        }};

        setFromX(e.clientX - rect.left);

        const move = (ev) => setFromX(ev.clientX - rect.left);
        const up = () => {{
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
        }};
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      }});
    }}

    document.addEventListener("mousemove", (e) => {{
      if (!ui.dragging) return;
      const p = safeParse(projEl.value);
      if (!p) return;

      const dx = e.clientX - ui.dragging.startX;
      const newLeft = Math.max(0, ui.dragging.startLeftPx + dx);

      const el = document.querySelector(`.clip[data-clip-id="${{ui.dragging.clipId}}"]`);
      if (el) el.style.left = `${{newLeft}}px`;
    }});

    document.addEventListener("mouseup", (e) => {{
      if (!ui.dragging) return;

      const p = safeParse(projEl.value);
      if (!p) {{
        ui.dragging = null;
        return;
      }}

      const dx = e.clientX - ui.dragging.startX;
      const newLeft = Math.max(0, ui.dragging.startLeftPx + dx);
      const ppf = p.px_per_frame || 2.0;
      const newStartF = Math.max(0, Math.round(newLeft / ppf));

      let newTrack = null;
      const elUnder = document.elementFromPoint(e.clientX, e.clientY);
      if (elUnder) {{
        const trackEl = elUnder.closest(".track");
        if (trackEl && trackEl.dataset.track) newTrack = trackEl.dataset.track;
      }}

      sendCmd(cmdEl, {{
        type: "MOVE_CLIP",
        clip_id: ui.dragging.clipId,
        start_f: newStartF,
        track_id: newTrack
      }});

      const el = document.querySelector(`.clip[data-clip-id="${{ui.dragging.clipId}}"]`);
      if (el) el.style.zIndex = "10";

      ui.dragging = null;
    }});

    // Import behavior
    if (btnImport && hiddenFileInput) {{
      btnImport.addEventListener("click", (e) => {{
        e.preventDefault();
        e.stopPropagation();
        hiddenFileInput.click();
      }});
    }}

    if (mediaPool && hiddenFileInput) {{
      mediaPool.addEventListener("click", (e) => {{
        if (e.target.closest("[data-media-id]")) return;
        hiddenFileInput.click();
      }});
    }}

    // State sync loop
    let lastProjRaw = null;
    let lastPrevUri = null;
    let lastScreenshotHref = null;

    setInterval(() => {{
      // Update from backend json only if not currently managing playhead smoothly via RAF
      if (projEl && projEl.value !== lastProjRaw) {{
        lastProjRaw = projEl.value;
        const p = safeParse(lastProjRaw);
        if (p && !playing) {{
          renderMediaPool(p);
          renderTimeline(p);
          renderEffectControls(p);
        }}
      }}
      
      if (prevEl && prevEl.value !== lastPrevUri) {{
        lastPrevUri = prevEl.value;
        const uri = lastPrevUri || "";
        if (programPreview) {{
          if (uri.startsWith("data:image/")) {{
            programPreview.src = uri;
            programPreview.style.opacity = "1";
            programPreview.classList.remove("opacity-0");
          }} else {{
            programPreview.src = "";
            programPreview.style.opacity = "0";
            programPreview.classList.add("opacity-0");
          }}
        }}
      }}

      // Check for invisible automatic download (screenshot requested from backend)
      const fileLink = document.querySelector("#te-screenshot-file a");
      if (fileLink && fileLink.href && fileLink.href !== lastScreenshotHref) {{
        lastScreenshotHref = fileLink.href;
        fileLink.click();
      }}

    }}, 100);

    // Initial render bindings (fallback)
    projEl.addEventListener("input", () => {{
      const p = safeParse(projEl.value);
      if (!p) return;
      if (!playing) {{
        renderMediaPool(p);
        renderTimeline(p);
        renderEffectControls(p);
      }}
    }});

    prevEl.addEventListener("input", () => {{
      const uri = prevEl.value || "";
      if (programPreview && uri.startsWith("data:image/")) {{
        programPreview.src = uri;
        programPreview.style.opacity = "1";
      }}
    }});

    const p0 = safeParse(projEl.value);
    if (p0) {{
      setCursor();
      renderMediaPool(p0);
      renderTimeline(p0);
      renderEffectControls(p0);
    }}
  }}

  async function init() {{
    const mountOk = mountUI();
    if (!mountOk) return;

    await ensureAssets();

    const mount = document.getElementById("nle-mount");
    if (mount && mount.dataset.inited === "1") return;
    if (mount) mount.dataset.inited = "1";

    appInit();
  }}

  init();
  const obs = new MutationObserver(() => init());
  obs.observe(document.body, {{ childList: true, subtree: true }});
}}
"""

        with gr.Blocks() as root:
            gr.HTML(mount_container)

            # Hidden bridges inside a specific group
            with gr.Group(elem_id="nle-bridge-host"):
                project_json = gr.Textbox(value=dumps_project(default_project()), elem_id="te-project-json")
                cmd_json = gr.Textbox(value="", elem_id="te-cmd-json")
                preview_uri = gr.Textbox(value="", elem_id="te-preview-uri")
                uploader = gr.File(label="Uploader", file_count="multiple", type="filepath", elem_id="nle-upload")
                screenshot_file = gr.File(label="Screenshot", visible=False, elem_id="te-screenshot-file")

            root.load(fn=None, js=js)

            def _detect_kind(path: str) -> str:
                hv = getattr(self, "has_video_file_extension", None)
                hi = getattr(self, "has_image_file_extension", None)
                ha = getattr(self, "has_audio_file_extension", None)
                if callable(hv) and hv(path):
                    return "video"
                if callable(hi) and hi(path):
                    return "image"
                if callable(ha) and ha(path):
                    return "audio"
                return "video"

            def _uid() -> str:
                uid_fn = getattr(self, "get_unique_id", None)
                if callable(uid_fn):
                    return str(uid_fn())
                return f"id_{abs(hash(os.urandom(16)))}"

            def on_upload(files, raw_proj: str):
                p = loads_project(raw_proj)
                if not files:
                    return raw_proj, compute_preview_uri(self, p)

                for f in files:
                    path = getattr(f, "name", None) or str(f)
                    name = os.path.basename(path)
                    kind = _detect_kind(path)

                    item = MediaItem(id=_uid(), name=name, path=path, kind=kind)

                    try:
                        if kind == "video":
                            info_fn = getattr(self, "get_video_info", None)
                            if callable(info_fn):
                                info = info_fn(path)
                                if isinstance(info, (list, tuple)) and len(info) >= 4:
                                    fps, _w, _h, frame_count = info[:4]
                                    item.fps = float(fps) if fps else None
                                    item.frames = int(frame_count) if frame_count else None
                                    if item.fps and item.frames is not None:
                                        item.duration_s = float(item.frames) / float(item.fps)

                        elif kind == "image":
                            img = Image.open(path)
                            item.duration_s = 2.0
                            item.frames = int(round(item.duration_s * p.fps))

                        elif kind == "audio":
                            dur = probe_audio_duration_seconds(path)
                            if dur is not None:
                                item.duration_s = float(dur)
                                item.frames = int(round(item.duration_s * p.fps))
                    except Exception:
                        pass

                    p.media.append(item)

                raw2 = dumps_project(p)
                prev = compute_preview_uri(self, p)
                return raw2, prev

            def on_cmd(raw_cmd: str, raw_proj: str):
                p = loads_project(raw_proj)
                screenshot_path = gr.update()

                if not raw_cmd:
                    return raw_proj, compute_preview_uri(self, p), "", screenshot_path

                try:
                    cmd = json.loads(raw_cmd)
                except Exception:
                    return raw_proj, compute_preview_uri(self, p), "", screenshot_path

                t = cmd.get("type")

                if t == "SET_PLAYHEAD":
                    p.playhead_f = max(0, int(cmd.get("frame", 0)))
                
                elif t == "SCREENSHOT":
                    img = _get_preview_image(self, p)
                    if img:
                        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="screenshot_")
                        os.close(tmp_fd)
                        img.save(tmp_path, "PNG")
                        screenshot_path = tmp_path

                elif t == "SELECT_CLIP":
                    p.selected_clip_id = cmd.get("clip_id")

                elif t == "ADD_CLIP":
                    media_id = cmd.get("media_id")
                    track_id = cmd.get("track_id", "V1")
                    start_f = max(0, int(cmd.get("start_f", 0)))

                    m = find_media(p, media_id)
                    if m:
                        if track_id.startswith("A") and m.kind != "audio":
                            pass
                        elif track_id.startswith("V") and m.kind == "audio":
                            pass
                        else:
                            if m.frames is not None:
                                dur_f = max(1, int(m.frames))
                                if m.kind == "video":
                                    dur_f = min(dur_f, int(round(p.fps * 5.0)))
                            else:
                                dur_f = int(round(p.fps * 2.0))

                            clip_id = f"c_{abs(hash(os.urandom(8)))}"
                            c = Clip(
                                id=clip_id,
                                media_id=m.id,
                                track_id=track_id,
                                start_f=start_f,
                                in_f=0,
                                out_f=dur_f,
                                kind=m.kind,
                            )
                            p.clips.append(c)
                            p.selected_clip_id = clip_id

                elif t == "MOVE_CLIP":
                    cid = cmd.get("clip_id")
                    new_start = max(0, int(cmd.get("start_f", 0)))
                    new_track = cmd.get("track_id")

                    for c in p.clips:
                        if c.id == cid:
                            c.start_f = new_start
                            if isinstance(new_track, str) and new_track:
                                if new_track.startswith("V") and c.kind in ("video", "image"):
                                    c.track_id = new_track
                                elif new_track.startswith("A") and c.kind == "audio":
                                    c.track_id = new_track
                            break

                elif t == "RAZOR_CUT":
                    cid = cmd.get("clip_id")
                    cut_off = int(cmd.get("cut_offset_f", 0))

                    target = find_clip(p, cid)
                    if target:
                        dur = clip_duration_frames(target)
                        cut_off = max(1, min(dur - 1, cut_off))

                        first = target
                        second_id = f"{first.id}_b"
                        second = Clip(
                            id=second_id,
                            media_id=first.media_id,
                            track_id=first.track_id,
                            start_f=first.start_f + cut_off,
                            in_f=first.in_f + cut_off,
                            out_f=first.out_f,
                            kind=first.kind,
                        )
                        first.out_f = first.in_f + cut_off

                        p.clips = [c for c in p.clips if c.id != first.id]
                        p.clips.append(first)
                        p.clips.append(second)
                        p.selected_clip_id = first.id

                raw2 = dumps_project(p)
                prev = compute_preview_uri(self, p)
                return raw2, prev, "", screenshot_path

            uploader.upload(
                on_upload,
                inputs=[uploader, project_json],
                outputs=[project_json, preview_uri],
            )

            cmd_json.input(
                on_cmd,
                inputs=[cmd_json, project_json],
                outputs=[project_json, preview_uri, cmd_json, screenshot_file],
            )

        return root


Plugin = TimelineEditorPlugin
