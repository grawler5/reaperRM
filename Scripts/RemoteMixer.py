# -*- coding: utf-8 -*-
# RemoteMixer.py (v5.13.1) - REAPER ReaScript (Python)
# TCP client -> Node server (localhost), sends state+meters, receives control commands.
# Key fixes:
#  - Stable GUID strings via guidToString to avoid UI "blinking/empty" when pointer IDs change
#  - Faster meter updates (default ~30Hz)
#  - Rec input selection support
#  - FX list/params + add FX by name support
#
# Install: put into REAPER/Scripts/RemoteMixer/RemoteMixer.py and run from Actions.

from __future__ import print_function
import sys
import time
import json
import socket
import traceback
import math
import os

VERSION = "5.13.2"
HOST = "127.0.0.1"
PORT = 7071

# meters/state pacing
STATE_INTERVAL = 0.25
METER_INTERVAL = 0.020  # ~50Hz (smoother meters)

# --- REAPER API shim ---
# REAPER injects RPR_* functions into globals.
# We'll reference them directly; guard failures gracefully.


def get_project_info():
    """Returns (projectName, projectPath) for current project tab."""
    name = "Untitled"
    path = ""
    try:
        ret = RPR_EnumProjects(-1, "", 2048)
        proj = None
        if isinstance(ret, tuple):
            proj = ret[0]
            # Find first human-looking path string
            p = _pick_human_string(ret[1:], "")
            if p:
                path = p
        # Project name from project handle
        if proj:
            ret2 = RPR_GetProjectName(proj, "", 512)
            nm = _pick_human_string(ret2, "")
            if nm:
                name = nm
    except Exception:
        pass
    try:
        name = str(name) if name else "Untitled"
    except Exception:
        name = "Untitled"
    try:
        path = str(path) if path else ""
    except Exception:
        path = ""
    return name, path

def _now():
    try:
        return time.time()
    except Exception:
        return 0.0

def _log_path():
    # __file__ may be undefined in REAPER; try resource path
    try:
        script_dir = globals().get("SCRIPT_DIR")
        if not script_dir:
            # try REAPER resource path
            try:
                r = RPR_GetResourcePath()
                if isinstance(r, tuple):
                    r = r[0]
                script_dir = os.path.join(str(r), "Scripts", "RemoteMixer")
            except Exception:
                script_dir = "."
        return os.path.join(script_dir, "RemoteMixer.log")
    except Exception:
        return "RemoteMixer.log"

LOG_PATH = _log_path()

def log(*a):
    try:
        s = " ".join([str(x) for x in a])
        line = "[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), s)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except Exception:
        pass

def _as_str(x):
    try:
        if x is None:
            return ""
        if isinstance(x, bytes):
            return x.decode("utf-8", "replace")
        return str(x)
    except Exception:
        return ""


def _looks_like_ptr(s):
    try:
        s = _as_str(s).strip()
        # Typical wrapper pointers: '(MediaTrack*)0x0000021...' or '(GUID*)0x...'
        if not s:
            return False
        if "0x" in s and s.startswith("(") and ")" in s:
            return True
        # Occasionally just '0x....'
        if s.startswith("0x") and len(s) > 6:
            return True
        return False
    except Exception:
        return False

def _pick_human_string(ret, default=""):
    """Pick the most likely human-readable string from REAPER tuple returns."""
    try:
        if isinstance(ret, tuple):
            cand = []
            for part in ret:
                if part is None:
                    continue
                if isinstance(part, (bytes, str)):
                    s = _as_str(part)
                else:
                    # skip numbers and pointers
                    continue
                s2 = s.strip()
                if not s2:
                    continue
                if _looks_like_ptr(s2):
                    continue
                cand.append(s2)
            if cand:
                # Prefer the last non-pointer string (wrappers often put it last)
                return cand[-1]
            # Fallback: any string
            for part in ret:
                if isinstance(part, (bytes, str)):
                    return _as_str(part)
            return default
        # Non-tuple
        if isinstance(ret, (bytes, str)):
            s = _as_str(ret).strip()
            return "" if _looks_like_ptr(s) else (s or default)
        return default
    except Exception:
        return default

def _pick_num(ret, default=0.0):
    try:
        if isinstance(ret, tuple):
            for part in ret:
                if isinstance(part, (int, float)):
                    return part
        if isinstance(ret, (int, float)):
            return ret
        try:
            return float(ret)
        except Exception:
            return default
    except Exception:
        return default


def _safe_json(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",",":"))
    except Exception:
        try:
            return json.dumps(obj, ensure_ascii=True, separators=(",",":"))
        except Exception:
            return "{}"

def _send(sock, obj):
    try:
        data = (_safe_json(obj) + "\n").encode("utf-8", "replace")
        sock.sendall(data)
        return True
    except Exception:
        return False

def _recv_lines(sock):
    # non-blocking-ish line reader
    try:
        sock.settimeout(0.0)
        try:
            data = sock.recv(65536)
        except BlockingIOError:
            return []
        except Exception:
            return []
        if not data:
            return ["__EOF__"]
        return data.decode("utf-8", "replace").splitlines()
    except Exception:
        return []

