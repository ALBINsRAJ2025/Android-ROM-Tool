# Android ROM Image Tool v3.0

Ubuntu 24+ only.

## Run
```bash
python3 rom_tool.py setup    # first-time setup
python3 rom_tool.py          # interactive menu
```

## Commands
```
python3 rom_tool.py unpack   <img> [name] [outdir]
python3 rom_tool.py repack   <workdir> [out.img]
python3 rom_tool.py info     <img>
python3 rom_tool.py super-unpack  <super.img> [outdir]
python3 rom_tool.py super-repack  <parts_dir> [out.img] [meta_dir]
python3 rom_tool.py batch-unpack  <img_dir>
python3 rom_tool.py fix-perms     [path]
```

## Workflow

### EXT4
1. `unpack` → files appear in `workspace/<name>/fs/`
2. Add/edit files freely in `fs/`
3. `repack` → new .img with all permissions auto-set

### super.img
1. `super-unpack` → individual .img files in `workspace/super_unpacked/partitions/`
2. `unpack` each .img, edit, `repack`
3. `super-repack` → new super.img

## Permissions / new files
- New files auto-get uid/gid/mode/SELinux based on path:
  - `*/bin/*` → 0:2000 0755
  - `*/lib/*` → 0:0 0644
  - `*/app/*` → 1000:1000 0644
  - `*.sh`    → 0755
  - `*.so`    → 0644
