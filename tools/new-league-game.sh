#!/usr/bin/env bash
# new-league-game.sh - make this board's game folder for a new BRE or FE league,
# from a golden image, and RESET it, ready for the league's reset from the hub.
#
#   tools/new-league-game.sh --image ~/bre-sj1-r2.tar.gz --league 969 \
#       --folder /opt/eqx/bbs/EZY/DOORS/bre_969 --like /opt/eqx/bbs/EZY/DOORS/bre_015 \
#       --drive M=/opt/eqx/bbs --service eqx-nova
#
# What it does, in order:
#   1. Works everything out and shows the plan before changing anything.
#   2. Stops --service, if given, so the client's maintenance can't run mid-way.
#   3. Moves an existing --folder aside to <folder>.replaced-<time> (never deletes it),
#      keeping whatever is waiting in its inbound.
#   4. Unpacks the image, and writes bbs.cfg: --like's (or --bbs-cfg's), with the
#      inbound and netmail paths and the league number changed to this league's.
#   5. Installs the league's nodelist: --nodes, else the hub's from inbound.
#   6. Gives the folder to the client's group, setgid on every directory, so
#      files you or the game make later stay usable by the client.
#   7. Moves inbound's files out of the way (RESET empties inbound), runs
#      <GAME> RESET under dosemu for you to answer, and puts them back.
#   8. Starts --service again.
#
# A fresh install, not a copy of a league you already play: a played game
# remembers its league's coordinator and rejects a new league's reset.
set -euo pipefail

me=$(basename "$0")
die() { echo "$me: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'
    cat <<'EOF'
Options:
  --image FILE      golden image (bre-*.tar.gz or fe-*.tar.gz)             required
  --league NNN      the league's number                                    required
  --folder DIR      the game folder to make, e.g. .../DOORS/bre_969        required
  --like DIR        a league folder on this board to take bbs.cfg from
  --bbs-cfg FILE    or a bbs.cfg to take it from   (one of these two, unless
                    --folder exists already and has a bbs.cfg of its own)
  --nodes FILE      the league's nodelist, if the hub's isn't in inbound yet
  --drive L=PATH    a dosemu drive the BBS reaches its games through, as in the
                    client's [daemon] dosemu_drives (repeat for more). Without
                    one, the folder must be under ~/.dosemu/drive_c.
  --service UNIT    the client's systemd unit, stopped while this runs (sudo)
  --group GROUP     the client's group (default: --folder's parent's group)
  --dosemu PATH     dosemu to run (default: dosemu on PATH)
  --no-reset        stop before RESET (you then RESET by hand, minding inbound)
  -y                don't ask before starting
  -h, --help        this
EOF
}

image= league= folder= like= bbs_cfg= nodes= service= group= no_reset= yes=
dosemu_bin=dosemu
drives=()
while [ $# -gt 0 ]; do
    case $1 in
        --image)   image=$2; shift ;;
        --league)  league=$2; shift ;;
        --folder)  folder=$2; shift ;;
        --like)    like=$2; shift ;;
        --bbs-cfg) bbs_cfg=$2; shift ;;
        --nodes)   nodes=$2; shift ;;
        --drive)   drives+=("$2"); shift ;;
        --service) service=$2; shift ;;
        --group)   group=$2; shift ;;
        --dosemu)  dosemu_bin=$2; shift ;;
        --no-reset) no_reset=1 ;;
        -y)        yes=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ -n "$image" ] && [ -n "$league" ] && [ -n "$folder" ] || { usage >&2; exit 2; }
[ -f "$image" ] || die "no image at $image"
[[ $league =~ ^[0-9]{1,3}$ ]] || die "league must be a number up to 999, not '$league'"
league=$(printf '%03d' "$((10#$league))")
command -v python3 >/dev/null || die "python3 is needed"
[ -n "$no_reset" ] || command -v "$dosemu_bin" >/dev/null || die "no dosemu at '$dosemu_bin'"

folder=$(realpath -m "$folder")
parent=$(dirname "$folder")
name=$(basename "$folder")
[ -d "$parent" ] || die "$parent does not exist"

# The file or folder called $2 (any case) in folder $1, or nothing.
ci() { find "$1" -maxdepth 1 -iname "$2" -print -quit 2>/dev/null; }

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
    *) die "this script makes BRE and FE folders; $image is game '$game'" ;;
esac
[ "$recipe" != "1" ] || die "$image is a recipe 1 image: it carries the donor league's COORD.KEY. Use an r2 image."

