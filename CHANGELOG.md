# Changelog

## 0.2.0 - Phase 1 timeline integration
- Replace scaffold with full single-file Timeline Editor implementation in `plugin.py`.
- Register Timeline tab through `add_tab()` and request required Wan2GP globals/components.
- Add vis-timeline integration via CDN with frame↔ms mapping, selection, playhead, move/updateGroup.
- Add Project/Bin import, auto-clip creation, inspector actions, and frame preview wiring.
- Update plugin metadata for Wan2GP/WanGP compatibility (`wan2gp_version >=10.952`).
- Add a real project README with install and validation guidance.

## 0.1.0 - Initial scaffold
- Add installable Wan2GP plugin structure (`plugin.py`, `__init__.py`, `plugin_info.json`).
- Add minimal UI tab scaffold for `Timeline Editor`.