# --- GUID handling ---
def _guid_to_string(guid_ptr):
    """Convert REAPER GUID pointer-like values into stable '{...}' string where possible."""
    try:
        s = _as_str(guid_ptr).strip()
        if s.startswith("{") and s.endswith("}"):
            return s
        # Wrapper pointers are unstable; try guidToString if available
        if "RPR_guidToString" in globals():
            try:
                ret = RPR_guidToString(guid_ptr, "", 128)
                s2 = _pick_human_string(ret, "")
                if s2 and s2.startswith("{") and s2.endswith("}"):
                    return s2
            except Exception:
                pass
        # If still looks like pointer, return as-is (caller may replace for MASTER)
        return s
    except Exception:
        return _as_str(guid_ptr)

def track_guid(track):
    try:
        if _is_master_track(track):
            return "MASTER"
        g = RPR_GetTrackGUID(track)
        return _guid_to_string(g)
    except Exception:
        return _as_str(track)

def _is_master_track(track):
    try:
        m = RPR_GetMasterTrack(0)
        if isinstance(m, tuple): m = m[0]
        # Pointer equality works in wrapper; fallback to string compare
        return (track == m) or (_as_str(track) == _as_str(m))
    except Exception:
        return False

# --- State gathering ---
def track_color_hex(track):
    """
    Returns '#rrggbb' or '' if no custom color.
    Uses GetTrackColor when available; falls back to I_CUSTOMCOLOR.
    """
    def _native_to_hex(native_int):
        try:
            native_int = int(native_int)
        except Exception:
            return ""
        if native_int == 0:
            return ""
        try:
            if "RPR_ColorFromNative" in globals():
                r,g,b = RPR_ColorFromNative(native_int)
                if isinstance(r, tuple): r=r[0]
                if isinstance(g, tuple): g=g[0]
                if isinstance(b, tuple): b=b[0]
                return "#{:02x}{:02x}{:02x}".format(int(r)&255,int(g)&255,int(b)&255)
        except Exception:
            pass
        # Fallback bit ops (best-effort)
        r = (native_int & 255)
        g = (native_int >> 8) & 255
        b = (native_int >> 16) & 255
        return "#{:02x}{:02x}{:02x}".format(r,g,b)

    try:
        c = RPR_GetTrackColor(track)
        if isinstance(c, tuple): c = c[0]
        c = int(c)
        if c != 0:
            return _native_to_hex(c)
    except Exception:
        pass

    # Fallback: I_CUSTOMCOLOR (has 0x1000000 flag)
    try:
        c2 = RPR_GetMediaTrackInfo_Value(track, "I_CUSTOMCOLOR")
        if isinstance(c2, tuple): c2 = c2[0]
        c2 = int(c2)
        if c2 != 0:
            c2 = c2 & 0xFFFFFF  # drop flag bit
            return _native_to_hex(c2)
    except Exception:
        pass

    return ""

def get_track_name(track):
    try:
        ret = RPR_GetTrackName(track, "", 512)
        s = _pick_human_string(ret, "")
        if s:
            return s
        # Some wrappers return (retval, name)
        if isinstance(ret, tuple) and len(ret) >= 2:
            s2 = _pick_human_string(ret[1], "")
            if s2:
                return s2
        return "Track"
    except Exception:
        return "Track"

def get_track_mute(track):
    try:
        v = RPR_GetMediaTrackInfo_Value(track, "B_MUTE")
        if isinstance(v, tuple): v=v[0]
        return bool(int(v))
    except Exception:
        return False

def get_track_solo(track):
    try:
        v = RPR_GetMediaTrackInfo_Value(track, "I_SOLO")
        if isinstance(v, tuple): v=v[0]
        return int(v) != 0
    except Exception:
        return False

def get_track_rec(track):
    try:
        v = RPR_GetMediaTrackInfo_Value(track, "I_RECARM")
        if isinstance(v, tuple): v=v[0]
        return int(v) != 0
    except Exception:
        return False

def get_track_recinput(track):
    # I_RECINPUT: <0 = output, >=0 hardware input, 4096.. = MIDI.
    try:
        v = RPR_GetMediaTrackInfo_Value(track, "I_RECINPUT")
        if isinstance(v, tuple): v=v[0]
        return int(v)
    except Exception:
        return 0

def get_track_vol(track):
    try:
        v = RPR_GetMediaTrackInfo_Value(track, "D_VOL")
        if isinstance(v, tuple): v=v[0]
        return float(v)
    except Exception:
        return 1.0

def get_track_pan(track):
    try:
        v = RPR_GetMediaTrackInfo_Value(track, "D_PAN")
        if isinstance(v, tuple): v=v[0]
        return float(v)
    except Exception:
        return 0.0

def get_fx_count(track):
    try:
        v = RPR_TrackFX_GetCount(track)
        if isinstance(v, tuple): v=v[0]
        return int(v)
    except Exception:
        return 0

