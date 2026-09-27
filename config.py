import os
import platform
import sys
import shutil
import subprocess
import logging
from dotenv import load_dotenv

# Load secrets from the .env file
load_dotenv()

# --- PATHS ---
# Using dynamic relative paths to prevent sandbox crashes
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STAGING_DIR = os.path.join(BASE_DIR, "staging")
CSV_BLUEPRINT = os.path.join(BASE_DIR, "configs", "fmp_data_7718.csv")

# Centralized cross-platform directories
if platform.system() == "Windows":
    if os.path.exists("Z:\\"):
        MUSIC_DIR = "Z:\\"
    else:
        MUSIC_DIR = r"G:\My Drive\FMP MUSIC\BASE\MUSIC"
    BROADCASTER_DB = r"C:\FMP_Broadcaster\fmp_radio.db"
else:
    MUSIC_DIR = "/home/ubuntu/music"
    BROADCASTER_DB = "/home/ubuntu/FMP-Broadcaster/fmp_radio.db"


def resolve_physical_path(db_path: str) -> str:
    """
    Resolves a database relative path to the physical path of the current environment.
    If the path already exists directly as-is on the disk, it is returned directly.
    """
    if not db_path:
        return ""
    
    # Normalize separators to forward slashes first
    clean_path = db_path.replace('\\', '/')
    
    # If the file exists directly as-is on the disk, return it
    if os.path.exists(clean_path):
        return clean_path
    
    # Strip legacy prefixes if any
    if clean_path.upper().startswith('Z:/'):
        clean_path = clean_path[3:]
    elif clean_path.lower().startswith('/home/ubuntu/music/'):
        clean_path = clean_path[len('/home/ubuntu/music/'):]
    elif clean_path.lower().startswith('g:/my drive/fmp music/base/music/'):
        clean_path = clean_path[len('g:/my drive/fmp music/base/music/'):]
        
    if platform.system() == "Windows":
        # Check G: drive first, fall back to Z: drive if G: is unavailable
        g_drive_path = os.path.join(MUSIC_DIR, clean_path)
        if os.path.exists(g_drive_path):
            return g_drive_path.replace('/', '\\')
        return os.path.join("Z:", clean_path).replace('/', '\\')
    else:
        # Linux playout server path
        return os.path.join(MUSIC_DIR, clean_path).replace('\\', '/')



# --- FTP SERVER SETTINGS ---
# Loaded securely from .env
FTP_HOST = os.getenv("FTP_HOST", "hello.citrus3.com")
FTP_PORT = int(os.getenv("FTP_PORT", 2121))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
# Citrus3 drops the user directly into the root media folder
FTP_BASE_DIR = "/" 

# --- EXECUTABLES ---
# Using Firefox to bypass Chromium App-Bound Encryption DPAPI locks.
# If a manual cookies.txt file is placed in configs/, prioritize it (essential for headless servers/OCI).
if platform.system() == "Windows":
    somedl_executable = "somedl"
    if shutil.which("yt-dlp"):
        ytdlp_base = ["yt-dlp"]
    else:
        ytdlp_base = [sys.executable, "-m", "yt_dlp"]
else:
    somedl_executable = "/home/ubuntu/venv-fmp/bin/somedl"
    ytdlp_base = ["/home/ubuntu/venv-fmp/bin/yt-dlp"]

COOKIES_FILE = os.path.join(BASE_DIR, "configs", "cookies.txt")

# Where YouTube sign-in cookies come from. On Windows, read them live from Firefox (signed into the station's
# YouTube Music Premium account) so they never go stale; an exported cookies.txt goes stale silently - until
# 2026-09-27 every download was free-tier 128k because the exported file had lost its youtube.com sign-in.
# YT_COOKIES_SOURCE=file forces the exported file; the VM (no browser) always uses the file.
# tools/check_youtube_quality.py checks daily that the Premium formats are still offered.
_cookie_source = os.getenv("YT_COOKIES_SOURCE", "firefox" if platform.system() == "Windows" else "file").lower()
if _cookie_source == "firefox":
    COOKIE_ARGS = ["--cookies-from-browser", "firefox"]
elif os.path.exists(COOKIES_FILE):
    COOKIE_ARGS = ["--cookies", COOKIES_FILE]
else:
    COOKIE_ARGS = ["--cookies-from-browser", "firefox"]
