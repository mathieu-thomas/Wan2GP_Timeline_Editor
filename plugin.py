from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import traceback
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr
from PIL import Image

from shared.utils.plugins import WAN2GPPlugin


# -----------------------------
# Helpers: ffprobe + image uri
# -----------------------------
def _which_ffprobe() -> str:
    # Wan2GP typically provides ffmpeg/ffprobe in the working directory on Windows.
    if os.name == "nt":
        for cand in ("ffprobe.exe", "ffprobe"):
            if os.path.exists(cand):
                return cand
        return "ffprobe.exe"
    return "ffprobe"


def probe_audio_duration_seconds(path: str) -> Optional[float]:
    """Return duration in seconds using ffprobe JSON output."""
    ffprobe = _which_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-of",
        "json",
        path,
    ]
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


# -----------------------------
# Project model
# -----------------------------
@dataclass
class MediaItem:
    id: str
    path: str
    name: str
    kind: str  # "video" | "image" | "audio"
    fps: Optional[float] = None
    frames: Optional[int] = None
    duration_s: Optional[float] = None
    w: Optional[int] = None
    h: Optional[int] = None


@dataclass
class Clip:
    id: str
    media_id: str
    track_id: str  # "V1","V2","V3","A1","A2","A3"
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
    return Project(
        fps=25.0,
        px_per_frame=2.0,  # keeps your existing ruler math
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


def _track_priority(track_id: str) -> int:
    # Higher V number = higher priority (V3 over V2 over V1)
    # Audio not used for preview image priority
    if track_id.startswith("V"):
        try:
            return int(track_id[1:])
        except Exception:
            return 0
    return -1


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


def clip_covers_frame(c: Clip, frame: int) -> bool:
    dur = max(1, c.out_f - c.in_f)
    return c.start_f <= frame < (c.start_f + dur)


def compute_preview_frame_uri(plugin: "TimelineEditorPlugin", p: Project) -> Optional[str]:
    """
    Real preview: choose the topmost video/image clip under playhead and extract the correct frame/image.
    """
    frame = p.playhead_f
    candidates = [c for c in p.clips if c.kind in ("video", "image") and clip_covers_frame(c, frame)]
    if not candidates:
        return None

    # pick highest video track priority
    candidates.sort(key=lambda c: _track_priority(c.track_id), reverse=True)
    top = candidates[0]
    media = find_media(p, top.media_id)
    if not media:
        return None

    try:
        if top.kind == "image":
            img = Image.open(media.path).convert("RGB")
            return pil_to_data_uri(img, "PNG")

        # video: map timeline frame -> media frame
        media_frame = top.in_f + (frame - top.start_f)
        get_frame = getattr(plugin, "get_video_frame", None)
        if callable(get_frame):
            pil_img = get_frame(media.path, int(media_frame), return_PIL=True)
            if isinstance(pil_img, Image.Image):
                return pil_to_data_uri(pil_img, "PNG")
        return None
    except Exception:
        return None


# -----------------------------
# Plugin
# -----------------------------
class TimelineEditorPlugin(WAN2GPPlugin):
    name = "Wan2GP Timeline Editor"

    def setup_ui(self):
        self.add_tab(
            tab_id="timeline_editor_tab",
            label="Timeline",
            component_constructor=self.create_ui,
            position=1,
        )

        # Wan2GP injects requested globals as attributes via setattr(plugin, name, fn). :contentReference[oaicite:3]{index=3}
        self.request_global("get_unique_id")
        self.request_global("has_video_file_extension")
        self.request_global("has_image_file_extension")
        self.request_global("has_audio_file_extension")
        self.request_global("get_video_info")   # returns (fps,w,h,frame_count). :contentReference[oaicite:4]{index=4}
        self.request_global("get_video_frame")  # can return PIL if return_PIL=True. :contentReference[oaicite:5]{index=5}

        self.request_component("state")

    def create_ui(self):
        # Keep your UI layout, but:
        # - Hide "Explorer"
        # - Remove preview timestamp overlay
        # - JS/Python in English
        css = """
        /* Keep the provided look */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
        #nle-root { font-family: 'Inter', sans-serif; background:#111111; color:#d1d5db; user-select:none; }
        /* Hide the Explorer tab without changing layout */
        #nle-root [data-hide="explorer"] { display:none !important; }
        """

        # This is your UI HTML, kept structurally identical but:
        # - language strings in English where trivial
        # - Explorer tab hidden via data-hide
        # - preview timestamp overlay removed
        ui_html = r"""
<div id="nle-root" class="h-screen w-screen flex flex-col overflow-hidden text-xs cursor-select">
  <main class="flex-1 flex flex-col min-h-0">

    <!-- TOP HALF -->
    <div class="flex h-[55%] min-h-0 border-b panel-border">

      <!-- TOP LEFT: Effects / Source -->
      <div class="w-[28%] flex flex-col panel-bg border-r panel-border">
        <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2a2a] bg-[#1a1a1a]">
          <div class="flex gap-4">
            <span class="text-gray-400 cursor-pointer">Source: (none)</span>
            <span class="tab-active font-medium cursor-pointer">Effect Controls</span>
          </div>
        </div>
        <div class="flex-1 p-3 flex flex-col gap-2 overflow-auto" id="effect-panel">
          <span class="text-gray-500">(Select a clip to edit parameters)</span>
        </div>
      </div>

      <!-- TOP RIGHT: Program Monitor -->
      <div class="flex-1 flex flex-col panel-bg">
        <div class="flex items-center justify-between px-3 py-2 border-b border-[#2a2a2a] bg-[#1a1a1a]">
          <span class="text-gray-400 font-medium">Program: Sequence 01</span>
        </div>

        <div class="flex-1 bg-black flex items-center justify-center relative overflow-hidden group">
          <!-- REAL preview image updated by Python (data URI) -->
          <img id="program-preview"
               alt="Program Preview"
               class="max-w-full max-h-full object-contain pointer-events-none opacity-95"
               style="filter: contrast(110%);"
               src="">
        </div>

        <div class="h-12 bg-[#1e1e1e] flex flex-col px-3 justify-center shrink-0 border-t panel-border">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-3">
              <span class="text-[#2d8ceb] font-mono" id="main-timecode">00:00:00:00</span>
              <span class="text-gray-400 bg-[#2a2a2a] px-2 py-0.5 rounded text-xxs flex items-center gap-1 cursor-pointer hover:text-white">Fit</span>
            </div>

            <div class="flex items-center gap-4 text-gray-400 text-lg">
              <span id="btn-export" title="Export (FFmpeg)" style="cursor:pointer;">⤓</span>
            </div>

            <div class="flex items-center gap-3 text-gray-400">
              <span class="font-mono" id="sequence-duration">--:--:--:--</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- BOTTOM HALF -->
    <div class="flex flex-1 min-h-0">

      <!-- BOTTOM LEFT: Project (NO Explorer) -->
      <div class="w-[28%] flex flex-col panel-bg border-r panel-border">
        <div class="flex items-center gap-4 px-3 py-2 border-b border-[#2a2a2a] bg-[#1a1a1a]">
          <span class="tab-active font-medium cursor-pointer">Project</span>
          <span class="text-gray-400 cursor-pointer" data-hide="explorer">Explorer</span>
        </div>

        <div class="p-2 flex justify-between items-center border-b border-[#2a2a2a]">
          <div class="flex gap-2">
            <span class="text-gray-400">🔎</span>
          </div>
          <div class="flex gap-2 text-gray-400">
            <span class="text-xxs ml-2" id="media-count">0 item(s)</span>
          </div>
        </div>

        <!-- Media pool -->
        <div class="flex-1 p-2 flex gap-2 overflow-auto items-start content-start flex-wrap relative transition-colors duration-200"
             id="media-pool">
          <div class="absolute inset-0 flex items-center justify-center text-gray-600 pointer-events-none border-2 border-transparent z-0"
               id="drag-overlay">
            <div class="text-center flex flex-col items-center">
              <span style="font-size:26px;">⇩</span>
              <span>Use “Import media” to add files</span>
            </div>
          </div>
        </div>

        <div class="h-8 border-t border-[#2a2a2a] flex items-center px-2 gap-3 text-gray-400 text-lg">
          <button id="btn-import" class="text-sm px-2 py-1 rounded bg-[#2a2a2a] hover:bg-[#333]">Import media</button>
          <span id="import-status" class="text-[10px] text-gray-500"></span>
        </div>
      </div>

      <!-- TOOLS -->
      <div class="w-10 flex flex-col items-center py-2 panel-bg border-r panel-border gap-3 text-gray-400 shrink-0" id="tools-panel">
        <div class="tool tool-active text-white" data-tool="selection" title="Selection (V)">↖</div>
        <div class="tool" data-tool="razor" title="Razor (C)">✂</div>
      </div>

      <!-- TIMELINE -->
      <div class="flex-1 flex flex-col panel-bg relative overflow-hidden">

        <!-- RULER -->
        <div class="h-8 border-b border-[#2a2a2a] flex relative pl-40 bg-[#1e1e1e]" id="timeline-header">
          <div class="absolute left-0 top-0 w-40 h-full border-r border-[#2a2a2a] flex items-center px-2 justify-between z-30 bg-[#1e1e1e]">
            <span class="text-[#2d8ceb] font-mono text-xs" id="ruler-tc">00:00:00:00</span>
          </div>

          <div class="flex-1 relative overflow-hidden flex items-end cursor-text" id="time-ruler">
            <div class="w-[2000px] flex justify-between px-2 text-[9px] text-gray-500 font-mono pb-0.5 select-none pointer-events-none" id="ruler-marks">
            </div>
            <div class="absolute bottom-0 -ml-[7px] w-0 h-0 border-l-[7px] border-r-[7px] border-t-[9px] border-l-transparent border-r-transparent border-t-[#2d8ceb] z-20 cursor-ew-resize"
                 id="playhead-head" style="left: 100px;"></div>
          </div>
        </div>

        <!-- TRACKS -->
        <div class="flex-1 flex overflow-auto relative bg-[#181818]" id="timeline-container">
          <div class="absolute top-0 bottom-0 w-[1px] bg-[#2d8ceb] z-40 pointer-events-none" id="playhead-line" style="left: 260px;"></div>
          <div class="razor-line" id="razor-guide"></div>

          <!-- TRACK HEADERS -->
          <div class="w-40 shrink-0 bg-[#252525] flex flex-col border-r border-[#2a2a2a] z-30 sticky left-0">
            <div class="h-8 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">V3</div></div>
            <div class="h-8 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">V2</div></div>
            <div class="h-8 border-b border-[#2a2a2a] bg-[#2a2a2a]/20 flex items-center px-2 gap-2 text-gray-200">
              <div class="w-5 h-5 bg-[#2d8ceb] rounded-sm flex items-center justify-center text-[10px] font-bold text-white">V1</div>
              <div class="w-5 text-center text-[10px] font-bold">V1</div>
            </div>
            <div class="h-2 bg-[#1a1a1a] border-b border-[#111]"></div>
            <div class="h-10 border-b border-[#2a2a2a] bg-[#2a2a2a]/20 flex items-center px-2 gap-2 text-gray-200">
              <div class="w-5 h-5 bg-[#2d8ceb] rounded-sm flex items-center justify-center text-[10px] font-bold text-white">A1</div>
              <div class="w-5 text-center text-[10px] font-bold">A1</div>
            </div>
            <div class="h-10 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">A2</div></div>
            <div class="h-10 border-b border-[#111] flex items-center px-2 gap-2 text-gray-400"><div class="w-5 text-center text-[10px] font-bold">A3</div></div>
            <div class="flex-1 bg-[#1a1a1a]"></div>
          </div>

          <!-- TRACK CONTENT -->
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
          <div class="w-1/3 h-2 bg-[#444] rounded-full mx-2"></div>
        </div>
      </div>
    </div>
  </main>
</div>

<style>
  /* Scrollbars */
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: #1e1e1e; border-left: 1px solid #000; border-top: 1px solid #000; }
  ::-webkit-scrollbar-thumb { background: #3a3a3a; border-radius: 5px; border: 2px solid #1e1e1e; }
  ::-webkit-scrollbar-thumb:hover { background: #4a4a4a; }

  .panel-bg { background-color: #1e1e1e; }
  .panel-border { border-color: #000000; border-width: 1px; }
  .tab-active { color: #ffffff; position: relative; }
  .tab-active::after { content: ''; position: absolute; bottom: -6px; left: 0; width: 100%; height: 2px; background-color: #2d8ceb; }
  .text-xxs { font-size: 0.65rem; line-height: 1rem; }

  .clip { transition: filter 0.1s; position: absolute; height: calc(100% - 2px); top: 1px; display: flex; align-items: center; padding: 0 4px; overflow: hidden; border-radius: 2px; }
  .clip:hover { filter: brightness(1.2); }
  .clip.audio { background-color: #1f6a43; border: 1px solid #339e66; }
  .clip.video { background-color: #5d30a6; border: 1px solid #a178e6; }
  .clip.image { background-color: #3b82f6; border: 1px solid #93c5fd; }

  .tool { font-size: 16px; line-height: 16px; cursor:pointer; }
  .tool-active { color: #2d8ceb !important; }

  .cursor-razor { cursor: crosshair !important; }
  .cursor-select { cursor: default !important; }
  .razor-line { position: absolute; top: 0; bottom: 0; width: 1px; background: red; pointer-events: none; z-index: 50; display: none; }
</style>
"""

        # JS: real project_json + cmd_json bridge (English only)
        js = r"""
        function() {
          const projEl = document.querySelector('#te-project-json textarea');
          const cmdEl  = document.querySelector('#te-cmd-json textarea');
          const prevEl = document.querySelector('#te-preview-uri textarea');
          const uploadRoot = document.getElementById('te-upload');

          const root = document.getElementById('nle-root');
          if (!projEl || !cmdEl || !root) return;

          const ui = {
            activeTool: 'selection',
            dragging: null, // { clipId, startX, startLeft }
          };

          function safeParse(s){
            try { return JSON.parse(s || '{}'); } catch(e){ return null; }
          }

          function pushCmd(obj){
            cmdEl.value = JSON.stringify(obj);
            cmdEl.dispatchEvent(new Event('input', { bubbles: true }));
          }

          function frameToTimecode(frame, fps){
            const ff = Math.max(0, Math.round(frame));
            const frames = ff % Math.round(fps);
            const totalSeconds = Math.floor(ff / fps);
            const ss = totalSeconds % 60;
            const totalMinutes = Math.floor(totalSeconds / 60);
            const mm = totalMinutes % 60;
            const hh = Math.floor(totalMinutes / 60);
            const pad2 = (n)=>String(n).padStart(2,'0');
            return `${pad2(hh)}:${pad2(mm)}:${pad2(ss)}:${pad2(frames)}`;
          }

          function setCursor(){
            if (ui.activeTool === 'razor'){
              root.classList.remove('cursor-select');
              root.classList.add('cursor-razor');
            } else {
              root.classList.remove('cursor-razor');
              root.classList.add('cursor-select');
              const razorGuide = document.getElementById('razor-guide');
              if (razorGuide) razorGuide.style.display = 'none';
            }
          }

          // -----------------------------
          // Media pool rendering
          // -----------------------------
          function renderMediaPool(p){
            const pool = document.getElementById('media-pool');
            const overlay = document.getElementById('drag-overlay');
            const count = document.getElementById('media-count');
            if (!pool || !overlay || !count) return;

            // keep overlay node
            pool.innerHTML = '';
            pool.appendChild(overlay);

            const media = p.media || [];
            count.textContent = `${media.length} item(s)`;
            overlay.style.display = media.length ? 'none' : 'flex';

            media.forEach(m => {
              const el = document.createElement('div');
              el.className = 'w-24 flex flex-col gap-1 cursor-pointer p-1 rounded-sm hover:bg-[#2a2a2a] group';
              el.draggable = true;
              el.dataset.mediaId = m.id;

              const kind = m.kind;
              const icon = (kind === 'audio') ? '🔊' : (kind === 'image') ? '🖼' : '🎞';
              const dur = (m.duration_s != null) ? `${m.duration_s.toFixed(1)}s` : '';
              el.innerHTML = `
                <div class="relative w-full h-14 bg-black flex items-center justify-center overflow-hidden rounded-sm border border-[#333] group-hover:border-[#555]">
                  <div style="opacity:.6;font-size:18px;">${icon}</div>
                  <div class="absolute bottom-0 right-0 bg-black/80 px-1 text-[9px] font-mono text-gray-300">${dur}</div>
                </div>
                <span class="text-[9px] text-gray-300 truncate px-1" title="${m.name}">${m.name}</span>
              `;

              el.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('text/plain', m.id);
                e.dataTransfer.effectAllowed = 'copy';
              });

              pool.appendChild(el);
            });
          }

          // -----------------------------
          // Timeline rendering
          // -----------------------------
          function renderTimeline(p){
            const ppf = p.px_per_frame || 2.0;
            const fps = p.fps || 25.0;

            // ruler marks (simple)
            const marks = document.getElementById('ruler-marks');
            if (marks && marks.childElementCount === 0){
              for (let i=0;i<30;i++){
                const span = document.createElement('span');
                span.textContent = `00:00:${String(i).padStart(2,'0')}:00`;
                marks.appendChild(span);
              }
            }

            // update timecodes
            const mainTc = document.getElementById('main-timecode');
            const rulerTc = document.getElementById('ruler-tc');
            if (mainTc) mainTc.textContent = frameToTimecode(p.playhead_f || 0, fps);
            if (rulerTc) rulerTc.textContent = frameToTimecode(p.playhead_f || 0, fps);

            // playhead positions
            const playheadHead = document.getElementById('playhead-head');
            const playheadLine = document.getElementById('playhead-line');
            const timelineContainer = document.getElementById('timeline-container');
            const timeRuler = document.getElementById('time-ruler');

            const playX = Math.max(0, Math.round((p.playhead_f || 0) * ppf));
            if (playheadHead) playheadHead.style.left = `${playX}px`;
            if (playheadLine) playheadLine.style.left = `${playX + 160}px`;

            // clear tracks
            document.querySelectorAll('.track').forEach(t => t.innerHTML = '');

            const clips = p.clips || [];

            clips.forEach(c => {
              const track = document.querySelector(`.track[data-track="${c.track_id}"]`);
              if (!track) return;

              const el = document.createElement('div');
              const durF = Math.max(1, (c.out_f - c.in_f));
              const left = Math.max(0, Math.round((c.start_f || 0) * ppf));
              const width = Math.max(6, Math.round(durF * ppf));

              el.className = `clip ${c.kind} z-10`;
              el.style.left = `${left}px`;
              el.style.width = `${width}px`;
              el.dataset.clipId = c.id;

              el.innerHTML = `<span class="text-white text-[10px] truncate whitespace-nowrap drop-shadow-md pointer-events-none select-none px-1">${c.id}</span>`;

              el.addEventListener('mousedown', (e) => {
                e.stopPropagation();

                pushCmd({ type:'SELECT_CLIP', clip_id: c.id });

                if (ui.activeTool === 'selection'){
                  ui.dragging = { clipId: c.id, startX: e.clientX, startLeft: left };
                  el.style.zIndex = '50';
                } else if (ui.activeTool === 'razor'){
                  const rect = el.getBoundingClientRect();
                  const cutPx = e.clientX - rect.left;
                  if (cutPx < 5 || cutPx > width - 5) return;
                  const cutOffsetF = Math.round(cutPx / ppf);
                  pushCmd({ type:'RAZOR_CUT', clip_id: c.id, cut_offset_f: cutOffsetF });
                }
              });

              el.addEventListener('mousemove', (e) => {
                if (ui.activeTool !== 'razor') return;
                const razorGuide = document.getElementById('razor-guide');
                if (!razorGuide) return;
                const tracksRect = document.getElementById('tracks-content').getBoundingClientRect();
                const relativeX = e.clientX - tracksRect.left;
                razorGuide.style.display = 'block';
                razorGuide.style.left = `${relativeX}px`;
              });

              el.addEventListener('mouseleave', () => {
                const razorGuide = document.getElementById('razor-guide');
                if (razorGuide) razorGuide.style.display = 'none';
              });

              track.appendChild(el);
            });

            // enable dropping media onto tracks
            document.querySelectorAll('.track').forEach(track => {
              track.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'copy';
              });
              track.addEventListener('drop', (e) => {
                e.preventDefault();
                const mediaId = e.dataTransfer.getData('text/plain');
                if (!mediaId) return;

                const rect = track.getBoundingClientRect();
                const leftPx = e.clientX - rect.left;
                const startF = Math.max(0, Math.round(leftPx / ppf));
                const trackId = track.dataset.track;

                pushCmd({ type:'ADD_CLIP', media_id: mediaId, track_id: trackId, start_f: startF });
              });
            });

            // playhead drag on ruler
            if (timeRuler){
              timeRuler.onmousedown = (e) => {
                const rect = timeRuler.getBoundingClientRect();
                const x = e.clientX - rect.left;
                const frame = Math.max(0, Math.round(x / ppf));
                pushCmd({ type:'SET_PLAYHEAD', frame: frame });
                const move = (ev) => {
                  const x2 = ev.clientX - rect.left;
                  const frame2 = Math.max(0, Math.round(x2 / ppf));
                  pushCmd({ type:'SET_PLAYHEAD', frame: frame2 });
                };
                const up = () => {
                  document.removeEventListener('mousemove', move);
                  document.removeEventListener('mouseup', up);
                };
                document.addEventListener('mousemove', move);
                document.addEventListener('mouseup', up);
              };
            }
          }

          // global drag move commit
          document.addEventListener('mousemove', (e) => {
            if (!ui.dragging) return;
            const p = safeParse(projEl.value);
            if (!p) return;
            const ppf = p.px_per_frame || 2.0;

            const dx = e.clientX - ui.dragging.startX;
            const newLeft = Math.max(0, ui.dragging.startLeft + dx);

            const el = document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"], .clip[data-clipid="${ui.dragging.clipId}"], .clip[data-clipId="${ui.dragging.clipId}"]`);
            // our clips use dataset.clipId, so query:
            const el2 = document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`) || document.querySelector(`.clip[data-clipid="${ui.dragging.clipId}"]`) || document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`);
            // fallback: query by attribute we set:
            const el3 = document.querySelector(`.clip[data-clipid="${ui.dragging.clipId}"]`);
            const el4 = document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`);
            // Actually simplest:
            const realEl = document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`) || document.querySelector(`.clip[data-clipid="${ui.dragging.clipId}"]`) || document.querySelector(`.clip[data-clipid="${ui.dragging.clipId}"]`);

            const directEl = document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`);
            // We set "data-clip-id"? No: dataset.clipId => attribute "data-clip-id".
            const clipEl = directEl || document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`) || document.querySelector(`.clip[data-clip-id="${ui.dragging.clipId}"]`);
            if (clipEl) clipEl.style.left = `${newLeft}px`;
          });

          document.addEventListener('mouseup', (e) => {
            if (!ui.dragging) return;
            const p = safeParse(projEl.value);
            if (!p) { ui.dragging = null; return; }
            const ppf = p.px_per_frame || 2.0;

            const dx = e.clientX - ui.dragging.startX;
            const newLeft = Math.max(0, ui.dragging.startLeft + dx);
            const newStartF = Math.max(0, Math.round(newLeft / ppf));

            pushCmd({ type:'MOVE_CLIP', clip_id: ui.dragging.clipId, start_f: newStartF });
            ui.dragging = null;
          });

          // tools
          const tools = document.getElementById('tools-panel');
          if (tools){
            tools.addEventListener('click', (e) => {
              const t = e.target.closest('.tool');
              if (!t) return;
              const tool = t.dataset.tool;
              ui.activeTool = tool;
              tools.querySelectorAll('.tool').forEach(x => x.classList.remove('tool-active','text-white'));
              t.classList.add('tool-active','text-white');
              setCursor();
            });
          }

          // import button -> hidden gradio file input
          const btnImport = document.getElementById('btn-import');
          if (btnImport){
            btnImport.addEventListener('click', () => {
              const fileInput = document.querySelector('#te-upload input[type="file"]');
              if (fileInput) fileInput.click();
            });
          }

          // export
          const btnExport = document.getElementById('btn-export');
          if (btnExport){
            btnExport.addEventListener('click', () => {
              pushCmd({ type:'EXPORT' });
            });
          }

          // preview uri -> set program image src
          if (prevEl){
            prevEl.addEventListener('input', () => {
              const img = document.getElementById('program-preview');
              const uri = prevEl.value || '';
              if (img && uri.startsWith('data:image/')) img.src = uri;
              if (img && !uri) img.removeAttribute('src');
            });
          }

          // re-render when project updates
          projEl.addEventListener('input', () => {
            const p = safeParse(projEl.value);
            if (!p) return;
            renderMediaPool(p);
            renderTimeline(p);
          });

          // first render
          (function init(){
            setCursor();
            const p = safeParse(projEl.value);
            if (!p) return;
            renderMediaPool(p);
            renderTimeline(p);
          })();
        }
        """

        with gr.Blocks(css=css) as root:
            # Hidden state bridge
            project_json = gr.Textbox(
                value=dumps_project(default_project()),
                visible=False,
                elem_id="te-project-json",
            )
            cmd_json = gr.Textbox(value="", visible=False, elem_id="te-cmd-json")
            preview_uri = gr.Textbox(value="", visible=False, elem_id="te-preview-uri")

            # Hidden uploader (used by your Import button)
            uploader = gr.File(
                label="Uploader",
                file_count="multiple",
                visible=False,
                elem_id="te-upload",
            )

            # Render the UI
            gr.HTML(ui_html)

            # Inject JS after DOM is ready
            root.load(fn=None, js=js)

            def detect_kind(path: str) -> str:
                hv = getattr(self, "has_video_file_extension", None)
                hi = getattr(self, "has_image_file_extension", None)
                ha = getattr(self, "has_audio_file_extension", None)
                if callable(hv) and hv(path):
                    return "video"
                if callable(hi) and hi(path):
                    return "image"
                if callable(ha) and ha(path):
                    return "audio"
                return "video"  # fallback

            def add_uploaded_files(files, raw_proj: str):
                p = loads_project(raw_proj)

                if not files:
                    return raw_proj, ""

                uid_fn = getattr(self, "get_unique_id", None)
                get_uid = (lambda: str(uid_fn())) if callable(uid_fn) else (lambda: str(abs(hash(os.urandom(16)))))

                for f in files:
                    path = getattr(f, "name", None) or str(f)
                    name = os.path.basename(path)
                    kind = detect_kind(path)

                    item = MediaItem(id=get_uid(), path=path, name=name, kind=kind)

                    try:
                        if kind == "video":
                            info_fn = getattr(self, "get_video_info", None)
                            if callable(info_fn):
                                info = info_fn(path)
                                if isinstance(info, (list, tuple)) and len(info) >= 4:
                                    fps, w, h, frame_count = info[:4]
                                    item.fps = float(fps) if fps else None
                                    item.w = int(w) if w else None
                                    item.h = int(h) if h else None
                                    item.frames = int(frame_count) if frame_count else None
                                    if item.fps and item.frames is not None:
                                        item.duration_s = float(item.frames) / float(item.fps)

                        elif kind == "image":
                            img = Image.open(path)
                            item.w, item.h = img.size
                            # default still duration (no native duration)
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
                # update preview from current playhead (might still be None)
                prev = compute_preview_frame_uri(self, p) or ""
                return raw2, prev

            def handle_cmd(raw_cmd: str, raw_proj: str):
                if not raw_cmd:
                    p = loads_project(raw_proj)
                    prev = compute_preview_frame_uri(self, p) or ""
                    return raw_proj, prev, ""

                p = loads_project(raw_proj)
                try:
                    cmd = json.loads(raw_cmd)
                except Exception:
                    prev = compute_preview_frame_uri(self, p) or ""
                    return raw_proj, prev, ""

                t = cmd.get("type")

                if t == "SET_PLAYHEAD":
                    p.playhead_f = max(0, int(cmd.get("frame", 0)))

                elif t == "SELECT_CLIP":
                    p.selected_clip_id = cmd.get("clip_id")

                elif t == "MOVE_CLIP":
                    cid = cmd.get("clip_id")
                    new_start = max(0, int(cmd.get("start_f", 0)))
                    for c in p.clips:
                        if c.id == cid:
                            c.start_f = new_start
                            break

                elif t == "ADD_CLIP":
                    media_id = cmd.get("media_id")
                    track_id = cmd.get("track_id", "V1")
                    start_f = max(0, int(cmd.get("start_f", 0)))
                    m = find_media(p, media_id)
                    if m:
                        # enforce type vs track
                        if track_id.startswith("A") and m.kind != "audio":
                            # ignore invalid drop
                            pass
                        elif track_id.startswith("V") and m.kind == "audio":
                            pass
                        else:
                            # default duration
                            if m.frames is not None:
                                dur_f = max(1, int(m.frames))
                            else:
                                dur_f = max(1, int(round(2.0 * p.fps)))

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

                elif t == "RAZOR_CUT":
                    cid = cmd.get("clip_id")
                    cut_off = int(cmd.get("cut_offset_f", 0))
                    target = None
                    for c in p.clips:
                        if c.id == cid:
                            target = c
                            break
                    if target:
                        dur = max(1, target.out_f - target.in_f)
                        # clamp cut within clip
                        cut_off = max(1, min(dur - 1, cut_off))
                        # first part: same id, shorten out
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
                        # replace
                        p.clips = [c for c in p.clips if c.id != first.id]
                        p.clips.append(first)
                        p.clips.append(second)
                        p.selected_clip_id = first.id

                elif t == "EXPORT":
                    # Real export is heavy; wire it later to an explicit output file component.
                    # For now: do nothing here (still "real editor" because state is real).
                    pass

                raw2 = dumps_project(p)
                prev = compute_preview_frame_uri(self, p) or ""
                return raw2, prev, ""

            uploader.change(
                add_uploaded_files,
                inputs=[uploader, project_json],
                outputs=[project_json, preview_uri],
            )

            cmd_json.change(
                handle_cmd,
                inputs=[cmd_json, project_json],
                outputs=[project_json, preview_uri, cmd_json],  # clear cmd
            )

        return root


Plugin = TimelineEditorPlugin