def track_folder_info(track):
    # I_FOLDERDEPTH: 1 start folder, 0 normal, -1 end folder, etc
    # I_FOLDERCOMPACT: 0 normal, 1 small, 2 children hidden
    try:
        depth = RPR_GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH")
        if isinstance(depth, tuple): depth=depth[0]
        compact = RPR_GetMediaTrackInfo_Value(track, "I_FOLDERCOMPACT")
        if isinstance(compact, tuple): compact=compact[0]
        indent = RPR_GetTrackDepth(track) if "RPR_GetTrackDepth" in globals() else 0
        if isinstance(indent, tuple): indent=indent[0]
        return int(depth), int(compact), int(indent)
    except Exception:
        return 0, 0, 0

def get_send_names(track):
    # Returns list of destination track names for sends
    names = []
    try:
        send_cnt = RPR_GetTrackNumSends(track, 0)
        if isinstance(send_cnt, tuple): send_cnt=send_cnt[0]
        send_cnt = int(send_cnt)
        for i in range(send_cnt):
            dest = RPR_GetTrackSendInfo_Value(track, 0, i, "P_DESTTRACK")
            if isinstance(dest, tuple): dest=dest[0]
            if dest:
                names.append(get_track_name(dest))
            else:
                names.append("Send %d" % (i+1))
    except Exception:
        pass
    return names

def _chan_pair_label(v):
    try:
        v = int(v)
    except Exception:
        v = 0
    if v < 0:
        v = 0
    # v is 0-based channel offset
    a = v + 1
    b = v + 2
    return "%d-%d" % (a, b)

def get_send_details(track):
    """Return detailed send objects for UI: name, vol, mute, mode, src/dst channels."""
    out = []
    try:
        send_cnt = RPR_GetTrackNumSends(track, 0)
        if isinstance(send_cnt, tuple): send_cnt = send_cnt[0]
        send_cnt = int(send_cnt)
        for i in range(send_cnt):
            dest = RPR_GetTrackSendInfo_Value(track, 0, i, "P_DESTTRACK")
            if isinstance(dest, tuple): dest = dest[0]
            dest_guid = track_guid(dest) if dest else ""
            dest_name = get_track_name(dest) if dest else ("Send %d" % (i+1))
            vol = RPR_GetTrackSendInfo_Value(track, 0, i, "D_VOL")
            vol = _pick_num(vol, 1.0)
            mute = RPR_GetTrackSendInfo_Value(track, 0, i, "B_MUTE")
            mute = bool(_pick_num(mute, 0))
            mode = RPR_GetTrackSendInfo_Value(track, 0, i, "I_SENDMODE")
            mode = int(_pick_num(mode, 0))
            src_chan = RPR_GetTrackSendInfo_Value(track, 0, i, "I_SRCCHAN")
            dst_chan = RPR_GetTrackSendInfo_Value(track, 0, i, "I_DSTCHAN")
            src_chan = int(_pick_num(src_chan, 0))
            dst_chan = int(_pick_num(dst_chan, 0))
            out.append({
                "index": i,
                "destGuid": dest_guid,
                "destName": dest_name,
                "vol": float(vol),
                "mute": mute,
                "mode": mode,
                "srcChan": src_chan,
                "dstChan": dst_chan,
                "srcCh": _chan_pair_label(src_chan),
                "dstCh": _chan_pair_label(dst_chan),
            })
    except Exception:
        pass
    return out

def get_recv_details(track):
    """Return detailed receive objects for UI."""
    out = []
    try:
        recv_cnt = RPR_GetTrackNumSends(track, -1)
        if isinstance(recv_cnt, tuple): recv_cnt = recv_cnt[0]
        recv_cnt = int(recv_cnt)
        for i in range(recv_cnt):
            src = RPR_GetTrackSendInfo_Value(track, -1, i, "P_SRCTRACK")
            if isinstance(src, tuple): src = src[0]
            src_guid = track_guid(src) if src else ""
            src_name = get_track_name(src) if src else ("Return %d" % (i+1))
            vol = RPR_GetTrackSendInfo_Value(track, -1, i, "D_VOL")
            vol = _pick_num(vol, 1.0)
            mute = RPR_GetTrackSendInfo_Value(track, -1, i, "B_MUTE")
            mute = bool(_pick_num(mute, 0))
            src_chan = RPR_GetTrackSendInfo_Value(track, -1, i, "I_SRCCHAN")
            dst_chan = RPR_GetTrackSendInfo_Value(track, -1, i, "I_DSTCHAN")
            src_chan = int(_pick_num(src_chan, 0))
            dst_chan = int(_pick_num(dst_chan, 0))
            out.append({
                "index": i,
                "srcGuid": src_guid,
                "srcName": src_name,
                "vol": float(vol),
                "mute": mute,
                "srcChan": src_chan,
                "dstChan": dst_chan,
                "srcCh": _chan_pair_label(src_chan),
                "dstCh": _chan_pair_label(dst_chan),
            })
    except Exception:
        pass
    return out

def get_recv_names(track):

    names = []
    try:
        recv_cnt = RPR_GetTrackNumSends(track, -1)
        if isinstance(recv_cnt, tuple): recv_cnt=recv_cnt[0]
        recv_cnt = int(recv_cnt)
        for i in range(recv_cnt):
            src = RPR_GetTrackSendInfo_Value(track, -1, i, "P_SRCTRACK")
            if isinstance(src, tuple): src=src[0]
            if src:
                names.append(get_track_name(src))
            else:
                names.append("Return %d" % (i+1))
    except Exception:
        pass
    return names

