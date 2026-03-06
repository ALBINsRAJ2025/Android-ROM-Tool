#!/usr/bin/env bash
# =============================================================================
#  Android ROM EXT4/Super Image Tool
#  Ubuntu 24+ | Handles ext4, sparse, super.img, fs_config, SELinux contexts
#  Supports: system, system_ext, product, vendor
# =============================================================================

# Do NOT use set -e here — it kills menus when any sub-command returns non-zero
set -uo pipefail

# ─── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR/tools"
WORK_DIR="${WORK_DIR:-$SCRIPT_DIR/workspace}"
LOG_FILE="$SCRIPT_DIR/rom_tool.log"
VERSION="2.1"

# Track the real (non-root) user even when sudo is called internally
REAL_USER="${SUDO_USER:-${USER:-$(id -un)}}"
REAL_UID="$(id -u "$REAL_USER" 2>/dev/null || id -u)"
REAL_GID="$(id -g "$REAL_USER" 2>/dev/null || id -g)"

# ─── Colours ─────────────────────────────────────────────────────────────────
R=$'\033[0;31m'; G=$'\033[0;32m'; Y=$'\033[1;33m'
B=$'\033[0;34m'; C=$'\033[0;36m'; W=$'\033[1;37m'; N=$'\033[0m'
BOLD=$'\033[1m'

# ─── Logging ─────────────────────────────────────────────────────────────────
log()  { echo -e "$(date '+%H:%M:%S') [INFO]  $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${Y}$(date '+%H:%M:%S') [WARN]  $*${N}" | tee -a "$LOG_FILE"; }
err()  { echo -e "${R}$(date '+%H:%M:%S') [ERROR] $*${N}" | tee -a "$LOG_FILE" >&2; }
ok()   { echo -e "${G}$(date '+%H:%M:%S') [OK]    $*${N}" | tee -a "$LOG_FILE"; }
die()  { err "$*"; exit 1; }
fail() { err "$*"; return 1; }   # non-fatal — stays inside menu loops

# ─── Safe command runner (won't kill the menu on failure) ────────────────────
run() {
  "$@" 2>>"$LOG_FILE"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    warn "Command returned $rc: $*  (see $LOG_FILE)"
  fi
  return $rc
}

# ─── Ownership helper ─────────────────────────────────────────────────────────
# Always give dirs/files back to the real user so file managers can write to them
own_dir() {
  local dir="$1"
  mkdir -p "$dir"
  sudo chown -R "${REAL_UID}:${REAL_GID}" "$dir" 2>/dev/null || true
  chmod -R u+rwX "$dir" 2>/dev/null || true
}

# ─── Banner ──────────────────────────────────────────────────────────────────
banner() {
  clear
  echo -e "${C}${BOLD}"
  echo "  ╔══════════════════════════════════════════════════════╗"
  echo "  ║     Android ROM Image Tool  v${VERSION}                   ║"
  echo "  ║     ext4 · super · sparse · fs_config · SELinux     ║"
  echo "  ╚══════════════════════════════════════════════════════╝${N}"
  echo ""
}

# ─── Ubuntu 24+ check ────────────────────────────────────────────────────────
check_os() {
  if [[ ! -f /etc/os-release ]]; then die "Cannot detect OS"; fi
  # shellcheck source=/dev/null
  . /etc/os-release
  if [[ "$ID" != "ubuntu" ]]; then die "Ubuntu required (got $ID)"; fi
  local major; major=$(echo "$VERSION_ID" | cut -d. -f1)
  if [[ "$major" -lt 24 ]]; then die "Ubuntu 24+ required (got $VERSION_ID)"; fi
  ok "OS check passed: $PRETTY_NAME"
}

# ─── Architecture ────────────────────────────────────────────────────────────
get_arch() {
  local m; m=$(uname -m)
  case "$m" in
    x86_64)  echo "x86_64"  ;;
    aarch64) echo "aarch64" ;;
    *) die "Unsupported architecture: $m" ;;
  esac
}

# ─── Setup tools dir ─────────────────────────────────────────────────────────
setup_tools() {
  local arch; arch=$(get_arch)
  local src="$SCRIPT_DIR/binaries/bin/Linux/$arch"

  if [[ ! -d "$src" ]]; then
    die "Bundled binaries not found at: $src\nMake sure 'binaries/' folder is alongside this script."
  fi

  mkdir -p "$TOOLS_DIR"

  local tools_needed=(
    simg2img img2simg e2fsdroid mke2fs make_ext4fs
    lpmake imgkit magiskboot brotli zstd
  )
  # simg2img only in x86_64; aarch64 uses system's
  for t in "${tools_needed[@]}"; do
    local tsrc="$src/$t"
    local tdst="$TOOLS_DIR/$t"
    if [[ -f "$tsrc" ]]; then
      if [[ ! -f "$tdst" ]] || [[ "$tsrc" -nt "$tdst" ]]; then
        cp -f "$tsrc" "$tdst"
        chmod +x "$tdst"
      fi
    fi
  done

  ok "Tools ready in: $TOOLS_DIR"
}

# ─── Install apt deps ────────────────────────────────────────────────────────
install_deps() {
  local pkgs=(
    e2fsprogs          # debugfs dumpe2fs e2fsck tune2fs
    util-linux         # losetup mount
    attr               # getfattr setfattr
    python3            # helper scripts
    python3-pip
    coreutils
    file
    bc
    pv
  )

  # lpunpack from android tools if available
  if apt-cache show android-sdk-libsparse-utils &>/dev/null 2>&1; then
    pkgs+=(android-sdk-libsparse-utils)
  fi

  log "Checking / installing required packages …"
  sudo apt-get update -qq 2>>"$LOG_FILE"
  sudo apt-get install -y "${pkgs[@]}" 2>>"$LOG_FILE" | grep -E "^(Setting up|already)" || true
  ok "Dependencies satisfied"
}

