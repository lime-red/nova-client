#!/usr/bin/env bash
# new-league-game.sh - make this board's game folder for a new BRE or FE league
# from a golden image, and RESET it, ready for the league's reset from the hub.
#
#   tools/new-league-game.sh --image ~/bre-sj1-r2.tar.gz --league 969
#
# Everything about this board comes from the client's own config.toml, the
# same settings the client plays the game with:
#   - the new league's game_folder and game_dos_path, from its [leagues.<GAME>.<NNN>]
#     section. Not there yet? They are made from another league's (--like),
#     with its number changed, and the section to add is printed at the end.
#   - [daemon] dosemu_drives, for a board that reaches its games through a
#     drive other than C:.
#   - bbs.cfg, from the --like league's folder: its inbound and netmail paths
#     and league number are changed to the new league's.
#
# The easy order: add the league to config.toml, let the client sync once (it
# makes the folder's inbound and downloads the hub's nodelist and reset into
# it), then run this. Running it first works too, given --nodes.
#
# What it does:
#   1. Works everything out and shows the plan before changing anything.
#   2. Stops the client's service, if one runs with this config (sudo).
#   3. Moves an existing folder aside to <folder>.replaced-<time> (never deletes
#      it), setting aside what is waiting in its inbound.
#   4. Unpacks the image, writes bbs.cfg, installs the nodelist.
#   5. Gives the folder the --like folder's group, setgid on every directory.
#   6. Runs <GAME> RESET under dosemu for you to answer (RESET empties inbound,
#      which is why inbound's files were set aside), then puts them back.
#   7. Starts the service again.
#
# A fresh install, never a copy of a league you already play: a played game
# remembers its league's coordinator and rejects a new league's reset.
set -euo pipefail

me=$(basename "$0")
here=$(cd "$(dirname "$0")" && pwd)
die() { echo "$me: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'
    cat <<'EOF'
Options:
  --image FILE      golden image (bre-*.tar.gz or fe-*.tar.gz)             required
  --league NNN      the new league's number                                required
  --like NNN        a league of the same game in config.toml to copy bbs.cfg
                    and the folder naming from (default: the only one there is)
  --config FILE     the client's config.toml
                    (default: python/config.toml beside this script)
  --nodes FILE      the league's nodelist, if the hub's isn't in inbound yet
  --service UNIT    the client's systemd unit (default: the one running with
                    --config, found by itself); --service none to leave it be
  --no-reset        stop before RESET (you then RESET by hand, minding inbound)
  -y                don't ask before starting
  -h, --help        this
EOF
}

image= league= like= config= nodes= service= no_reset= yes=
while [ $# -gt 0 ]; do
    case $1 in
        --image)   image=$2; shift ;;
        --league)  league=$2; shift ;;
        --like)    like=$2; shift ;;
        --config)  config=$2; shift ;;
        --nodes)   nodes=$2; shift ;;
        --service) service=$2; shift ;;
        --no-reset) no_reset=1 ;;
        -y)        yes=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ -n "$image" ] && [ -n "$league" ] || { usage >&2; exit 2; }
[ -f "$image" ] || die "no image at $image"
for n in "$league" ${like:+"$like"}; do
    [[ $n =~ ^[0-9]{1,3}$ ]] || die "a league number is up to 3 digits, not '$n'"
done
league=$(printf '%03d' "$((10#$league))")
[ -z "$like" ] || like=$(printf '%03d' "$((10#$like))")
command -v python3 >/dev/null || die "python3 is needed"
config=${config:-$here/../python/config.toml}
[ -r "$config" ] || die "can't read the client's config at $config (give --config)"
config=$(realpath "$config")

# The file or folder called $2 (any case) in folder $1, or nothing.
ci() { [ -d "$1" ] && find "$1" -maxdepth 1 -iname "$2" -print -quit 2>/dev/null || true; }

# --- the image -------------------------------------------------------------
top=$(tar -tzf "$image" | awk -F/ 'NR == 1 { print $1 }')   # not head: SIGPIPE under pipefail
manifest=$(tar -xzOf "$image" "$top/manifest.json" 2>/dev/null) \
    || die "$image has no $top/manifest.json: not a golden image"
read -r game recipe version < <(python3 -c '
import json, sys
m = json.loads(sys.stdin.read())
print(m["game"], m.get("recipe", "?"), m.get("version", "?"))' <<<"$manifest")
case $game in
    B) exe=BRE; nodes_name=BRNODES.DAT; nodelist=BRNODES.$league ;;
    F) exe=FE;  nodes_name=FENODES.DAT; nodelist=FENODES.$league ;;
    *) die "this makes BRE and FE folders; $image is game '$game'" ;;
esac
[ "$recipe" != "1" ] || die "$image is a recipe 1 image: it carries the donor league's COORD.KEY. Use an r2 image."