def build_state():
    # master + tracks
    tracks = []
    projName, projPath = get_project_info()

    try:
        n = RPR_CountTracks(0)
        if isinstance(n, tuple): n=n[0]
        n = int(n)
    except Exception:
        n = 0

    # master
    master = None
    try:
        m = RPR_GetMasterTrack(0)
        if isinstance(m, tuple): m = m[0]
        master = {
            "kind":"master",
            "guid": "MASTER",
            "id":"0",
            "idx": 0,
            "name":"MASTER",
            "vol": get_track_vol(m),
            "pan": 0.0,
            "mute": get_track_mute(m),
            "solo": False,
            "rec": False,
            "fxCount": get_fx_count(m),
            "fxAllOff": get_fx_all_off(m),
            "sendSlots": [],
            "recvSlots": [],
            "sendDetails": [],
            "recvDetails": [],
            "color": track_color_hex(m),
            "folderDepth": 0,
            "indent": 0
        }
    except Exception:
        master = None

    # normal tracks
    for i in range(n):
        tr = RPR_GetTrack(0, i)
        if isinstance(tr, tuple): tr=tr[0]
        if not tr:
            continue
        depth, compact, indent = track_folder_info(tr)
        t = {
            "kind":"track",
            "guid": track_guid(tr),
            "id": str(i+1),
            "idx": i+1,
            "name": get_track_name(tr),
            "vol": get_track_vol(tr),
            "pan": get_track_pan(tr),
            "mute": get_track_mute(tr),
            "solo": get_track_solo(tr),
            "rec": get_track_rec(tr),
            "recInput": get_track_recinput(tr),
            "fxCount": get_fx_count(tr),
            "fxAllOff": get_fx_all_off(tr),
            "sendSlots": get_send_names(tr),
            "recvSlots": get_recv_names(tr),
            "sendDetails": get_send_details(tr),
            "recvDetails": get_recv_details(tr),
            "folderDepth": int(depth),
            "folderCompact": int(compact),
            "indent": int(indent),
            "color": track_color_hex(tr),
        }
        tracks.append(t)

    return {"type":"state",
            "master": master,
            "tracks": tracks,
            "projectName": projName,
            "projectPath": projPath,
            "transport": get_transport_state(),
            "ts": _now(),
            "version": VERSION}


# --- meters ---
def _db_to_lin(db):
    return math.pow(10.0, db/20.0)

def get_track_peaks(track):
    """Return (L,R) peaks as linear 0..1 floats.

    - Prefer Track_GetPeakInfo when available (fast, linear).
    - When track is record-armed, prefer UI peaks (they follow input/monitoring).
    - Also use UI peaks as a fallback if Track_GetPeakInfo stays near-zero.
    """
    def _ui_peaks_lin(tr):
        # Don't clear UI peak-hold every 33ms; REAPER updates meters slower.
        now = _now()
        last = getattr(_ui_peaks_lin, "_last_clear", 0.0)
        clear = (now - last) >= 0.12
        if clear:
            _ui_peaks_lin._last_clear = now
        try:
            vL = RPR_GetTrackUIPeakHoldDB(tr, 0, clear)
            vR = RPR_GetTrackUIPeakHoldDB(tr, 1, clear)
            if isinstance(vL, tuple): vL = vL[0]
            if isinstance(vR, tuple): vR = vR[0]
            vL = float(vL)
            vR = float(vR)

            # Heuristic: if values look like dB (<=0 and >=-150), convert to linear.
            def norm(v):
                if v < -150:
                    return 0.0
                if v <= 0.0 and v >= -150.0:
                    return float(_db_to_lin(v))
                # already linear
                return max(0.0, min(1.0, v))
            return norm(vL), norm(vR)
        except Exception:
            return 0.0, 0.0

    # If track is record-armed, UI peaks are usually the most useful (input/monitor).
    try:
        recarm = RPR_GetMediaTrackInfo_Value(track, "I_RECARM")
        if isinstance(recarm, tuple): recarm = recarm[0]
        if float(recarm) >= 0.5:
            return _ui_peaks_lin(track)
    except Exception:
        pass

    # Prefer Track_GetPeakInfo if available
    try:
        if "RPR_Track_GetPeakInfo" in globals():
            pkL = RPR_Track_GetPeakInfo(track, 0)
            pkR = RPR_Track_GetPeakInfo(track, 1)
            if isinstance(pkL, tuple): pkL = pkL[0]
            if isinstance(pkR, tuple): pkR = pkR[0]
            pkL = float(pkL)
            pkR = float(pkR)
            # If it looks dead, fall back to UI peaks (useful for monitoring cases).
            if pkL < 1e-6 and pkR < 1e-6:
                uL, uR = _ui_peaks_lin(track)
                if uL > pkL or uR > pkR:
                    return uL, uR
            return max(0.0, min(1.0, pkL)), max(0.0, min(1.0, pkR))
    except Exception:
        pass

    # fallback: UI peaks (hold)
    return _ui_peaks_lin(track)