SOMEDL_CMD = [somedl_executable] + COOKIE_ARGS
YT_DLP_CMD = ytdlp_base + COOKIE_ARGS

# --- API KEYS ---
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
MUSICBRAINZ_USERAGENT = ("FMP_Ultimate_AutoTagger", "1.0", "formypeopleinfo@gmail.com")

# --- SYNC CONFIGS ---
# Toggle Auto-Git Synchronization on successful vaults. The whole
# multi-machine sync architecture (Windows <-> VM, all the tools/ scripts)
# depends on this being True - the "Keep False by default" comment here was
# stale/aspirational and never matched the actual value.
AUTO_GIT_PUSH = True

# Cross-process lock serializing every git pull/push against this repo.
# Multiple independent processes touch it concurrently - the long-running
# app.py service (its own periodic git_sync_worker, plus several per-action
# git-sync call sites), the cron-invoked tools/sync_library_db.py, and
# manual deploys. A threading.Lock only protects against races within one
# process, so two of these could still run `git pull --rebase` at the same
# moment and produce a real, stuck merge conflict - confirmed happening
# 2026-09-03: a stuck rebase left literal <<<<<<< conflict markers baked
# into the live CSV for ~50 minutes before anything noticed. Callers should
# hold this for the full pull+push sequence of a sync, not just one half.
GIT_LOCK_PATH = os.path.join(BASE_DIR, "configs", "git_ops.lock")

def git_operation_lock(timeout=120):
    from filelock import FileLock
    return FileLock(GIT_LOCK_PATH, timeout=timeout)


def git_safe_pull(branch_name, cwd=None):
    """Reconciles the local branch with origin, replacing every hand-rolled
    `git pull --rebase --autostash` call in this codebase.

    Two real incidents on 2026-09-03 (both discovered hours after the fact,
    with literal <<<<<<< markers sitting in the live production CSV in the
    meantime) drove this:

    1. --autostash's own pop step conflicting with the freshly-rebased
       branch, even with git_operation_lock already serializing same-machine
       callers - the lock prevents concurrent git commands, it does nothing
       for a single caller's rebase+autostash-pop sequence going wrong on
       its own. This class of failure is eliminated by not rebasing at all:
       a plain merge never stashes/pops, so it never has this failure mode.
    2. A rebase left stuck (interactive, "editing a commit", no
       rebase-merge/todo commands remaining) from an earlier failed run,
       still sitting there when the NEXT scheduled sync fired - which then
       tried to start its own pull on top of an already-broken repo state,
       compounding the mess. Any caller of this function gets a stuck
       rebase/merge from a previous run cleaned up FIRST, loudly logged,
       before attempting anything new - never silently layered on top of.

    Raises on a genuine content conflict (callers already wrap their git
    sequence in try/except and log) rather than leaving the repo mid-merge -
    a merge conflict is always aborted back to a clean state before this
    raises, so the working tree is never left broken for the next caller.

    cwd: repo root to run in. Defaults to the process's own working
    directory (matches every caller except tools/sync_library_db.py, which
    explicitly manages its own cwd since it can be invoked from elsewhere).
    """
    git_dir = os.path.join(cwd, ".git") if cwd else ".git"
    rebase_dir_i = os.path.join(git_dir, "rebase-merge")
    rebase_dir_a = os.path.join(git_dir, "rebase-apply")
    merge_head = os.path.join(git_dir, "MERGE_HEAD")

    if os.path.exists(rebase_dir_i) or os.path.exists(rebase_dir_a):
        logging.warning("[git_safe_pull] Found a stuck rebase from a previous run - aborting it before proceeding.")
        subprocess.run(["git", "rebase", "--abort"], capture_output=True, text=True, cwd=cwd)
    if os.path.exists(merge_head):
        logging.warning("[git_safe_pull] Found a stuck merge from a previous run - aborting it before proceeding.")
        subprocess.run(["git", "merge", "--abort"], capture_output=True, text=True, cwd=cwd)

    pull_res = subprocess.run(
        ["git", "pull", "--no-rebase", "origin", branch_name],
        capture_output=True, text=True, cwd=cwd
    )
    if pull_res.returncode != 0:
        # Conflict (or any other pull failure) - abort back to a clean
        # state rather than leaving a half-merged working tree for
        # whatever runs next. Real content conflicts on the shared CSV
        # still need a human, same as before - this just guarantees the
        # repo is never left silently broken while waiting for one.
        subprocess.run(["git", "merge", "--abort"], capture_output=True, text=True, cwd=cwd)
        raise Exception(f"git pull failed and was aborted: {pull_res.stderr or pull_res.stdout}")