# --- the board, from the client's config -----------------------------------
# One line of tab-separated fields; python does the TOML and the path rules.
plan=$(python3 - "$config" "$exe" "$league" "$like" <<'EOF'
import os, sys, tomllib
from pathlib import Path, PureWindowsPath

config, exe, league, like = sys.argv[1:]
cfg = tomllib.load(open(config, "rb"))
leagues = {f"{int(k):03d}": v for k, v in cfg.get("leagues", {}).get(exe, {}).items()}
daemon = cfg.get("daemon", {})
fail = lambda msg: sys.exit(f"{config}: {msg}")

if not like:
    others = sorted(k for k in leagues if k != league)
    if len(others) != 1:
        fail(f"give --like: {exe} leagues here are {', '.join(others) or 'none'}")
    like = others[0]
if like not in leagues:
    fail(f"no [leagues.{exe}.{like}] to take after")
src = leagues[like]
if not src.get("game_folder") or not src.get("game_dos_path"):
    fail(f"[leagues.{exe}.{like}] has no game_folder / game_dos_path")

def renumber(name: str) -> str:
    """bre_015 -> bre_969, BRE015 -> BRE969: the old league's number, swapped."""
    for old in (like, str(int(like))):
        if old in name:
            return name.replace(old, league)
    fail(f"can't see league {like} in '{name}' to make {league}'s name from it; "
         f"add [leagues.{exe}.{league}] with its game_folder and game_dos_path first")

new_section = league not in leagues
if new_section:
    folder = str(Path(src["game_folder"]).with_name(renumber(Path(src["game_folder"]).name)))
    dos = PureWindowsPath(src["game_dos_path"])
    dos_path = str(dos.with_name(renumber(dos.name)))
else:
    dst = leagues[league]
    folder, dos_path = dst.get("game_folder"), dst.get("game_dos_path")
    if not folder or not dos_path:
        fail(f"[leagues.{exe}.{league}] has no game_folder / game_dos_path")

# Check DOS reaches the folder where game_dos_path says: C: is ~/.dosemu/drive_c,
# other letters are [daemon] dosemu_drives, as the client maps them.
drives = {str(k).rstrip(":").upper(): os.path.realpath(v)
          for k, v in (daemon.get("dosemu_drives") or {}).items()}
roots = dict(drives, C=os.path.realpath(os.path.expanduser("~/.dosemu/drive_c")))
folder = os.path.realpath(os.path.expanduser(folder))
letter = dos_path[:1].upper()
if dos_path[1:2] != ":" or letter not in roots:
    fail(f"game_dos_path {dos_path}: drive {letter}: is neither C: nor in [daemon] dosemu_drives")
rel = os.path.relpath(folder, roots[letter])
if rel.startswith("..") or rel.replace("/", "\\").lower() != dos_path[3:].lower():
    fail(f"game_folder {folder} is not where {dos_path} points ({roots[letter]} is {letter}:)")
for part in PureWindowsPath(dos_path).parts[1:]:
    stem, _, ext = part.partition(".")
    if len(stem) > 8 or len(ext) > 3 or " " in part:
        fail(f"'{part}' in {dos_path} is not a DOS 8.3 name: the game would silently read nothing")

like_folder = os.path.realpath(os.path.expanduser(src["game_folder"]))
fields = [like, folder, dos_path, like_folder, "1" if new_section else "0",
          daemon.get("dosemu_path", "dosemu"), " ".join(f"{k}={v}" for k, v in drives.items())]
print("\t".join(fields))
EOF
) || exit 1
IFS=$'\t' read -r like folder dos_path like_folder new_section dosemu_bin drive_list <<<"$plan"
parent=$(dirname "$folder")
name=$(basename "$folder")
[ -d "$parent" ] || die "$parent does not exist"
[ -n "$no_reset" ] || command -v "$dosemu_bin" >/dev/null || die "no dosemu at '$dosemu_bin'"

cfg_from=$(ci "$like_folder" bbs.cfg)
[ -n "$cfg_from" ] || die "league $like's folder $like_folder has no bbs.cfg"
group=$(stat -c %G "$like_folder")
id -nG | tr ' ' '\n' | grep -qx "$group" || die "you are not in group '$group', which $like_folder belongs to"

# The inbound and netmail folder names, from league $like's bbs.cfg.
read -r in_name net_name < <(python3 - "$cfg_from" <<'EOF'
import sys
lines = open(sys.argv[1], "rb").read().decode("cp437").splitlines()
if len(lines) < 6:
    sys.exit(f"{sys.argv[1]} has {len(lines)} lines; a bbs.cfg has at least 6")
last = lambda s: s.strip().rstrip("\\").split("\\")[-1] or "INBOUND"
print(last(lines[3]), last(lines[4]))
EOF
)