def get_project_bpm():
    try:
        if "RPR_Master_GetTempo" in globals():
            bpm = RPR_Master_GetTempo()
            if isinstance(bpm, tuple): bpm = bpm[0]
            return float(bpm)
    except Exception:
        pass
    try:
        if "RPR_TimeMap_GetDividedBpm" in globals():
            bpm = RPR_TimeMap_GetDividedBpm(0)
            if isinstance(bpm, tuple): bpm = bpm[0]
            return float(bpm)
    except Exception:
        pass
    return None


def get_regions():
    regions = []
    try:
        res = RPR_CountProjectMarkers(0, 0, 0)
        if isinstance(res, tuple):
            total = int(res[0])
        else:
            total = int(res)
    except Exception:
        total = 0

    for i in range(total):
        try:
            r = RPR_EnumProjectMarkers2(0, i, 0, 0, 0, 0, 0)
            if isinstance(r, tuple):
                isrgn = int(r[1]) if len(r) > 1 else 0
                start = float(r[2]) if len(r) > 2 else 0.0
                end = float(r[3]) if len(r) > 3 else 0.0
                name = str(r[4]) if len(r) > 4 else ""
                idx = int(r[5]) if len(r) > 5 else i
            else:
                continue
            if isrgn:
                regions.append({"index": idx, "name": name, "start": start, "end": end})
        except Exception:
            continue
    return regions


def get_transport_state():
    try:
        ps = RPR_GetPlayState()
        if isinstance(ps, tuple): ps = ps[0]
        play_state = int(ps)
    except Exception:
        play_state = 0
    playing = (play_state & 1) != 0
    paused = (play_state & 2) != 0
    recording = (play_state & 4) != 0

    pos = None
    try:
        if playing or paused or recording:
            if "RPR_GetPlayPosition2" in globals():
                pos = RPR_GetPlayPosition2()
            else:
                pos = RPR_GetPlayPosition()
        else:
            pos = RPR_GetCursorPosition()
        if isinstance(pos, tuple): pos = pos[0]
        pos = float(pos)
    except Exception:
        pos = 0.0

    bpm = get_project_bpm()

    bar = beat = 0
    beat_frac = 0.0
    try:
        res = RPR_TimeMap2_timeToBeats(0, pos, 0, 0, 0, 0)
        if isinstance(res, tuple):
            beats = float(res[0]) if len(res) > 0 else 0.0
            measures = int(res[1]) if len(res) > 1 else 0
            beat = int(beats) + 1
            beat_frac = beats - int(beats)
            bar = int(measures) + 1
    except Exception:
        bar = 0
        beat = 0
        beat_frac = 0.0

    regions = get_regions()
    region_name = ""
    region_index = None
    try:
        rinfo = RPR_GetLastMarkerAndCurRegion(0, pos, 0, 0)
        if isinstance(rinfo, tuple) and len(rinfo) > 2:
            region_index = int(rinfo[2])
    except Exception:
        region_index = None
    if region_index is not None:
        for r in regions:
            if int(r.get("index", -1)) == region_index:
                region_name = r.get("name", "")
                break

    return {
        "playState": play_state,
        "playing": playing,
        "paused": paused,
        "recording": recording,
        "position": pos,
        "bpm": bpm,
        "bar": bar,
        "beat": beat,
        "beatFrac": beat_frac,
        "regionName": region_name,
        "regionIndex": region_index,
        "regions": regions
    }


def build_meter():
    frames = []
    try:
        # master
        m = RPR_GetMasterTrack(0)
        pkL, pkR = get_track_peaks(m)
        frames.append({"guid": track_guid(m), "pkL": pkL, "pkR": pkR})
    except Exception:
        pass
    try:
        n = RPR_CountTracks(0)
        if isinstance(n, tuple): n=n[0]
        n=int(n)
    except Exception:
        n=0
    for i in range(n):
        tr = RPR_GetTrack(0,i)
        if isinstance(tr, tuple): tr=tr[0]
        if not tr: continue
        pkL, pkR = get_track_peaks(tr)
        frames.append({"guid": track_guid(tr), "pkL": pkL, "pkR": pkR})
    return {"type":"meter", "frames": frames, "ts": _now(), "version": VERSION}

# --- FX helpers ---
def get_fx_all_off(track):
    try:
        cnt = get_fx_count(track)
        if cnt <= 0:
            return False
        enabled_any = False
        if "RPR_TrackFX_GetEnabled" in globals():
            for i in range(cnt):
                try:
                    en = RPR_TrackFX_GetEnabled(track, i)
                    if isinstance(en, tuple): en = en[0]
                    if bool(en):
                        enabled_any = True
                        break
                except Exception:
                    pass
        else:
            # If API not available, assume not all-off
            return False
        return (not enabled_any)
    except Exception:
        return False


def _fx_name(track, idx):
    try:
        ret = RPR_TrackFX_GetFXName(track, idx, "", 512)
        return _pick_human_string(ret, default="FX %d" % (idx+1))
    except Exception:
        return "FX %d" % (idx+1)


def _fx_enabled(track, idx):
    try:
        v = RPR_TrackFX_GetEnabled(track, idx)
        if isinstance(v, tuple): v=v[0]
        return bool(v)
    except Exception:
        return True