# ─── Tool path helper ─────────────────────────────────────────────────────────
T() {
  # Returns path to tool: bundled > system
  local name="$1"
  if [[ -x "$TOOLS_DIR/$name" ]]; then
    echo "$TOOLS_DIR/$name"
  elif command -v "$name" &>/dev/null; then
    command -v "$name"
  else
    die "Tool not found: $name  (run Setup first)"
  fi
}

# ─── Detect image format ─────────────────────────────────────────────────────
detect_format() {
  local img="$1"
  local magic
  magic=$(xxd -l 4 -p "$img" 2>/dev/null || od -A x -t x1z -v "$img" 2>/dev/null | head -1 | awk '{print $2$3$4$5}' | tr -d ' ')

  case "$magic" in
    3aff26ed)    echo "sparse" ;;   # Android sparse
    53ef*)       echo "ext4"   ;;   # ext4 superblock offset… check differently
    e2*)         echo "ext4"   ;;
    *)
      # Check ext4 by looking at offset 1080 (superblock magic 0xEF53)
      local sb
      sb=$(dd if="$img" bs=1 skip=1080 count=2 2>/dev/null | xxd -p 2>/dev/null || true)
      if [[ "$sb" == "53ef" ]]; then
        echo "ext4"
      else
        # Check for super / LP metadata
        local lp
        lp=$(dd if="$img" bs=1 skip=4096 count=4 2>/dev/null | xxd -p 2>/dev/null || true)
        if [[ "$lp" == "67c40c4d" ]] || [[ "$lp" == "4d0cc467" ]]; then
          echo "super"
        else
          local ftype; ftype=$(file -b "$img" 2>/dev/null || true)
          echo "$ftype" | grep -qi "ext2\|ext3\|ext4" && echo "ext4" && return
          echo "unknown"
        fi
      fi
      ;;
  esac
}

# ─── Convert sparse → raw ────────────────────────────────────────────────────
to_raw() {
  local in="$1" out="$2"
  local fmt; fmt=$(detect_format "$in")
  if [[ "$fmt" == "sparse" ]]; then
    log "Converting sparse → raw …"
    "$(T simg2img)" "$in" "$out"
  else
    log "Already raw, copying …"
    cp -f "$in" "$out"
  fi
}

# ─── Convert raw → sparse ────────────────────────────────────────────────────
to_sparse() {
  local in="$1" out="$2"
  log "Converting raw → sparse …"
  "$(T img2simg)" "$in" "$out"
}

# ─── Save image metadata ─────────────────────────────────────────────────────
save_image_meta() {
  local raw_img="$1"
  local meta_dir="$2"
  local part_name="$3"

  mkdir -p "$meta_dir"

  # Basic fs info
  dumpe2fs -h "$raw_img" 2>/dev/null > "$meta_dir/dumpe2fs.txt" || true

  # Extract key values
  local block_size block_count inode_count inode_size mnt_point label
  block_size=$(grep "^Block size:" "$meta_dir/dumpe2fs.txt" | awk '{print $3}')
  block_count=$(grep "^Block count:" "$meta_dir/dumpe2fs.txt" | awk '{print $3}')
  inode_count=$(grep "^Inode count:" "$meta_dir/dumpe2fs.txt" | awk '{print $3}')
  inode_size=$(grep "^Inode size:" "$meta_dir/dumpe2fs.txt" | awk '{print $3}')
  mnt_point=$(grep "^Last mounted on:" "$meta_dir/dumpe2fs.txt" | awk '{print $4}')
  label=$(grep "^Filesystem volume name:" "$meta_dir/dumpe2fs.txt" | awk '{print $4}')

  # Fallback mount point from partition name
  if [[ -z "$mnt_point" || "$mnt_point" == "<not" ]]; then
    case "$part_name" in
      system)      mnt_point="/"           ;;
      system_ext)  mnt_point="/system_ext" ;;
      product)     mnt_point="/product"    ;;
      vendor)      mnt_point="/vendor"     ;;
      odm)         mnt_point="/odm"        ;;
      *)           mnt_point="/$part_name" ;;
    esac
  fi

  local img_size; img_size=$(stat -c%s "$raw_img")

  cat > "$meta_dir/image_info.txt" <<EOF
partition_name=$part_name
mount_point=$mnt_point
block_size=$block_size
block_count=$block_count
inode_count=$inode_count
inode_size=$inode_size
label=$label
original_size=$img_size
original_was_sparse=0
EOF

  log "Saved image metadata → $meta_dir/image_info.txt"
}