# --- what is there already ------------------------------------------------------
old=
[ -e "$folder" ] && old="$folder.replaced-$(date +%Y%m%d-%H%M%S)"
old_inbound=$(ci "$folder" "$in_name")
waiting=()
if [ -n "$old_inbound" ]; then
    while IFS= read -r -d '' f; do waiting+=("$f"); done \
        < <(find "$old_inbound" -maxdepth 1 -type f -print0)
fi

nodes_from=$nodes
[ -n "$nodes_from" ] || nodes_from=$(ci "$old_inbound" "$nodelist")
[ -n "$nodes_from" ] || nodes_from=$(ci "$folder" "$nodes_name")
[ -n "$nodes_from" ] && [ -f "$nodes_from" ] || die "no nodelist for league $league yet.
  Add the league to $config and let the client sync once: it downloads
  $nodelist into $name's inbound. Or give --nodes <file>."

if [ -z "$service" ]; then
    while read -r unit _; do
        systemctl cat "$unit" 2>/dev/null | grep -q -- "--config $config\b" && { service=$unit; break; }
    done < <(systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null)
fi
[ "$service" != none ] || service=

# --- the plan ------------------------------------------------------------------
say "Making $exe league $league at $folder ($dos_path)"
say "  image     $image  ($version, recipe $recipe)"
say "  config    $config"
[ "$new_section" = 0 ] || say "            (no [leagues.$exe.$league] there yet: named after league $like)"
[ -z "$old" ] || say "  existing  moved aside to $(basename "$old")"
say "  bbs.cfg   league $like's, with league $league's paths and number"
say "  nodelist  $nodes_from"
say "  inbound   ${#waiting[@]} waiting file(s), set aside during RESET and put back"
say "  group     $group (as league $like's folder), setgid folders"
[ -z "$drive_list" ] || say "  drives    $drive_list"
if [ -n "$service" ]; then say "  service   $service, stopped meanwhile (sudo)"; else say "  service   none found running with this config"; fi
if [ -n "$no_reset" ]; then say "  RESET     not run (--no-reset)"; else say "  RESET     $exe RESET under dosemu, for you to answer"; fi
if [ -z "$yes" ]; then
    read -r -p "Go ahead? [y/N] " ok
    [[ $ok == [yY]* ]] || die "nothing changed"
fi

# --- doing it --------------------------------------------------------------------
umask 002
stash= batch_dir= stopped=
finish() {
    rc=$?
    [ -z "$batch_dir" ] || rm -rf "$batch_dir"
    if [ -n "$stash" ] && [ -d "$stash" ]; then
        if [ "$rc" -ne 0 ] && [ -n "$(ls -A "$stash")" ]; then
            echo "$me: failed. The waiting inbound files are kept in $stash" >&2
        else
            rmdir "$stash" 2>/dev/null || true
        fi
    fi
    if [ -n "$stopped" ]; then
        say "Starting $service again"
        sudo systemctl start "$service" || echo "$me: could not start $service" >&2
    fi
    exit "$rc"
}
trap finish EXIT

if [ -n "$service" ] && systemctl is-active --quiet "$service"; then
    say "Stopping $service"
    sudo systemctl stop "$service"
    stopped=1
fi