def fx_list(track):
    out=[]
    cnt = get_fx_count(track)
    for i in range(cnt):
        out.append({"index": i, "name": _fx_name(track,i), "enabled": _fx_enabled(track,i)})
    return out

def fx_params(track, fxIndex):
    params=[]
    try:
        pc = RPR_TrackFX_GetNumParams(track, fxIndex)
        if isinstance(pc, tuple): pc=pc[0]
        pc = int(pc)
    except Exception:
        pc = 0
    for p in range(pc):
        try:
            nm = _pick_human_string(RPR_TrackFX_GetParamName(track, fxIndex, p, "", 256), default="Param %d" % (p+1))
            val = RPR_TrackFX_GetParamNormalized(track, fxIndex, p)
            if isinstance(val, tuple): val=val[0]
            # formatted value (human-friendly text like "-12.0 dB" / "3.5 ms")
            fmt = ""
            try:
                fmt = _pick_human_string(RPR_TrackFX_GetFormattedParamValue(track, fxIndex, p, "", 256), default="")
            except Exception:
                fmt = ""

            raw = None
            mn = None
            mx = None
            try:
                rv = RPR_TrackFX_GetParam(track, fxIndex, p)
                if isinstance(rv, tuple): rv = rv[0]
                raw = float(rv)
            except Exception:
                raw = None
            try:
                ex = RPR_TrackFX_GetParamEx(track, fxIndex, p, 0.0, 0.0)
                # typically returns (retval, minval, maxval)
                if isinstance(ex, tuple) and len(ex) >= 3:
                    mn = float(ex[1])
                    mx = float(ex[2])
            except Exception:
                mn = None
                mx = None

            params.append({"index": p, "name": nm, "value": float(val), "fmt": fmt, "raw": raw, "min": mn, "max": mx})
        except Exception:
            pass
    return params

def add_fx_by_name(track, name):
    # add by name; returns index or -1
    try:
        # instantiate flag:
        #  -1 = query only (do not add)
        #   1 = add/instantiate
        idx = RPR_TrackFX_AddByName(track, name, False, 1)
        if isinstance(idx, tuple): idx=idx[0]
        return int(idx)
    except Exception:
        return -1

def _norm_fx_name(s):
    """Normalize FX names so we can match catalog names to existing FX for duplication."""
    try:
        s = str(s or "").strip()
    except Exception:
        return ""
    sl = s.lower()
    for pref in ("jsfx:","js:","vst3:","vst:","au:","clap:","dx:"):
        if sl.startswith(pref):
            s = s[len(pref):].strip()
            break
    return s.lower()


def copy_fx(track, frm, to):
    """Copy FX from index frm to index to (append when to == current count)."""
    try:
        RPR_TrackFX_CopyToTrack(track, frm, track, to, False)
        return True
    except Exception:
        return False


def move_fx(track, frm, to):
    try:
        RPR_TrackFX_CopyToTrack(track, frm, track, to, True)
        return True
    except Exception:
        return False