# ─── Extract fs_config from mounted ext4 ─────────────────────────────────────
extract_fs_config() {
  local mount_dir="$1"
  local meta_dir="$2"
  local mnt_point="$3"
  local out_cfg="$meta_dir/fs_config.txt"
  local out_ctx="$meta_dir/file_contexts.txt"

  log "Extracting fs_config and selinux contexts …"

  : > "$out_cfg"
  : > "$out_ctx"

  # Walk all files + dirs
  find "$mount_dir" -print0 | sort -z | while IFS= read -r -d '' fpath; do
    local rel="${fpath#$mount_dir}"
    [[ -z "$rel" ]] && rel="/"

    # stat: uid gid mode
    local uid gid mode
    uid=$(stat -c '%u' "$fpath" 2>/dev/null || echo 0)
    gid=$(stat -c '%g' "$fpath" 2>/dev/null || echo 0)
    mode=$(stat -c '%a' "$fpath" 2>/dev/null || echo 644)

    # capabilities
    local caps=0
    if command -v getcap &>/dev/null; then
      local cap_str; cap_str=$(getcap "$fpath" 2>/dev/null || true)
      if [[ -n "$cap_str" ]]; then
        caps=$(echo "$cap_str" | grep -oP '0x[0-9a-fA-F]+' | head -1 || echo 0)
        [[ -z "$caps" ]] && caps=0
      fi
    fi

    # Build fs_config line: path uid gid mode [caps]
    local cfg_path
    if [[ "$rel" == "/" ]]; then
      cfg_path="${mnt_point#/}"
      [[ -z "$cfg_path" ]] && cfg_path="system"
    else
      cfg_path="${mnt_point#/}${rel}"
    fi
    # Remove leading slash
    cfg_path="${cfg_path#/}"

    echo "$cfg_path $uid $gid $mode $caps" >> "$out_cfg"

    # SELinux context
    local ctx
    ctx=$(getfattr -n security.selinux --only-values --absolute-names "$fpath" 2>/dev/null | tr -d '\0' || true)
    if [[ -z "$ctx" ]]; then
      ctx=$(attr -g selinux "$fpath" 2>/dev/null | tail -1 || true)
    fi
    if [[ -z "$ctx" ]]; then
      ctx="u:object_r:system_file:s0"
    fi

    # file_contexts line: /mount_point/rel_path context
    local ctx_path
    if [[ "$rel" == "/" ]]; then
      ctx_path="${mnt_point}"
    else
      ctx_path="${mnt_point}${rel}"
    fi
    # Escape regex special chars
    local ctx_regex="${ctx_path//./\\.}"
    ctx_regex="${ctx_regex//[/\\[}"
    ctx_regex="${ctx_regex//]/\\]}"
    echo "${ctx_regex} ${ctx}" >> "$out_ctx"

  done

  local fc=$(wc -l < "$out_cfg")
  ok "fs_config: $fc entries → $out_cfg"
  ok "file_contexts: $fc entries → $out_ctx"
}

# ─── Save file list for new-file detection ────────────────────────────────────
save_file_list() {
  local extract_dir="$1"
  local meta_dir="$2"
  find "$extract_dir" -print0 | sort -z | \
    while IFS= read -r -d '' f; do echo "${f#$extract_dir}"; done \
    > "$meta_dir/file_list.txt"
  ok "File list saved: $(wc -l < "$meta_dir/file_list.txt") entries"
}