stash=$(mktemp -d "$parent/.$name.inbound.XXXXXX")
for f in "${waiting[@]}"; do mv "$f" "$stash/"; done
[ "${#waiting[@]}" -eq 0 ] || say "Set aside ${#waiting[@]} inbound file(s) in $stash"
case $nodes_from in
    "$old_inbound"/*) [ -z "$old_inbound" ] || nodes_from=$stash/$(basename "$nodes_from") ;;
esac
if [ -n "$old" ]; then
    mv "$folder" "$old"
    case $nodes_from in "$folder"/*) nodes_from=$old/${nodes_from#"$folder"/} ;; esac
fi

mkdir "$folder"
tar -xzf "$image" -C "$folder" --strip-components=1 --no-same-owner

# The image's INBOUND / NETMAIL, named as bbs.cfg names them.
for want in "$in_name" "$net_name"; do
    have=$(ci "$folder" "$want")
    if [ -z "$have" ]; then mkdir "$folder/$want"
    elif [ "$(basename "$have")" != "$want" ]; then mv "$have" "$folder/$want"
    fi
done

# bbs.cfg / BBS.CFG and BRNODES.DAT / brnodes.dat: named as league $like's folder
# names them, and only once: a case twin leaves the game reading the wrong one.
cfg_name=$(basename "$cfg_from")
nodes_dst=$(ci "$like_folder" "$nodes_name"); nodes_dst=$(basename "${nodes_dst:-$nodes_name}")
find "$folder" -maxdepth 1 \( -iname bbs.cfg -o -iname "$nodes_name" \) -delete

python3 - "$cfg_from" "$folder/$cfg_name" "$dos_path" "$league" <<'EOF'
import sys
src, dst, dos, league = sys.argv[1:]
lines = open(src, "rb").read().decode("cp437").splitlines()
last = lambda s: s.strip().rstrip("\\").split("\\")[-1]
lines[3] = f"{dos}\\{last(lines[3])}"
lines[4] = f"{dos}\\{last(lines[4])}"
lines[5] = league
open(dst, "wb").write(("\r\n".join(lines) + "\r\n").encode("cp437"))
EOF
cp "$nodes_from" "$folder/$nodes_dst"

fix_group() {
    chgrp -R "$group" "$folder"
    chmod -R g+rwX "$folder"
    find "$folder" -type d -exec chmod g+s {} +
}
fix_group
say "Unpacked $version into $folder"

if [ -z "$no_reset" ]; then
    batch_dir=$(mktemp -d)
    conf=$batch_dir/reset.conf
    {
        printf '%s\n' '$_layout = "us"' '$_floppy_a = ""' '$_xms = (8192)' '$_ems = (8192)' \
            '$_com1 = ""' '$_com2 = ""'
        [ -z "$drive_list" ] || printf '$_lredir_paths = "%s"\n' "$(tr ' ' '\n' <<<"$drive_list" | cut -d= -f2- | tr '\n' ' ' | sed 's/ $//')"
    } >"$conf"
    {
        printf '@ECHO OFF\r\n'
        for d in $drive_list; do printf 'lredir -f %s: %s\r\n' "${d%%=*}" "${d#*=}"; done
        printf '%s\r\nCD %s\r\n%s RESET\r\nEXIT\r\n' "${dos_path:0:2}" "$dos_path" "$exe"
    } >"$batch_dir/RESET.BAT"

    cat <<EOF

Now $exe RESET, under dosemu. In it:
  - "reset the Game?"           answer Y
  - the Configuration Editor    press ESC to leave it, changing nothing (dosemu can
                                swallow a lone ESC: press again if nothing happens).
                                The league's reset from the hub sets these values.
  - "league-wide reset?"        answer N. A member board isn't asked this: if you
                                are, this board is the HOST in the nodelist.
EOF
    read -r -p "Press Enter to start dosemu. " _
    TERM=${TERM:-linux}
    [ "$TERM" != dumb ] || TERM=linux   # dosemu2 dies under TERM=dumb
    export TERM
    # -K/-E, never a bare path to the batch: that remaps C: and the game never runs.
    "$dosemu_bin" -f "$conf" -K "$batch_dir" -E RESET.BAT || true
    flag=$(ci "$folder" inuse.flg); [ -z "$flag" ] || rm -f "$flag"

    data=$(ci "$folder" DATA)
    [ -n "$(ci "$data" game.dat)" ] || die "RESET did not make DATA/game.dat in $folder.
  Run it by hand ($exe RESET in $dos_path), with inbound emptied first."
    say "RESET made $(ls "$data" | tr '\n' ' ')"
fi

for f in "$stash"/*; do [ -e "$f" ] && mv "$f" "$folder/$in_name/"; done
fix_group

say ""
say "Done: $folder is league $league's $exe game for this board."
[ -z "$old" ] || say "The old folder is at $old: delete it once the league is running."
if [ "$new_section" = 1 ]; then
    say ""
    say "Add this to $config, beside [leagues.$exe.$like]:"
    python3 - "$config" "$exe" "$like" "$league" "$folder" "$dos_path" <<'EOF'
import sys, tomllib
config, exe, like, league, folder, dos = sys.argv[1:]
src = {f"{int(k):03d}": v for k, v in tomllib.load(open(config, "rb"))["leagues"][exe].items()}[like]
print(f"\n[leagues.{exe}.{league}]")
for key, value in src.items():
    value = {"game_folder": folder, "game_dos_path": dos}.get(key, value)
    if isinstance(value, bool):
        print(f"{key} = {str(value).lower()}")
    elif isinstance(value, (int, float)):
        print(f"{key} = {value}")
    else:
        print(f'{key} = "' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"')
EOF
fi
say ""
if [ -n "$no_reset" ]; then
    say "Next: $exe RESET in $dos_path. It empties $in_name, so move its files out and back."
elif [ "${#waiting[@]}" -gt 0 ]; then
    say "${#waiting[@]} file(s) back in $in_name: the client's next maintenance takes them in."
    say "If none of them is a reset the hub sent after this board's game was replaced,"
    say "ask the hub to reset the league again."
else
    say "Next: ask the hub to reset the league; this game takes it in at its next maintenance."
fi