# --- command handling ---
def handle_cmd(cmd, sock):
    typ = cmd.get("type","")
    try:
        if typ == "reqState":
            _send(sock, build_state()); return
        if typ == "transport":
            action = cmd.get("action", "")
            if action == "play":
                try: RPR_OnPlayButton()
                except Exception: pass
            elif action == "stop":
                try: RPR_OnStopButton()
                except Exception: pass
            elif action == "pause":
                try: RPR_OnPauseButton()
                except Exception: pass
            elif action == "record":
                try: RPR_OnRecordButton()
                except Exception: pass
            return
        if typ == "setBpm":
            bpm = float(cmd.get("bpm", 120.0))
            bpm = max(20.0, min(300.0, bpm))
            try:
                if "RPR_SetCurrentBPM" in globals():
                    RPR_SetCurrentBPM(0, bpm, True)
                elif "RPR_SetTempoTimeSigMarker" in globals():
                    RPR_SetTempoTimeSigMarker(0, -1, 0, -1, -1, bpm, 0, 0, False)
            except Exception:
                pass
            return
        if typ == "gotoRegion":
            idx = int(cmd.get("index", -1))
            if idx >= 0:
                try:
                    res = RPR_CountProjectMarkers(0, 0, 0)
                    total = int(res[0]) if isinstance(res, tuple) else int(res)
                except Exception:
                    total = 0
                for i in range(total):
                    try:
                        r = RPR_EnumProjectMarkers2(0, i, 0, 0, 0, 0, 0)
                        if not isinstance(r, tuple) or len(r) < 6:
                            continue
                        isrgn = int(r[1])
                        start = float(r[2])
                        mark_idx = int(r[5])
                        if isrgn and mark_idx == idx:
                            try:
                                RPR_SetEditCurPos2(0, start, True, True)
                            except Exception:
                                RPR_SetEditCurPos(start, True, True)
                            break
                    except Exception:
                        continue
            return
        if typ == "setVol":
            guid = cmd.get("guid","")
            vol = float(cmd.get("vol", 1.0))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_SetMediaTrackInfo_Value(tr, "D_VOL", vol)
            return
        if typ == "setPan":
            guid = cmd.get("guid","")
            pan = float(cmd.get("pan", 0.0))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_SetMediaTrackInfo_Value(tr, "D_PAN", pan)
            return
        if typ == "setMute":
            guid = cmd.get("guid","")
            mute = bool(cmd.get("mute", False))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_SetMediaTrackInfo_Value(tr, "B_MUTE", 1.0 if mute else 0.0)
            return
        if typ == "setSolo":
            guid = cmd.get("guid","")
            solo = bool(cmd.get("solo", False))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_SetMediaTrackInfo_Value(tr, "I_SOLO", 2.0 if solo else 0.0)
            return
        if typ == "setRec":
            guid = cmd.get("guid","")
            rec = bool(cmd.get("rec", False))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_SetMediaTrackInfo_Value(tr, "I_RECARM", 1.0 if rec else 0.0)
            return
        if typ == "setSendVol":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            vol = float(cmd.get("vol", 1.0))
            tr = find_track_by_guid(guid)
            if tr:
                try: RPR_SetTrackSendInfo_Value(tr, 0, idx, "D_VOL", vol)
                except Exception: pass
            return
        if typ == "setSendMute":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            mute = bool(cmd.get("mute", False))
            tr = find_track_by_guid(guid)
            if tr:
                try: RPR_SetTrackSendInfo_Value(tr, 0, idx, "B_MUTE", 1.0 if mute else 0.0)
                except Exception: pass
            return
        if typ == "setSendMode":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            # 2-state UI: 0=post, 1=pre (use REAPER pre-fx mode = 1)
            mode = int(cmd.get("mode", 0))
            mode = 1 if mode else 0
            tr = find_track_by_guid(guid)
            if tr:
                try: RPR_SetTrackSendInfo_Value(tr, 0, idx, "I_SENDMODE", mode)
                except Exception: pass
            return
        if typ == "setRecvVol":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            vol = float(cmd.get("vol", 1.0))
            tr = find_track_by_guid(guid)
            if tr:
                try: RPR_SetTrackSendInfo_Value(tr, -1, idx, "D_VOL", vol)
                except Exception: pass
            return
        if typ == "setRecvMute":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            mute = bool(cmd.get("mute", False))
            tr = find_track_by_guid(guid)
            if tr:
                try: RPR_SetTrackSendInfo_Value(tr, -1, idx, "B_MUTE", 1.0 if mute else 0.0)
                except Exception: pass
            return
        if typ == "setRecInput":

            guid = cmd.get("guid","")
            # UI sends 1..16 for mono hw input
            inp = int(cmd.get("input", 1))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_SetMediaTrackInfo_Value(tr, "I_RECINPUT", max(0, inp-1))
            return
        if typ == "showFxChain":
            guid = cmd.get("guid","")
            tr = find_track_by_guid(guid)
            if tr:
                # Show track FX chain (command 40291)
                RPR_TrackFX_Show(tr, 0, 1)  # show chain if possible
            return
        if typ == "reqFxList":
            guid = cmd.get("guid","")
            tr = find_track_by_guid(guid)
            if tr:
                _send(sock, {"type":"fxList","guid":guid,"fx":fx_list(tr)})
            else:
                _send(sock, {"type":"fxList","guid":guid,"fx":[]})
            return
        if typ == "setFxEnabled":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            enabled = bool(cmd.get("enabled", True))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_TrackFX_SetEnabled(tr, idx, enabled)
            return
        if typ == "setFxAllEnabled":
            guid = cmd.get("guid","")
            enabled = bool(cmd.get("enabled", True))
            tr = find_track_by_guid(guid)
            if tr:
                cnt = get_fx_count(tr)
                for i in range(cnt):
                    try: RPR_TrackFX_SetEnabled(tr, i, enabled)
                    except Exception: pass
            return

        if typ == "deleteFx":
            guid = cmd.get("guid","")
            idx = int(cmd.get("index", 0))
            tr = find_track_by_guid(guid)
            if tr:
                try:
                    RPR_TrackFX_Delete(tr, idx)
                except Exception:
                    pass
            return

        if typ == "moveFx":
            guid = cmd.get("guid","")
            frm = int(cmd.get("from", 0))
            to = int(cmd.get("to", 0))
            tr = find_track_by_guid(guid)
            if tr:
                move_fx(tr, frm, to)
            return
        if typ == "reqFxParams":
            guid = cmd.get("guid","")
            fxIndex = int(cmd.get("fxIndex", 0))
            tr = find_track_by_guid(guid)
            if tr:
                _send(sock, {"type":"fxParams","guid":guid,"fxIndex":fxIndex,"params":fx_params(tr, fxIndex)})
            else:
                _send(sock, {"type":"fxParams","guid":guid,"fxIndex":fxIndex,"params":[]})
            return
        if typ == "setFxParam":
            guid = cmd.get("guid","")
            fxIndex = int(cmd.get("fxIndex", 0))
            param = int(cmd.get("param", 0))
            value = float(cmd.get("value", 0.0))
            tr = find_track_by_guid(guid)
            if tr:
                RPR_TrackFX_SetParamNormalized(tr, fxIndex, param, value)
            return
        if typ == "addFx":
            guid = cmd.get("guid","")
            name = _as_str(cmd.get("name",""))
            tr = find_track_by_guid(guid)
            if tr and name:
                # Support fallback names separated by "||"
                candidates = [s.strip() for s in name.split("||") if str(s).strip()]
                try:
                    before = RPR_TrackFX_GetCount(tr)
                    if isinstance(before, tuple): before = before[0]
                    before = int(before)
                except Exception:
                    before = -1

                def _try_duplicate_by_name(candName, beforeCount):
                    """Fallback: if AddByName doesn't increase FX count (some ReaScript setups),
                    duplicate the first matching existing FX by copying it to the end."""
                    try:
                        cn = _norm_fx_name(candName)
                        if not cn:
                            return False
                        existing = fx_list(tr)
                        for fx in existing:
                            try:
                                nm = _norm_fx_name(fx.get("name",""))
                                if cn in nm or nm in cn:
                                    return copy_fx(tr, int(fx.get("index",0)), int(beforeCount))
                            except Exception:
                                continue
                    except Exception:
                        pass
                    return False

                for cand in candidates:
                    try:
                        add_fx_by_name(tr, cand)
                    except Exception:
                        pass
                    try:
                        after = RPR_TrackFX_GetCount(tr)
                        if isinstance(after, tuple): after = after[0]
                        after = int(after)
                    except Exception:
                        after = before

                    # Success if FX count increased
                    if before >= 0 and after > before:
                        break

                    # If it didn't increase, try duplicating an existing matching FX
                    if before >= 0:
                        if _try_duplicate_by_name(cand, before):
                            try:
                                after2 = RPR_TrackFX_GetCount(tr)
                                if isinstance(after2, tuple): after2 = after2[0]
                                after2 = int(after2)
                            except Exception:
                                after2 = before
                            if after2 > before:
                                break

                _send(sock, {"type":"fxList","guid":guid,"fx":fx_list(tr)})
            return
    except Exception:
        log("handle_cmd error", typ, traceback.format_exc())