# ─── Default permission for a new file ───────────────────────────────────────
default_perm_for_path() {
  local fpath="$1"       # relative path within partition, e.g. system/bin/mybin
  local is_dir="$2"      # "dir" or "file"

  local uid=0 gid=0 mode=644 ctx="u:object_r:system_file:s0"

  if [[ "$is_dir" == "dir" ]]; then
    mode=755
    uid=0; gid=0
    ctx="u:object_r:system_file:s0"
  fi

  case "$fpath" in
    */bin/*|*/xbin/*|*/sbin/*)
      uid=0; gid=2000; mode=755
      ctx="u:object_r:system_file:s0"
      [[ "$is_dir" == "dir" ]] && mode=755 && uid=0 && gid=0
      ;;
    */lib/*|*/lib64/*)
      uid=0; gid=0; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:system_lib_file:s0"
      ;;
    */app/*|*/priv-app/*|*/system-app/*)
      uid=1000; gid=1000; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755 && uid=1000 && gid=1000
      ctx="u:object_r:system_app_file:s0"
      ;;
    */framework/*)
      uid=0; gid=0; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:system_file:s0"
      ;;
    */etc/*|*/vendor/etc/*)
      uid=0; gid=0; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:system_file:s0"
      ;;
    */vendor/bin/*|*/vendor/lib/*|*/vendor/lib64/*)
      uid=0; gid=2000; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755 && gid=0
      ctx="u:object_r:vendor_file:s0"
      [[ "$fpath" == */vendor/bin/* ]] && mode=755
      ;;
    */vendor/app/*)
      uid=1000; gid=1000; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:vendor_app_file:s0"
      ;;
    vendor/*|*/vendor/*)
      uid=0; gid=0; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:vendor_file:s0"
      ;;
    product/*|*/product/*)
      uid=0; gid=0; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:system_file:s0"
      ;;
    system_ext/*|*/system_ext/*)
      uid=0; gid=0; mode=644
      [[ "$is_dir" == "dir" ]] && mode=755
      ctx="u:object_r:system_ext_file:s0"
      ;;
  esac

  # Executable heuristic for files
  if [[ "$is_dir" == "file" ]]; then
    local ext="${fpath##*.}"
    case "$ext" in
      so)     mode=644 ;;
      apk|jar|apex) mode=644 ;;
      sh|py|pl|rb)  mode=755 ;;
    esac
  fi

  echo "$uid $gid $mode $ctx"
}

# ─── Generate fs_config for new files ────────────────────────────────────────
update_fs_config_for_new_files() {
  local extract_dir="$1"
  local meta_dir="$2"
  local mnt_point="$3"
  local part_prefix="${mnt_point#/}"     # e.g. "system"

  local orig_list="$meta_dir/file_list.txt"
  local fs_cfg="$meta_dir/fs_config.txt"
  local file_ctx="$meta_dir/file_contexts.txt"

  log "Scanning for new/changed files …"
  local new_count=0

  find "$extract_dir" -print0 | sort -z | while IFS= read -r -d '' fpath; do
    local rel="${fpath#$extract_dir}"
    [[ -z "$rel" ]] && continue

    # Check if this was in original list
    if grep -qF "$rel" "$orig_list" 2>/dev/null; then
      continue
    fi

    # New file — generate permissions
    local is_dir="file"
    [[ -d "$fpath" ]] && is_dir="dir"

    local cfg_path="${part_prefix}${rel}"
    cfg_path="${cfg_path#/}"

    read -r uid gid mode ctx <<< "$(default_perm_for_path "$cfg_path" "$is_dir")"

    echo "$cfg_path $uid $gid $mode 0" >> "$fs_cfg"

    local ctx_path="${mnt_point}${rel}"
    local ctx_regex="${ctx_path//./\\.}"
    echo "${ctx_regex} ${ctx}" >> "$file_ctx"

    new_count=$((new_count + 1))
    log "  [NEW] $cfg_path → uid=$uid gid=$gid mode=$mode ctx=$ctx"
  done

  ok "New file detection complete"
}

# ─── Mount ext4 image ────────────────────────────────────────────────────────
mount_image() {
  local img="$1" mnt="$2"
  mkdir -p "$mnt"
  sudo mount -o loop,ro "$img" "$mnt" 2>>"$LOG_FILE" \
    || die "Failed to mount $img → $mnt (need sudo)"
}

unmount_image() {
  local mnt="$1"
  sudo umount "$mnt" 2>>"$LOG_FILE" || sudo umount -l "$mnt" 2>>"$LOG_FILE" || true
  rmdir "$mnt" 2>/dev/null || true
}

# ─── UNPACK EXT4 ─────────────────────────────────────────────────────────────
cmd_unpack() {
  local img_path="$1"
  local part_name="${2:-$(basename "$img_path" .img)}"
  local out_dir="${3:-$WORK_DIR/$part_name}"

  [[ ! -f "$img_path" ]] && die "Image not found: $img_path"

  banner
  log "═══ UNPACK: $part_name ═══"
  log "Source : $img_path"
  log "Output : $out_dir"

  mkdir -p "$out_dir"
  own_dir "$out_dir"
  raw_img="$(realpath "$raw_img" 2>/dev/null || echo "$raw_img")"

  # 1. Detect format
  local fmt; fmt=$(detect_format "$img_path")
  log "Detected format: $fmt"

  local was_sparse=0
  case "$fmt" in
    sparse)
      was_sparse=1
      to_raw "$img_path" "$raw_img"
      ;;
    ext4|unknown)
      raw_img="$img_path"
      ;;
    super)
      die "This is a super.img — use Super Unpack option instead."
      ;;
    *)
      warn "Unknown format, attempting as raw ext4 …"
      raw_img="$img_path"
      ;;
  esac

  # 2. fsck
  log "Running e2fsck …"
  sudo e2fsck -fy "$raw_img" >>"$LOG_FILE" 2>&1 || true

  # 3. Save metadata
  local meta_dir="$out_dir/META"
  save_image_meta "$raw_img" "$meta_dir" "$part_name"
  if [[ "$was_sparse" == 1 ]]; then
    sed -i 's/original_was_sparse=0/original_was_sparse=1/' "$meta_dir/image_info.txt"
  fi

  # 4. Mount and extract
  local mnt_tmp; mnt_tmp=$(mktemp -d /tmp/rom_mnt_XXXXXX)
  local fs_dir="$out_dir/fs"
  mkdir -p "$fs_dir"

  log "Mounting and extracting files …"
  mount_image "$raw_img" "$mnt_tmp"

  # Read mount point
  local mnt_point
  mnt_point=$(grep "^mount_point=" "$meta_dir/image_info.txt" | cut -d= -f2)

  log "Copying files (preserving all attributes + xattrs) …"
  sudo cp -a --preserve=all "$mnt_tmp"/. "$fs_dir"/ 2>>"$LOG_FILE" || \
    sudo rsync -aAX "$mnt_tmp"/ "$fs_dir"/ 2>>"$LOG_FILE" || \
    { warn "cp failed, trying tar …"; sudo tar -cf - -C "$mnt_tmp" . | tar -xf - -C "$fs_dir"; }

  # 5. Extract fs_config and file_contexts while mounted
  extract_fs_config "$mnt_tmp" "$meta_dir" "$mnt_point"

  unmount_image "$mnt_tmp"

  # Fix ownership of extracted files so user can edit them
  sudo chown -R "${REAL_UID}:${REAL_GID}" "$fs_dir" 2>>"$LOG_FILE" || true
  sudo chmod -R u+rwX "$fs_dir" 2>>"$LOG_FILE" || true
  own_dir "$meta_dir"

  # 6. Save original file list for new-file detection
  save_file_list "$fs_dir" "$meta_dir"

  # 7. Save raw img path reference
  echo "$raw_img" > "$meta_dir/raw_img_path.txt"

  ok "═══ Unpack complete ═══"
  echo -e "${G}"
  echo "  Extracted to : $fs_dir"
  echo "  Metadata in  : $meta_dir"
  echo "  You can now add/modify files inside: $fs_dir"
  echo -e "${N}"
}

# ─── REPACK EXT4 ─────────────────────────────────────────────────────────────
cmd_repack() {
  local out_dir="$1"
  local output_img="${2:-}"

  local meta_dir="$out_dir/META"
  local fs_dir="$out_dir/fs"

  [[ ! -d "$out_dir"  ]] && die "Work directory not found: $out_dir"
  [[ ! -d "$meta_dir" ]] && die "META directory missing — was this unpacked with this tool?"
  [[ ! -d "$fs_dir"   ]] && die "fs directory missing: $fs_dir"

  # Load metadata
  local part_name mount_point block_size inode_size was_sparse label orig_size
  part_name=$(grep  "^partition_name="   "$meta_dir/image_info.txt" | cut -d= -f2)
  mount_point=$(grep "^mount_point="     "$meta_dir/image_info.txt" | cut -d= -f2)
  block_size=$(grep  "^block_size="      "$meta_dir/image_info.txt" | cut -d= -f2)
  inode_size=$(grep  "^inode_size="      "$meta_dir/image_info.txt" | cut -d= -f2)
  was_sparse=$(grep  "^original_was_sparse=" "$meta_dir/image_info.txt" | cut -d= -f2)
  label=$(grep       "^label="           "$meta_dir/image_info.txt" | cut -d= -f2 || true)
  orig_size=$(grep   "^original_size="   "$meta_dir/image_info.txt" | cut -d= -f2)

  block_size=${block_size:-4096}
  inode_size=${inode_size:-256}
  was_sparse=${was_sparse:-0}

  banner
  log "═══ REPACK: $part_name ═══"

  # Default output path
  if [[ -z "$output_img" ]]; then
    output_img="$WORK_DIR/${part_name}_repacked.img"
  fi
  mkdir -p "$(dirname "$output_img")"

  # 1. Detect new files and update configs
  update_fs_config_for_new_files "$fs_dir" "$meta_dir" "$mount_point"

  # 2. Calculate image size
  log "Calculating required image size …"
  local used_kb; used_kb=$(du -sk "$fs_dir" | awk '{print $1}')
  local used_bytes=$(( used_kb * 1024 ))

  # Add 20% headroom + at least 32MB
  local headroom=$(( used_bytes / 5 ))
  [[ "$headroom" -lt $((32 * 1024 * 1024)) ]] && headroom=$((32 * 1024 * 1024))
  local new_size=$(( used_bytes + headroom ))

  # Align to block size
  local bs=${block_size:-4096}
  new_size=$(( (new_size + bs - 1) / bs * bs ))

  # Never go smaller than original unless forced
  if [[ "$new_size" -lt "$orig_size" ]]; then
    new_size="$orig_size"
  fi

  log "Image size: $(( new_size / 1024 / 1024 )) MB"

  local raw_img="${output_img%.img}.raw.img"

  # 3. Create new ext4 filesystem with mke2fs
  log "Creating new ext4 filesystem …"
  local mke2fs_args=(
    -t ext4
    -b "$block_size"
    -I "$inode_size"
    -m 0
    -O "^huge_file,^metadata_csum_seed"
  )
  [[ -n "$label" && "$label" != "<none>" ]] && mke2fs_args+=(-L "$label")

  local block_count=$(( new_size / block_size ))

  "$(T mke2fs)" "${mke2fs_args[@]}" "$raw_img" "$block_count" >>"$LOG_FILE" 2>&1 \
    || die "mke2fs failed — see $LOG_FILE"

  # 4. Populate with e2fsdroid
  log "Populating filesystem with e2fsdroid …"

  local e2fs_args=(
    -f "$fs_dir"
    -T 0
  )

  # fs_config
  if [[ -f "$meta_dir/fs_config.txt" ]]; then
    e2fs_args+=(-C "$meta_dir/fs_config.txt")
  fi

  # SELinux file_contexts
  if [[ -f "$meta_dir/file_contexts.txt" ]]; then
    e2fs_args+=(-S "$meta_dir/file_contexts.txt")
  fi

  # Mount point
  e2fs_args+=(-D "$fs_dir" -m "$mount_point")

  "$(T e2fsdroid)" "${e2fs_args[@]}" "$raw_img" >>"$LOG_FILE" 2>&1 \
    || die "e2fsdroid failed — see $LOG_FILE"

  # 5. Optionally convert to sparse
  if [[ "$was_sparse" == "1" ]]; then
    log "Converting back to sparse …"
    to_sparse "$raw_img" "$output_img"
    rm -f "$raw_img"
  else
    mv -f "$raw_img" "$output_img"
  fi

  ok "═══ Repack complete ═══"
  echo -e "${G}"
  echo "  Output image : $output_img"
  echo "  Size         : $(du -sh "$output_img" | awk '{print $1}')"
  echo -e "${N}"
}

# ─── SUPER.IMG UNPACK ────────────────────────────────────────────────────────
cmd_super_unpack() {
  local super_img="$1"
  local out_dir="${2:-$WORK_DIR/super_unpacked}"

  [[ ! -f "$super_img" ]] && die "Super image not found: $super_img"

  banner
  log "═══ SUPER UNPACK ═══"
  log "Source : $super_img"
  log "Output : $out_dir"

  mkdir -p "$out_dir"
  own_dir "$out_dir"
  local raw_super="$out_dir/super.raw.img"
  local was_sparse=0
  local fmt; fmt=$(detect_format "$super_img")
  log "Super format: $fmt"

  if [[ "$fmt" == "sparse" ]]; then
    was_sparse=1
    to_raw "$super_img" "$raw_super"
  else
    raw_super="$super_img"
  fi

  # 2. Read LP metadata with lpdump / imgkit
  log "Reading LP partition table …"
  local lp_info_file="$out_dir/META/lp_info.txt"
  mkdir -p "$out_dir/META"

  # Try lpdump first (may be installed via apt)
  if command -v lpdump &>/dev/null; then
    lpdump "$raw_super" 2>/dev/null > "$lp_info_file" || true
  fi

  # Try imgkit for lp metadata
  if [[ ! -s "$lp_info_file" ]] && [[ -x "$(T imgkit)" ]]; then
    "$(T imgkit)" lpunpack --info "$raw_super" > "$lp_info_file" 2>/dev/null || true
  fi

  # 3. Unpack partitions
  local parts_dir="$out_dir/partitions"
  mkdir -p "$parts_dir"
  own_dir "$parts_dir"

  local unpacked=0

  # Method A: lpunpack (system or android-sdk)
  if command -v lpunpack &>/dev/null; then
    log "Using lpunpack …"
    lpunpack "$raw_super" "$parts_dir" >>"$LOG_FILE" 2>&1 && unpacked=1
  fi

  # Method B: imgkit lpunpack
  if [[ "$unpacked" == 0 ]] && [[ -x "$(T imgkit)" ]]; then
    log "Using imgkit lpunpack …"
    "$(T imgkit)" lpunpack "$raw_super" "$parts_dir" >>"$LOG_FILE" 2>&1 && unpacked=1
  fi

  # Method C: simg2img approach for older super.img
  if [[ "$unpacked" == 0 ]]; then
    warn "lpunpack not available — attempting manual LP extraction …"
    python3 "$SCRIPT_DIR/tools/lpunpack.py" "$raw_super" "$parts_dir" >>"$LOG_FILE" 2>&1 && unpacked=1
  fi

  if [[ "$unpacked" == 0 ]]; then
    die "Could not unpack super.img — install lpunpack: sudo apt install android-sdk-libsparse-utils"
  fi

  # Save super metadata for repack
  cat > "$out_dir/META/super_info.txt" <<EOF
original_super=$super_img
was_sparse=$was_sparse
raw_super_size=$(stat -c%s "$raw_super")
EOF

  # Append LP info
  lpdump "$raw_super" 2>/dev/null >> "$out_dir/META/super_info.txt" || \
    "$(T imgkit)" lpunpack --info "$raw_super" >> "$out_dir/META/super_info.txt" 2>/dev/null || true

  ok "═══ Super unpack complete ═══"
  echo -e "${G}"
  echo "  Extracted partitions:"
  for f in "$parts_dir"/*.img; do
    [[ -f "$f" ]] && echo "    $(basename "$f")  $(du -sh "$f" | awk '{print $1}')"
  done
  echo ""
  echo "  To edit partitions, run Unpack on each .img in:"
  echo "  $parts_dir"
  echo ""
  echo "  Metadata: $out_dir/META/"
  echo -e "${N}"
}

# ─── SUPER.IMG REPACK ────────────────────────────────────────────────────────
cmd_super_repack() {
  local parts_dir="$1"     # directory with repacked partition .img files
  local output_img="${2:-$WORK_DIR/super_repacked.img}"
  local meta_dir="${3:-$(dirname "$parts_dir")/META}"

  [[ ! -d "$parts_dir" ]] && die "Partitions directory not found: $parts_dir"

  banner
  log "═══ SUPER REPACK ═══"

  mkdir -p "$(dirname "$output_img")"

  # Read LP info
  local lp_info="$meta_dir/super_info.txt"

  # Parse original lpmake parameters from lp_info if available
  local metadata_size=65536
  local metadata_slots=3
  local super_size=0
  local was_sparse=0

  if [[ -f "$lp_info" ]]; then
    was_sparse=$(grep "^was_sparse=" "$lp_info" | cut -d= -f2 || echo 0)
    super_size=$(grep "^raw_super_size=" "$lp_info" | cut -d= -f2 || echo 0)
  fi

  # Build lpmake partition arguments
  local lpmake_args=()
  local total_part_size=0

  for img in "$parts_dir"/*.img; do
    [[ ! -f "$img" ]] && continue
    local pname; pname=$(basename "$img" .img)
    local psize; psize=$(stat -c%s "$img")

    # Convert to raw if sparse for size calc
    local raw_pimg="$img"
    local pfmt; pfmt=$(detect_format "$img")
    if [[ "$pfmt" == "sparse" ]]; then
      raw_pimg=$(mktemp /tmp/rom_part_XXXXXX.raw)
      "$(T simg2img)" "$img" "$raw_pimg" >>"$LOG_FILE" 2>&1
      psize=$(stat -c%s "$raw_pimg")
      rm -f "$raw_pimg"
    fi

    total_part_size=$(( total_part_size + psize ))

    # Determine group (all partitions in "main" group by default)
    local group="main"
    lpmake_args+=(
      --partition "${pname}:readonly:${psize}:${group}"
      --image "${pname}=${img}"
    )
    log "  Partition: $pname  size=$(( psize/1024/1024 ))MB"
  done

  # Calculate super device size
  local pad=$(( 4 * 1024 * 1024 ))  # 4MB padding
  local device_size=$(( total_part_size + pad + metadata_size * metadata_slots * 2 ))
  # Round up to 512 boundary
  device_size=$(( (device_size + 511) / 512 * 512 ))
  [[ "$super_size" -gt "$device_size" ]] && device_size="$super_size"

  log "Super device size: $(( device_size / 1024 / 1024 ))MB"

  local raw_out="${output_img%.img}.raw.img"

  "$(T lpmake)" \
    --metadata-size "$metadata_size" \
    --metadata-slots "$metadata_slots" \
    --device "super:${device_size}" \
    --group "main:${device_size}" \
    "${lpmake_args[@]}" \
    --sparse \
    --output "$output_img" \
    >>"$LOG_FILE" 2>&1 || die "lpmake failed — see $LOG_FILE"

  ok "═══ Super repack complete ═══"
  echo -e "${G}"
  echo "  Output : $output_img"
  echo "  Size   : $(du -sh "$output_img" | awk '{print $1}')"
  echo -e "${N}"
}

# ─── Batch: unpack multiple partition images ──────────────────────────────────
cmd_batch_unpack() {
  local img_dir="$1"
  local out_base="${2:-$WORK_DIR}"

  [[ ! -d "$img_dir" ]] && die "Directory not found: $img_dir"

  banner
  log "═══ BATCH UNPACK from $img_dir ═══"

  local count=0
  for img in "$img_dir"/{system,system_ext,product,vendor,odm}.img \
             "$img_dir"/{system,system_ext,product,vendor,odm}_a.img \
             "$img_dir"/{system,system_ext,product,vendor,odm}_b.img; do
    [[ ! -f "$img" ]] && continue
    local pname; pname=$(basename "$img" .img | sed 's/_[ab]$//')
    log "Processing: $img → $out_base/$pname"
    ( cmd_unpack "$img" "$pname" "$out_base/$pname" ) || { err "Unpack failed for $img"; }
    count=$(( count + 1 ))
  done

  ok "Batch unpack done: $count images processed"
}

# ─── Image info ───────────────────────────────────────────────────────────────
cmd_info() {
  local img="$1"
  [[ ! -f "$img" ]] && die "File not found: $img"

  local fmt; fmt=$(detect_format "$img")
  local fsize; fsize=$(stat -c%s "$img")

  echo -e "\n${W}Image: $img${N}"
  echo "  Size   : $(du -sh "$img" | awk '{print $1}') ($fsize bytes)"
  echo "  Format : $fmt"

  if [[ "$fmt" == "sparse" ]]; then
    echo ""
    echo "  (convert to raw to read ext4 metadata)"
    return
  fi

  echo ""
  dumpe2fs -h "$img" 2>/dev/null | grep -E "^(Block size|Block count|Inode count|Volume name|Last mounted|Filesystem features)" || true
}

# ─── Install missing tools ────────────────────────────────────────────────────
cmd_setup() {
  banner
  log "═══ SETUP ═══"
  check_os
  setup_tools
  install_deps
  own_dir "$WORK_DIR"
  ok "Workspace ready: $WORK_DIR  (owned by $REAL_USER)"
  ok "Setup complete. Run rom_tool.sh to start."
}

# ─── Interactive menu ─────────────────────────────────────────────────────────
prompt() {
  local question="$1"
  local default="${2:-}"
  local ans=""
  echo -ne "${C}  ${question}${N}"
  [[ -n "$default" ]] && echo -ne " [${default}]"
  echo -ne ": "
  read -r ans </dev/tty || true     # read from tty directly; never fails the script
  ans="${ans:-$default}"
  ans="${ans#"${ans%%[![:space:]]*}"}"   # trim leading whitespace
  ans="${ans%"${ans##*[![:space:]]}"}"   # trim trailing whitespace
  echo "$ans"
}

pause() {
  echo -e "\n${Y}  Press Enter to continue …${N}"
  read -r </dev/tty || true
}

menu_ext4() {
  while true; do
    banner
    echo -e "${W}  ── EXT4 Operations ──${N}\n"
    echo "  [1] Unpack image"
    echo "  [2] Repack image"
    echo "  [3] Quick Edit   (unpack → edit → repack in one flow)"
    echo "  [4] Batch Unpack (system/system_ext/product/vendor)"
    echo "  [5] Image Info"
    echo "  [6] Convert sparse → raw"
    echo "  [7] Convert raw → sparse"
    echo "  [0] Back"
    echo ""
    local choice; choice=$(prompt "Choice" "0")

    case "$choice" in
      1)
        local img; img=$(prompt "Image path" "")
        [[ -z "$img" ]] && continue
        local pname; pname=$(prompt "Partition name" "$(basename "$img" .img)")
        local outdir; outdir=$(prompt "Output directory" "$WORK_DIR/$pname")
        ( cmd_unpack "$img" "$pname" "$outdir" ) || { err "Unpack failed — check $LOG_FILE"; }
        pause ;;
      2)
        local outdir; outdir=$(prompt "Work directory (the one with META/ and fs/)" "")
        [[ -z "$outdir" ]] && continue
        local outimg; outimg=$(prompt "Output image path" "$WORK_DIR/$(basename "$outdir")_repacked.img")
        ( cmd_repack "$outdir" "$outimg" ) || { err "Repack failed — check $LOG_FILE"; }
        pause ;;
      3)
        local img; img=$(prompt "Image path" "")
        [[ -z "$img" ]] && continue
        local pname; pname=$(prompt "Partition name" "$(basename "$img" .img)")
        local outdir="$WORK_DIR/$pname"
        ( cmd_unpack "$img" "$pname" "$outdir" ) || { err "Unpack failed — check $LOG_FILE"; }
        echo -e "\n${Y}  Unpacked to: $outdir/fs${N}"
        echo -e "${Y}  Edit files now. Press Enter when done to repack …${N}"
        read -r </dev/tty || true
        local outimg; outimg=$(prompt "Output image path" "$WORK_DIR/${pname}_modified.img")
        ( cmd_repack "$outdir" "$outimg" ) || { err "Repack failed — check $LOG_FILE"; }
        pause ;;
      4)
        local imgdir; imgdir=$(prompt "Directory containing .img files" "")
        [[ -z "$imgdir" ]] && continue
        ( cmd_batch_unpack "$imgdir" ) || { err "Batch unpack failed — check $LOG_FILE"; }
        pause ;;
      5)
        local img; img=$(prompt "Image path" "")
        [[ -z "$img" ]] && continue
        ( cmd_info "$img" ) || true
        pause ;;
      6)
        local s; s=$(prompt "Sparse image path" "")
        [[ -z "$s" ]] && continue
        local r; r=$(prompt "Output raw path" "${s%.img}.raw.img")
        to_raw "$s" "$r"
        ok "Done: $r"
        pause ;;
      7)
        local r; r=$(prompt "Raw image path" "")
        [[ -z "$r" ]] && continue
        local s; s=$(prompt "Output sparse path" "${r%.raw.img}.sparse.img")
        to_sparse "$r" "$s"
        ok "Done: $s"
        pause ;;
      0) break ;;
    esac
  done
}

menu_super() {
  while true; do
    banner
    echo -e "${W}  ── Super.img Operations ──${N}\n"
    echo "  [1] Unpack super.img"
    echo "  [2] Repack super.img"
    echo "  [3] Full workflow: Unpack → Edit partitions → Repack"
    echo "  [0] Back"
    echo ""
    local choice; choice=$(prompt "Choice" "0")

    case "$choice" in
      1)
        local img; img=$(prompt "super.img path" "")
        [[ -z "$img" ]] && continue
        local outdir; outdir=$(prompt "Output directory" "$WORK_DIR/super_unpacked")
        ( cmd_super_unpack "$img" "$outdir" ) || { err "Super unpack failed — check $LOG_FILE"; }
        pause ;;
      2)
        local pdir; pdir=$(prompt "Partitions directory" "$WORK_DIR/super_unpacked/partitions")
        local outimg; outimg=$(prompt "Output super.img path" "$WORK_DIR/super_repacked.img")
        local metadir; metadir=$(prompt "META directory" "$(dirname "$pdir")/META")
        ( cmd_super_repack "$pdir" "$outimg" "$metadir" ) || { err "Super repack failed — check $LOG_FILE"; }
        pause ;;
      3)
        local img; img=$(prompt "super.img path" "")
        [[ -z "$img" ]] && continue
        local outdir="$WORK_DIR/super_unpacked"
        ( cmd_super_unpack "$img" "$outdir" ) || { err "Super unpack failed — check $LOG_FILE"; }
        echo -e "\n${Y}  Partitions unpacked to: $outdir/partitions${N}"
        echo -e "${Y}  You can now run EXT4 Unpack on each partition image.${N}"
        echo -e "${Y}  When done repacking partitions, press Enter to rebuild super.img …${N}"
        read -r </dev/tty || true
        local outsuper; outsuper=$(prompt "Output super.img path" "$WORK_DIR/super_modified.img")
        ( cmd_super_repack "$outdir/partitions" "$outsuper" "$outdir/META" ) || { err "Super repack failed — check $LOG_FILE"; }
        pause ;;
      0) break ;;
    esac
  done
}

main_menu() {
  while true; do
    banner
    echo -e "${W}  Main Menu${N}\n"
    echo "  [1] EXT4 Operations      (unpack / repack / edit)"
    echo "  [2] Super.img Operations (unpack / repack)"
    echo "  [3] Setup / Install deps"
    echo "  [4] Show workspace"
    echo "  [5] Fix permissions      (can't paste files? run this)"
    echo "  [0] Exit"
    echo ""
    local choice; choice=$(prompt "Choice" "0")

    case "$choice" in
      1) menu_ext4  ;;
      2) menu_super ;;
      3) cmd_setup  ;;
      4)
        echo -e "\n  Workspace: ${C}$WORK_DIR${N}"
        ls -lh "$WORK_DIR" 2>/dev/null || echo "  (empty)"
        pause ;;
      5)
        log "Fixing permissions on workspace …"
        own_dir "$WORK_DIR"
        ok "Done — all workspace dirs are now writable by $REAL_USER"
        pause ;;
      0)
        echo -e "\n${G}  Goodbye!${N}\n"
        exit 0 ;;
    esac
  done
}

# ─── CLI dispatch ─────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
${W}Android ROM Image Tool v${VERSION}${N}

Usage:
  $(basename "$0") [command] [options]

Commands:
  setup                        Install deps, prepare tools
  unpack   <img> [name] [dir]  Unpack ext4 image
  repack   <workdir> [out.img] Repack ext4 image
  info     <img>               Show image information
  super-unpack <super.img> [dir]           Unpack super.img
  super-repack <parts_dir> [out.img] [meta] Repack super.img
  batch-unpack <img_dir>       Batch unpack partition images

  (no args)                    Interactive menu

Examples:
  $(basename "$0") setup
  $(basename "$0") unpack system.img system ./work/system
  $(basename "$0") repack ./work/system ./out/system.img
  $(basename "$0") super-unpack super.img ./work/super
  $(basename "$0") super-repack ./work/super/partitions ./out/super.img
EOF
}

# ─── Entry point ──────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

case "${1:-}" in
  setup)         cmd_setup ;;
  unpack)        cmd_unpack "${2:-}" "${3:-}" "${4:-}" ;;
  repack)        cmd_repack "${2:-}" "${3:-}" ;;
  info)          cmd_info   "${2:-}" ;;
  super-unpack)  cmd_super_unpack "${2:-}" "${3:-}" ;;
  super-repack)  cmd_super_repack "${2:-}" "${3:-}" "${4:-}" ;;
  batch-unpack)  cmd_batch_unpack "${2:-}" "${3:-}" ;;
  fix-perms)
    TARGET="${2:-$WORK_DIR}"
    log "Fixing permissions on: $TARGET"
    own_dir "$TARGET"
    ok "Done — $TARGET is now writable by $REAL_USER"
    ;;
  help|--help|-h) usage ;;
  "")            main_menu ;;
  *)             usage; exit 1 ;;
esac
