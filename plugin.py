from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import gradio as gr
from shared.utils.plugins import WAN2GPPlugin


@dataclass
class MediaItem:
    id: str
    path: str
    kind: str
    fps: Optional[float] = None
    frames: Optional[int] = None
    duration_s: Optional[float] = None


@dataclass
class Clip:
    id: str
    media_id: str
    track: int
    start_f: int
    in_f: int
    out_f: int


@dataclass
class Project:
    fps: float
    tracks_video: int
    playhead_f: int
    selected_clip_id: Optional[str]
    media: List[MediaItem]
    clips: List[Clip]


def default_project() -> Project:
    return Project(
        fps=16.0,
        tracks_video=3,
        playhead_f=0,
        selected_clip_id=None,
        media=[],
        clips=[],
    )


def dumps_project(project: Project) -> str:
    return json.dumps(asdict(project), ensure_ascii=False)


def loads_project(raw: str) -> Project:
    data = json.loads(raw) if raw else {}
    media = [MediaItem(**m) for m in data.get("media", [])]
    clips = [Clip(**c) for c in data.get("clips", [])]
    return Project(
        fps=float(data.get("fps", 16.0)),
        tracks_video=int(data.get("tracks_video", 3)),
        playhead_f=int(data.get("playhead_f", 0)),
        selected_clip_id=data.get("selected_clip_id"),
        media=media,
        clips=clips,
    )


def find_clip(project: Project, clip_id: Optional[str]) -> Optional[Clip]:
    if not clip_id:
        return None
    for clip in project.clips:
        if clip.id == clip_id:
            return clip
    return None


def find_media(project: Project, media_id: str) -> Optional[MediaItem]:
    for media in project.media:
        if media.id == media_id:
            return media
    return None


def apply_cmd(project: Project, cmd: Dict[str, Any]) -> Project:
    cmd_type = cmd.get("type")

    if cmd_type == "SET_FPS":
        project.fps = max(1.0, float(cmd.get("fps", project.fps)))
        return project

    if cmd_type == "SET_PLAYHEAD":
        project.playhead_f = max(0, int(cmd.get("frame", 0)))
        return project

    if cmd_type == "SELECT_CLIP":
        project.selected_clip_id = cmd.get("clip_id")
        return project

    if cmd_type == "MOVE_CLIP":
        clip_id = cmd.get("clip_id")
        new_start = max(0, int(cmd.get("start_f", 0)))
        new_track = max(0, int(cmd.get("track", 0)))
        for clip in project.clips:
            if clip.id == clip_id:
                clip.start_f = new_start
                clip.track = new_track
                project.selected_clip_id = clip_id
                break
        return project

    if cmd_type == "DELETE_SELECTED":
        if project.selected_clip_id:
            project.clips = [
                clip for clip in project.clips if clip.id != project.selected_clip_id
            ]
            project.selected_clip_id = None
        return project

    return project