# --- track lookup ---
def find_track_by_guid(guid):
    # Special stable master guid
    try:
        if guid == "MASTER" or guid == "{MASTER}":
            m = RPR_GetMasterTrack(0)
            if isinstance(m, tuple): m = m[0]
            return m
    except Exception:
        pass

    # Compare using stable GUID strings
    try:
        m = RPR_GetMasterTrack(0)
        if track_guid(m) == guid:
            return m
    except Exception:
        pass
    try:
        n = RPR_CountTracks(0)
        if isinstance(n, tuple): n=n[0]
        n=int(n)
    except Exception:
        n=0
    for i in range(n):
        tr = RPR_GetTrack(0,i)
        if isinstance(tr, tuple): tr=tr[0]
        if not tr: continue
        if track_guid(tr) == guid:
            return tr
    return None

# --- TCP loop ---
sock = None
rx_buf = []
last_state_sent = 0.0
last_meter_sent = 0.0
next_connect = 0.0

def connect():
    global sock, next_connect
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect((HOST, PORT))
        s.settimeout(0.0)
        sock = s
        log("[RemoteMixer v%s] TCP connected -> %s:%d" % (VERSION, HOST, PORT))
        # hello (server may log)
        _send(sock, {"type":"hello","version":VERSION,"ts":_now()})
        return True
    except Exception:
        sock = None
        next_connect = _now() + 1.0
        return False

def loop():
    global sock, rx_buf, last_state_sent, last_meter_sent, next_connect
    now = _now()

    # connect
    if sock is None:
        if now >= next_connect:
            connect()
        # defer
        try:
            RPR_defer("loop()")
        except Exception:
            try: RPR_defer("loop")
            except Exception: pass
        return

    # read commands
    try:
        lines = _recv_lines(sock)
        if "__EOF__" in lines:
            try: sock.close()
            except Exception: pass
            sock = None
        else:
            for ln in lines:
                if not ln.strip(): continue
                try:
                    cmd = json.loads(ln)
                    handle_cmd(cmd, sock)
                except Exception:
                    # ignore malformed lines
                    pass
    except Exception:
        try: sock.close()
        except Exception: pass
        sock = None

    # periodic state
    try:
        if now - last_state_sent >= STATE_INTERVAL:
            st = build_state()
            _send(sock, st)
            last_state_sent = now
    except Exception:
        pass

    # periodic meter
    try:
        if now - last_meter_sent >= METER_INTERVAL:
            mt = build_meter()
            _send(sock, mt)
            last_meter_sent = now
    except Exception:
        pass

    # defer again
    try:
        RPR_defer("loop()")
    except Exception:
        try: RPR_defer("loop")
        except Exception: pass

def main():
    try:
        log("RemoteMixer.py started. v=%s TCP target=%s:%d" % (VERSION, HOST, PORT))
        log("[RemoteMixer v%s] START resource=%s" % (VERSION, _as_str(RPR_GetResourcePath() if "RPR_GetResourcePath" in globals() else "")))
        loop()
    except Exception:
        log("fatal", traceback.format_exc())

main()