# --- iHEART SYNC SETTINGS ---
IHEART_SYNC_ENABLED = True
IHEART_STATION_ID = "865"  # WVAZ V103 Chicago
IHEART_POLL_INTERVAL = 30  # seconds
IHEART_CHURCH_FOLDER = "Shows/InspirationalChurch"
IHEART_CHURCH_DAYS = [6]  # Sunday (6 in datetime.date.weekday())
IHEART_CHURCH_START_HOUR = 6  # 6:00 AM
IHEART_CHURCH_END_HOUR = 12  # 12:00 PM
IHEART_CHURCH_KEYWORDS = ["gospel", "choir", "worship", "praise", "pastor", "bishop", "jesus", "god", "lord", "christ", "hymn", "spiritual", "church", "amen"]

# --- NON-SONG EXCLUSION LOGIC ---
def is_non_song(track_name, file_path):
    path_lower = file_path.lower() if file_path else ""
    name_lower = track_name.lower() if track_name else ""

    # Directory/Path keywords -- matched against folder path SEGMENTS only,
    # never the filename. Two real bugs found and fixed 2026-09-15 from the
    # old "substring anywhere in the whole path" approach:
    # 1. Bare 'shows' matched the entire path, wrongly catching the real
    #    Shows/InspirationalChurch/ (114 tracks) and Shows/Danny Boy/ (11
    #    tracks) music folders. Replaced with 'shows_to_delete', the only
    #    actual non-song folder with "shows" in the name (a known
    #    junk-staging folder seen on disk, not currently in the CSV).
    # 2. 'drop' and 'intro' matched inside a SONG'S OWN TITLE, not just a
    #    folder name -- e.g. "Jackie Wilson - Lonely Teardrops" (contains
    #    "drop"), both Snoop Dogg "Drop It Like It's Hot" entries, and
    #    "Danny Boy - Black Heart Intro" were all wrongly flagged as
    #    non-songs by a keyword that was only ever meant to describe a
    #    folder. Excluding the filename (the last path segment) from this
    #    check fixes it without weakening the real folder-based matches
    #    (ondemand/celebrity_ids/RL/RLFMPBUTTADROP.mp3 still correctly
    #    matches on 'ondemand', intro/DBLiveMixOpening.mp3 still correctly
    #    matches on 'intro' as the actual folder).
    path_segments = [s for s in path_lower.replace('\\', '/').split('/') if s]
    folder_path = '/'.join(path_segments[:-1]) if len(path_segments) > 1 else ''
    non_song_dirs = ['ondemand', 'sweeper', 'promo', 'drop', 'commercial', 'sfx', 'effect', 'liner', 'branding', 'shows_to_delete', 'adbreak', 'ad break', 'quarantine', 'intro']
    if any(x in folder_path for x in non_song_dirs):
        return True

    # Track Name keywords
    non_song_names = ['sweeper', 'chicago l announcement', 'liner', 'celebrity drop', 'fmp radio', 'ad break', 'commercial', 'sfx']
    if any(x in name_lower for x in non_song_names):
        return True

    # Structural signal: every real song in this catalog is named
    # "Artist - Title". A file with no " - " separator at all is a
    # production asset (sweeper, station ID, show opener) mislabeled as
    # item_type=Music, not a song that slipped past the keyword lists
    # above. Deliberately not folder-gated -- real songs live in more than
    # just Music/ (e.g. Shows/InspirationalChurch/, confirmed above), so
    # folder alone isn't a safe signal; the missing separator is what
    # actually distinguishes a real song from a production asset.
    # Confirmed live 2026-09-15: "DBLiveMixOpening" (intro/) and
    # "DB Live Show Open Opening" / "Gators Live Show Opening"
    # (ondemand/sweepers/) all matched this pattern and were showing up in
    # fingerprint-dedup output looking like ambiguous "is this a duplicate
    # song" cases when it was actually obvious from the data alone that
    # they weren't songs at all.
    if track_name and ' - ' not in track_name:
        return True

    return False

# --- WEB SERVER SETTINGS ---
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", 58000))
DOWNLOAD_CONCURRENCY = int(os.getenv("DOWNLOAD_CONCURRENCY", 3))