# --- where DOS sees the folder ---------------------------------------------
dos_path= drive_letters=() drive_paths=()
for d in "${drives[@]}"; do
    [[ $d =~ ^([A-Za-z]):?=(/.*)$ ]] || die "--drive takes L=/host/path, not '$d'"
    letter=${BASH_REMATCH[1]^^}; path=$(realpath -m "${BASH_REMATCH[2]}")
    [ "$letter" != C ] || die "--drive C is dosemu's own drive_c; give another letter"
    drive_letters+=("$letter"); drive_paths+=("$path")
    case $folder/ in
        "$path"/*) [ -n "$dos_path" ] || dos_path="$letter:${folder#"$path"}" ;;
    esac
done
if [ -z "$dos_path" ]; then
    drive_c=$(realpath -m ~/.dosemu/drive_c)
    case $folder/ in
        "$drive_c"/*) dos_path="C:${folder#"$drive_c"}" ;;
        *) die "$folder is not under ~/.dosemu/drive_c or any --drive: dosemu can't reach it" ;;
    esac
fi
dos_path=${dos_path//\//\\}
IFS='\' read -r -a parts <<<"${dos_path#?:}"
for p in "${parts[@]}"; do
    [ -z "$p" ] && continue
    [ ${#p} -le 8 ] && [[ $p != *" "* ]] || die "'$p' in $dos_path is not a DOS 8.3 name: the game would silently read nothing from inbound"
done

# --- what the new folder takes from where ------------------------------------
old=
[ -e "$folder" ] && old="$folder.replaced-$(date +%Y%m%d-%H%M%S)"

cfg_from=
if [ -n "$bbs_cfg" ]; then cfg_from=$bbs_cfg
elif [ -n "$like" ]; then cfg_from=$(ci "$like" bbs.cfg)
elif [ -d "$folder" ]; then cfg_from=$(ci "$folder" bbs.cfg)
fi
[ -n "$cfg_from" ] && [ -f "$cfg_from" ] \
    || die "no bbs.cfg to start from: give --like <a league folder> or --bbs-cfg <file>"

# The inbound and netmail folder names, from the bbs.cfg we start from.
read -r in_name net_name < <(python3 - "$cfg_from" <<'EOF'
import sys
lines = open(sys.argv[1], "rb").read().decode("cp437").splitlines()
if len(lines) < 6:
    sys.exit(f"{sys.argv[1]} has {len(lines)} lines; a bbs.cfg has at least 6")
last = lambda s: s.strip().rstrip("\\").split("\\")[-1] or "x"
print(last(lines[3]), last(lines[4]))
EOF
)
for n in "$in_name" "$net_name"; do
    [ ${#n} -le 8 ] || die "'$n' (from $cfg_from) is not a DOS 8.3 name"
done

# What is waiting in the old folder's inbound: the hub's packets for this league.
old_inbound=
[ -d "$folder" ] && old_inbound=$(ci "$folder" "$in_name")
waiting=()
if [ -n "$old_inbound" ]; then
    while IFS= read -r -d '' f; do waiting+=("$f"); done \
        < <(find "$old_inbound" -maxdepth 1 -type f -print0)
fi

nodes_from=$nodes
if [ -z "$nodes_from" ] && [ -n "$old_inbound" ]; then nodes_from=$(ci "$old_inbound" "$nodelist"); fi
if [ -z "$nodes_from" ] && [ -d "$folder" ]; then
    nodes_from=$(ci "$folder" "$nodes_name")
    [ -z "$nodes_from" ] || say "note: no $nodelist waiting in inbound; using the old folder's $nodes_name"
fi
[ -n "$nodes_from" ] && [ -f "$nodes_from" ] || die "no nodelist for league $league.
  Give --nodes <file>, or add the league to the client's config and let it download
  $nodelist into the folder's inbound first, then run this again."

[ -n "$group" ] || group=$(stat -c %G "$parent")
id -nG | tr ' ' '\n' | grep -qx "$group" \
    || die "you are not in group '$group'; add yourself (and log in again) or give --group"

# --- the plan ------------------------------------------------------------------
say "Making $exe league $league at $folder ($dos_path)"
say "  image     $image  ($version, recipe $recipe)"
[ -z "$old" ] || say "  existing  moved aside to $old"
say "  bbs.cfg   from $cfg_from: inbound/netmail under $dos_path, league $league"
say "  nodelist  from $nodes_from"
say "  inbound   ${#waiting[@]} waiting file(s), set aside during RESET and put back"
say "  group     $group, setgid folders, umask 002"
[ -z "$service" ] || say "  service   $service stopped meanwhile (sudo)"
if [ -n "$no_reset" ]; then say "  RESET     not run (--no-reset)"; else say "  RESET     $exe RESET, under dosemu, for you to answer"; fi
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
# The nodelist moved with them.
[ -z "$old_inbound" ] || case $nodes_from in
    "$old_inbound"/*) nodes_from=$stash/$(basename "$nodes_from") ;;
esac

if [ -n "$old" ]; then
    mv "$folder" "$old"
    case $cfg_from in "$folder"/*) cfg_from=$old/${cfg_from#"$folder"/} ;; esac
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

python3 - "$cfg_from" "$folder/bbs.cfg" "$dos_path" "$league" <<'EOF'
import sys
src, dst, dos, league = sys.argv[1:]
raw = open(src, "rb").read().decode("cp437")
lines = raw.splitlines()
last = lambda s: s.strip().rstrip("\\").split("\\")[-1]
lines[3] = f"{dos}\\{last(lines[3])}"
lines[4] = f"{dos}\\{last(lines[4])}"
lines[5] = league
open(dst, "wb").write(("\r\n".join(lines) + "\r\n").encode("cp437"))
EOF
cp "$nodes_from" "$folder/$nodes_name"

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
        [ ${#drive_paths[@]} -eq 0 ] || printf '$_lredir_paths = "%s"\n' "${drive_paths[*]}"
    } >"$conf"
    {
        printf '@ECHO OFF\r\n'
        for i in "${!drive_letters[@]}"; do
            printf 'lredir -f %s: %s\r\n' "${drive_letters[$i]}" "${drive_paths[$i]}"
        done
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

inbound=$folder/$in_name
for f in "$stash"/*; do [ -e "$f" ] && mv "$f" "$inbound/"; done
fix_group

say ""
say "Done: $folder is league $league's $exe game for this board."
[ -z "$old" ] || say "The old folder is at $old: delete it once the league is running."
if [ -n "$no_reset" ]; then
    say "Next: $exe RESET in $dos_path. It empties $in_name, so move its files out and back."
elif [ "${#waiting[@]}" -gt 0 ]; then
    say "${#waiting[@]} file(s) back in $in_name: the client's next maintenance takes them in."
    say "If none of them is a reset sent after this board's game was replaced, ask the hub to reset the league."
else
    say "Next: add league $league to the client's config, if it isn't there yet, and ask the hub"
    say "to reset the league: this game takes that reset in at its next maintenance."
fi
