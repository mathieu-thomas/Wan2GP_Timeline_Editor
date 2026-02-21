# Wan2GP Timeline Editor Plugin

Single-file Phase 1 timeline editor plugin for Wan2GP/WanGP.

## Features (Phase 1)
- Timeline tab registration through Wan2GP plugin hooks (`add_tab`).
- vis-timeline integration (groups/items) with drag/move between tracks.
- Project/Bin import flow with automatic clip creation on V1 for video files.
- Inspector with selected clip JSON, FPS editing, and delete selected clip.
- JS ↔ Python command bridge via hidden Gradio textboxes.
- Program frame preview at playhead via `get_video_frame` when available.

## Installation
1. Install this plugin from its repository URL in Wan2GP plugin manager.
2. Enable the plugin.
3. Restart/reload Wan2GP UI if needed.

## Required Wan2GP APIs
The plugin requests these globals/components in `setup_ui()`:
- Globals: `get_unique_id`, `has_video_file_extension`, `has_image_file_extension`,
  `has_audio_file_extension`, `get_video_info`, `get_video_frame`
- Component: `state`

## Manual validation checklist
1. Timeline tab appears.
2. Import a video and click **Add to Bin**.
3. A clip appears on V1.
4. Drag clip horizontally and across tracks.
5. Click clip to select and inspect.
6. Click empty timeline space to move playhead.
7. Delete selected clip.
8. Change FPS and verify timeline resync.