class TimelineEditorPlugin(WAN2GPPlugin):
    name = "Wan2GP Timeline Editor"

    def __init__(self):
        super().__init__()
        self.globals: Dict[str, Any] = getattr(self, "globals", {})
        self.components: Dict[str, Any] = {}

    def setup_ui(self):
        self.add_tab(
            tab_id="timeline_editor_tab",
            label="Timeline",
            component_constructor=self.create_ui,
            position=1,
        )

        self.request_global("get_unique_id")
        self.request_global("has_video_file_extension")
        self.request_global("has_image_file_extension")
        self.request_global("has_audio_file_extension")
        self.request_global("get_video_info")
        self.request_global("get_video_frame")

        self.request_component("state")

    def post_ui_setup(
        self, components: Dict[str, gr.components.Component]
    ) -> Dict[gr.components.Component, Any]:
        self.components = components or {}
        return {}

    def create_ui(self):
        css = """
        #te-grid { display:grid; grid-template-columns: 340px 1fr 360px; gap: 12px; min-height: 78vh; }
        #te-left, #te-center, #te-right {
          border: 1px solid var(--border-color-primary);
          border-radius: 12px;
          background: var(--background-fill-primary);
          padding: 10px;
        }
        #te-center { display:grid; grid-template-rows: 1fr 320px; gap: 10px; }
        #te-timeline-host {
          border: 1px solid var(--border-color-primary);
          border-radius: 12px;
          background: var(--background-fill-secondary);
          height: 320px;
          overflow: hidden;
          position: relative;
        }
        .te-title { font-weight: 600; margin: 0 0 8px 0; }
        .te-sub { font-size: 12px; opacity: .8; margin-top: 4px; }
        """

        js = r"""
        function() {
          function $(sel, root=document){ return root.querySelector(sel); }

          async function loadCssOnce(href, id){
            if (document.getElementById(id)) return;
            const link = document.createElement('link');
            link.id = id;
            link.rel = 'stylesheet';
            link.type = 'text/css';
            link.href = href;
            document.head.appendChild(link);
          }

          async function loadScriptOnce(src, id){
            if (document.getElementById(id)) return;
            await new Promise((resolve, reject) => {
              const s = document.createElement('script');
              s.id = id;
              s.src = src;
              s.onload = resolve;
              s.onerror = reject;
              document.head.appendChild(s);
            });
          }

          async function initWhenReady(){
            const host = document.getElementById('te-timeline-host');
            const projEl = $('#te-project-json textarea');
            const cmdEl  = $('#te-cmd-json textarea');
            if (!host || !projEl || !cmdEl) return false;

            if (host.dataset.initialized === 'true') return true;

            await loadCssOnce(
              "https://unpkg.com/vis-timeline@latest/styles/vis-timeline-graph2d.min.css",
              "te-vis-timeline-css"
            );
            await loadScriptOnce(
              "https://unpkg.com/vis-timeline@latest/standalone/umd/vis-timeline-graph2d.min.js",
              "te-vis-timeline-js"
            );

            if (!window.vis) return false;

            host.innerHTML = "<div id='te-vis-container' style='width:100%;height:100%'></div>";
            const container = document.getElementById('te-vis-container');

            const groups = new vis.DataSet([]);
            const items  = new vis.DataSet([]);

            function safeJsonParse(s){
              try { return JSON.parse(s || "{}"); } catch(e){ return null; }
            }

            function sendCmd(obj){
              cmdEl.value = JSON.stringify(obj);
              cmdEl.dispatchEvent(new Event('input', { bubbles: true }));
            }

            function frameToMs(frame, fps){
              return Math.round((frame * 1000.0) / Math.max(1.0, fps));
            }

            function msToFrame(ms, fps){
              return Math.round((ms * Math.max(1.0, fps)) / 1000.0);
            }

            const options = {
              editable: { add:false, remove:false, updateTime:true, updateGroup:true },
              multiselect: false,
              stack: false,
              zoomable: true,
              horizontalScroll: true,
              selectable: true,
              margin: { item: 8, axis: 6 },
              orientation: "top",
            };

            const timeline = new vis.Timeline(container, items, groups, options);

            let hasPlayhead = false;
            function setPlayheadMs(ms){
              try {
                if (!hasPlayhead) {
                  timeline.addCustomTime(ms, "playhead");
                  hasPlayhead = true;
                } else {
                  timeline.setCustomTime(ms, "playhead");
                }
              } catch(e) {}
            }

            function syncFromProject(){
              const p = safeJsonParse(projEl.value);
              if (!p) return;

              const fps = p.fps || 16.0;

              let win = null;
              try { win = timeline.getWindow(); } catch(e) {}

              const gWanted = [];
              const n = p.tracks_video || 3;
              for (let i=0;i<n;i++){
                gWanted.push({ id: i, content: "V" + (i+1) });
              }
              const gExisting = new Set(groups.getIds());
              const gNext = new Set(gWanted.map(g=>g.id));
              gWanted.forEach(g => groups.update(g));
              gExisting.forEach(id => { if (!gNext.has(id)) groups.remove(id); });

              const clips = p.clips || [];
              const wantedIds = new Set();
              clips.forEach(c=>{
                wantedIds.add(c.id);
                const startMs = frameToMs(c.start_f || 0, fps);
                const durF = Math.max(1, (c.out_f - c.in_f));
                const endMs = frameToMs((c.start_f || 0) + durF, fps);
                items.update({
                  id: c.id,
                  content: c.id,
                  start: startMs,
                  end: endMs,
                  group: c.track || 0
                });
              });
              const existingIds = new Set(items.getIds());
              existingIds.forEach(id => { if (!wantedIds.has(id)) items.remove(id); });

              if (p.selected_clip_id) {
                try { timeline.setSelection([p.selected_clip_id], { focus: false }); } catch(e) {}
              } else {
                try { timeline.setSelection([], { focus: false }); } catch(e) {}
              }

              setPlayheadMs(frameToMs(p.playhead_f || 0, fps));

              if (win) {
                try { timeline.setWindow(win.start, win.end, { animation: false }); } catch(e) {}
              }
            }

            projEl.addEventListener('input', () => syncFromProject());
            syncFromProject();

            timeline.on('click', (props) => {
              const p = safeJsonParse(projEl.value);
              const fps = (p && p.fps) ? p.fps : 16.0;
              if (props.item) {
                sendCmd({ type: "SELECT_CLIP", clip_id: props.item });
              } else if (props.time != null) {
                const f = msToFrame(props.time.valueOf(), fps);
                sendCmd({ type: "SET_PLAYHEAD", frame: f });
              }
            });

            options.onMove = function (item, callback) {
              const p = safeJsonParse(projEl.value);
              const fps = (p && p.fps) ? p.fps : 16.0;

              const startF = msToFrame(item.start.valueOf(), fps);
              const track = (item.group != null) ? parseInt(item.group, 10) : 0;

              sendCmd({ type: "MOVE_CLIP", clip_id: item.id, start_f: startF, track: track });
              callback(item);
            };
            timeline.setOptions(options);

            host.dataset.initialized = 'true';
            window.__wan2gp_te = { timeline, items, groups };

            return true;
          }

          (function(){
            const obs = new MutationObserver(async () => { await initWhenReady(); });
            obs.observe(document.body, { childList: true, subtree: true });
            initWhenReady();
          })();
        }
        """

        with gr.Blocks(css=css) as root:
            root.load(fn=None, js=js)

            project_json = gr.Textbox(
                value=dumps_project(default_project()),
                visible=False,
                elem_id="te-project-json",
            )
            cmd_json = gr.Textbox(value="", visible=False, elem_id="te-cmd-json")

            with gr.Column(elem_id="te-grid"):
                with gr.Column(elem_id="te-left"):
                    gr.Markdown("### Project / Bin")
                    import_file = gr.File(
                        label="Import media (video/image/audio)", file_count="single"
                    )
                    btn_add = gr.Button("Add to Bin (auto-clip on V1)", variant="primary")
                    bin_json = gr.JSON(label="Media list (debug)", value=[])
                    gr.Markdown(
                        "<div class='te-sub'>"
                        "Phase 1: import + move clips + select + playhead + delete."
                        "</div>"
                    )

                with gr.Column(elem_id="te-center"):
                    with gr.Column():
                        gr.Markdown("### Timeline")
                        gr.HTML("<div id='te-timeline-host'></div>", elem_id="te-timeline-host")
                    with gr.Column():
                        gr.Markdown("### Program (frame preview)")
                        program_frame = gr.Image(
                            label="Frame @ playhead (track V1)", height=280
                        )

                with gr.Column(elem_id="te-right"):
                    gr.Markdown("### Inspector")
                    fps_box = gr.Number(label="Project FPS", value=16.0, precision=2)
                    btn_delete = gr.Button("Delete selected", variant="stop")
                    selected_json = gr.JSON(label="Selected clip (debug)", value={})

            def _kind_from_path(path: str) -> str:
                try:
                    if self.globals.get("has_video_file_extension", lambda _: False)(path):
                        return "video"
                    if self.globals.get("has_image_file_extension", lambda _: False)(path):
                        return "image"
                    if self.globals.get("has_audio_file_extension", lambda _: False)(path):
                        return "audio"
                except Exception:
                    pass
                return "unknown"

            def _get_uid() -> str:
                uid_fn = self.globals.get("get_unique_id")
                if callable(uid_fn):
                    return str(uid_fn())
                return "id_" + str(abs(hash(object())))

            def _add_media(file_obj, raw_proj):
                project = loads_project(raw_proj)

                if not file_obj:
                    return gr.update(value=[asdict(m) for m in project.media]), raw_proj

                path = getattr(file_obj, "name", None) or str(file_obj)
                kind = _kind_from_path(path)
                media_id = _get_uid()

                item = MediaItem(id=media_id, path=path, kind=kind)

                if kind == "video":
                    info_fn = self.globals.get("get_video_info")
                    if callable(info_fn):
                        try:
                            info = info_fn(path) or {}
                            if isinstance(info, dict):
                                item.fps = float(info.get("fps")) if info.get("fps") else None
                                item.frames = (
                                    int(info.get("frames")) if info.get("frames") else None
                                )
                                item.duration_s = (
                                    float(info.get("duration"))
                                    if info.get("duration")
                                    else None
                                )
                        except Exception:
                            pass

                    default_len_f = int(round(project.fps * 5.0))
                    if item.frames:
                        default_len_f = min(default_len_f, max(1, item.frames))
                    clip_id = f"CLIP_{media_id[-6:]}"
                    clip = Clip(
                        id=clip_id,
                        media_id=media_id,
                        track=0,
                        start_f=0,
                        in_f=0,
                        out_f=default_len_f,
                    )
                    project.clips.append(clip)
                    project.selected_clip_id = clip_id

                project.media.append(item)

                updated_raw = dumps_project(project)
                return gr.update(value=[asdict(m) for m in project.media]), updated_raw

            def _update_inspector_and_preview(raw_proj):
                project = loads_project(raw_proj)
                selected = find_clip(project, project.selected_clip_id)

                selected_info = asdict(selected) if selected else {}

                frame_img = None
                if selected:
                    media = find_media(project, selected.media_id)
                    if media and media.kind == "video":
                        get_frame = self.globals.get("get_video_frame")
                        if callable(get_frame):
                            try:
                                playhead = project.playhead_f
                                clip0 = None
                                for clip in project.clips:
                                    if clip.track != 0:
                                        continue
                                    dur = max(1, clip.out_f - clip.in_f)
                                    if clip.start_f <= playhead < (clip.start_f + dur):
                                        clip0 = clip
                                        break
                                if clip0:
                                    media0 = find_media(project, clip0.media_id)
                                    if media0 and media0.kind == "video":
                                        media_frame = clip0.in_f + (playhead - clip0.start_f)
                                        frame_img = get_frame(media0.path, int(media_frame))
                            except Exception:
                                frame_img = None

                return gr.update(value=selected_info), gr.update(value=frame_img)

            def _on_cmd(raw_cmd, raw_proj):
                if not raw_cmd:
                    selected_u, frame_u = _update_inspector_and_preview(raw_proj)
                    return raw_proj, selected_u, frame_u, ""

                project = loads_project(raw_proj)
                try:
                    cmd = json.loads(raw_cmd)
                except Exception:
                    selected_u, frame_u = _update_inspector_and_preview(raw_proj)
                    return raw_proj, selected_u, frame_u, ""

                project = apply_cmd(project, cmd)
                updated_raw = dumps_project(project)

                selected_u, frame_u = _update_inspector_and_preview(updated_raw)
                return updated_raw, selected_u, frame_u, ""

            def _set_fps(fps_value, raw_proj):
                project = loads_project(raw_proj)
                project.fps = max(1.0, float(fps_value or project.fps))
                updated_raw = dumps_project(project)
                selected_u, frame_u = _update_inspector_and_preview(updated_raw)
                return updated_raw, selected_u, frame_u

            def _delete_selected(raw_proj):
                project = loads_project(raw_proj)
                project = apply_cmd(project, {"type": "DELETE_SELECTED"})
                updated_raw = dumps_project(project)
                selected_u, frame_u = _update_inspector_and_preview(updated_raw)
                return updated_raw, selected_u, frame_u

            btn_add.click(
                _add_media,
                inputs=[import_file, project_json],
                outputs=[bin_json, project_json],
            )
            cmd_json.change(
                _on_cmd,
                inputs=[cmd_json, project_json],
                outputs=[project_json, selected_json, program_frame, cmd_json],
            )
            fps_box.change(
                _set_fps,
                inputs=[fps_box, project_json],
                outputs=[project_json, selected_json, program_frame],
            )
            btn_delete.click(
                _delete_selected,
                inputs=[project_json],
                outputs=[project_json, selected_json, program_frame],
            )

            selected_json.value, program_frame.value = {}, None

        return root


Plugin = TimelineEditorPlugin
