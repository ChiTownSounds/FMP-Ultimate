import sys
import os
import shutil
import subprocess
import logging
import threading
import queue
import time
import itertools
import collections
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import urllib.request
import urllib.parse
import json
import re
import uuid
import hmac
from werkzeug.utils import secure_filename

from config import STAGING_DIR, SOMEDL_CMD, YT_DLP_CMD, FTP_HOST, FTP_USER, FTP_PASS, FTP_BASE_DIR, FTP_PORT, CSV_BLUEPRINT, APP_HOST, APP_PORT, MUSIC_DIR, INTERNAL_API_KEY, git_operation_lock, git_safe_pull

# --- CORE MODULE IMPORTS ---
from modules.ingest import Gatekeeper
from modules.download import Transporter
from modules.tagger import AutoMaster
from modules.storage import VaultManager

# --- LOGGING INFRASTRUCTURE ---
# Using dynamic relative paths to match config.py setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# Silence werkzeug noise
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# [PHASE G] Persistent Error Logging
error_log_path = os.path.join(LOG_DIR, "error.log")
file_handler = logging.FileHandler(error_log_path)
file_handler.setLevel(logging.ERROR)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)

app = Flask(__name__)

def _require_internal_key():
    """Returns None if the request carries a valid internal key, else a Flask error response."""
    if not INTERNAL_API_KEY:
        return jsonify({"error": "Server misconfigured: INTERNAL_API_KEY not set"}), 500
    supplied = request.headers.get("X-Internal-Key", "") or request.args.get("key", "")
    if not hmac.compare_digest(supplied, INTERNAL_API_KEY):
        return jsonify({"error": "Unauthorized"}), 401
    return None

def is_smart_duplicate(existing_name, check_artist, check_title, vm=None):
    if vm is None:
        from modules.storage import VaultManager
        vm = VaultManager()
    
    # First try direct normalized key match (fast path)
    norm_key = vm._normalize_track_key(f"{check_artist} - {check_title}")
    if vm._normalize_track_key(existing_name) == norm_key:
        return True, "Already in library (normalized match)"
        
    # Fallback to smart parsing
    parts = existing_name.split(' - ', 1)
    if len(parts) == 2:
        ex_artist, ex_title = parts
    else:
        ex_artist, ex_title = "", existing_name
        
    ALIAS_MAP = {
        "3pc": "threepiece",
        "4evermore": "forevermore",
        "forevermore": "forevermore",
        "2pac": "tupac",
        "tupacshakur": "tupac"
    }

    def norm_title(t):
        t_lower = t.lower()
        # Detect version markers BEFORE the bracket-stripping regex below runs -
        # otherwise "(Radio Edit)"/"(Explicit)" gets silently discarded as if it
        # were decorative album-name noise (e.g. "[The Evolution of Robin
        # Thicke]"), making a Radio Edit normalize identically to its base
        # Clean version and get wrongly treated as an already-owned duplicate.
        # store_track's own _normalize_track_key() already keeps these as
        # distinct, coexisting versions (via a _radioedit/_explicit/_clean
        # suffix) - this mirrors that so the pre-download duplicate check
        # agrees with what vaulting itself considers a real duplicate.
        is_radio = 'radio edit' in t_lower or 'radio version' in t_lower
        is_explicit = 'explicit' in t_lower and 'clean' not in t_lower

        t = t_lower
        import unicodedata
        t = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode('ascii')
        t = t.replace('&', ' and ')
        t = re.sub(r'\[.*?\]|\(.*?\)', '', t)
        removals = ["radio edit", "single mix", "album version", "rerecorded", "clean", "explicit", "remix", "version", "feat", "ft"]
        for r in removals:
            t = t.replace(r, "")
        clean_t = re.sub(r'[^a-z0-9]', '', t)
        for k, v in ALIAS_MAP.items():
            clean_t = clean_t.replace(k, v)
        tag = "_radioedit" if is_radio else ("_explicit" if is_explicit else None)
        return clean_t, tag

    ex_base, ex_tag = norm_title(ex_title)
    check_base, check_tag = norm_title(check_title)
    if ex_base != check_base:
        return False, ""
    # A remix is a different recording from the original: "Go Crazy (Remix)" must not count as
    # already owning "Go Crazy" (norm_title strips the word "remix", found 2026-09-25).
    if ('remix' in ex_title.lower()) != ('remix' in check_title.lower()):
        return False, ""
    # A version tag only rules out a match when BOTH sides carry one and they
    # disagree (e.g. an on-disk Explicit copy vs. a request specifically for
    # Clean). Most real callers compare a bare title with no tag at all -
    # e.g. iHeart's live "now playing" metadata never includes "(Radio
    # Edit)" - against an on-disk filename that legitimately carries one.
    # Confirmed live 2026-09-14: "Usher - Nice & Slow" only exists on disk as
    # "(Radio Edit)"/"(Radio Version)", so the old exact-match here made the
    # iHeart poller re-stage it as a new discovery on every single play,
    # forever, since the untagged live title could never match either
    # on-disk copy. That's the same song we already own, not a request for a
    # specific missing variant, so a missing tag on either side should not
    # block the match.
    if ex_tag and check_tag and ex_tag != check_tag:
        return False, ""
        
    # Titles match! Now extract and clean co-artists
    def get_primary_artists(a):
        a_clean = a.lower()
        import unicodedata
        a_clean = unicodedata.normalize('NFKD', a_clean).encode('ascii', 'ignore').decode('ascii')
        a_clean = a_clean.replace('&', ' and ')
        a_clean = re.split(r'\s+(?:feat\.?|featuring|with|w/|f/|and)\s+', a_clean)[0]
        parts = re.split(r'[,/;]', a_clean)
        res = []
        for x in parts:
            c = re.sub(r'[^a-z0-9]', '', x).strip()
            for k, v in ALIAS_MAP.items():
                c = c.replace(k, v)
            if c:
                res.append(c)
        return res
        
    ex_artists = get_primary_artists(ex_artist)
    check_artists = get_primary_artists(check_artist)
    
    if set(ex_artists) & set(check_artists):
        return True, "Already in library (smart artist match)"
        
    ex_artist_clean = re.sub(r'[^a-z0-9]', '', ex_artist.lower().replace('&', 'and'))
    import unicodedata
    ex_artist_clean = unicodedata.normalize('NFKD', ex_artist_clean).encode('ascii', 'ignore').decode('ascii')
    check_artist_clean = re.sub(r'[^a-z0-9]', '', check_artist.lower().replace('&', 'and'))
    check_artist_clean = unicodedata.normalize('NFKD', check_artist_clean).encode('ascii', 'ignore').decode('ascii')
    for k, v in ALIAS_MAP.items():
        ex_artist_clean = ex_artist_clean.replace(k, v)
        check_artist_clean = check_artist_clean.replace(k, v)

    if ex_artist_clean in check_artist_clean or check_artist_clean in ex_artist_clean:
        return True, "Already in library (substring artist match)"
        
    return False, ""

class UrlQueue:
    def __init__(self, save_filename="saved_queue.json"):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.items = []
        self.counter = 0
        self._save_filename = save_filename

    def _save_to_disk(self):
        import json
        try:
            queue_file = os.path.join(BASE_DIR, "configs", self._save_filename)
            with open(queue_file, "w", encoding="utf-8") as f:
                json.dump(self.items, f, indent=4)
        except Exception as e:
            sys.stdout.write(f"\n[ERROR] Failed to save queue to disk: {e}\n")
            sys.stdout.flush()

    def put(self, item):
        with self.lock:
            self.counter += 1
            item = item.copy()
            item['id'] = self.counter
            self.items.append(item)
            self._save_to_disk()
            self.cond.notify()
            return item['id']

    def get(self, timeout=None):
        with self.lock:
            if not self.items:
                if timeout is not None:
                    self.cond.wait(timeout)
                    if not self.items:
                        raise queue.Empty
                else:
                    while not self.items:
                        self.cond.wait()
            item = self.items.pop(0)
            self._save_to_disk()
            return item

    def remove(self, item_id):
        with self.lock:
            for i, item in enumerate(self.items):
                if item.get('id') == item_id:
                    self.items.pop(i)
                    self._save_to_disk()
                    self.cond.notify_all()
                    return True
            return False

    def qsize(self):
        with self.lock:
            return len(self.items)

    def empty(self):
        with self.lock:
            return len(self.items) == 0

    def task_done(self):
        pass

    def get_all(self):
        with self.lock:
            return [dict(item) for item in self.items]

def is_inspirational_track(artist: str, title: str, album: str = "") -> bool:
    if not artist or not title:
        return False
    
    artist_lower = artist.lower()

    # Known gospel acts. PJ Morton was removed (he records R&B too: "Say So" is R&B/Soul);
    # Mary Mary added (Apple files their crossover hit "Shackles" under R&B/Soul).
    g_artists = [
        "smokie norful", "marvin sapp", "kirk franklin", "helen baylor", "fred hammond",
        "donnie mcclurkin", "yolanda adams", "cece winans", "tamela mann", "tasha cobbs",
        "kierra sheard", "hezekiah walker", "t.d. jakes", "richard smallwood", "john p. kee",
        "shirley caesar", "james fortune", "byron cage", "j.j. hairston", "koryn hawthorne",
        "zacardi cortez", "jonathan mcreynolds", "vashawn mitchell", "charles jenkins",
        "william murphy", "marvin winans", "clark sisters", "lisa knowles-smith",
        "josh copeland", "ted & sheri", "mary mary", "milton brunson", "douglas miller",
        "jekalyn carr", "bishop larry trotter", "mississippi mass choir", "chicago mass choir",
        "williams brothers", "victorious army", "tri-city singers", "donald lawrence",
        "andraé crouch", "andrae crouch", "edwin hawkins", "walter hawkins", "tramaine hawkins",
        "georgia mass choir", "rance allen", "cantons", "jackson southernaires", "sensational nightingales",
        "mighty clouds of joy", "lee williams", "spiritual qc", "canton spirituals",
        # 2026-09-27 library check: gospel acts Apple files under R&B/Soul (or "African") or doesn't have
        "karen clark-sheard", "karen clark sheard", "j moss", "brent jones", "kurt carr", "dorinda clark-cole",
        "ricky dillard", "sounds of blackness", "youthful praise", "dan willis", "luther barnes"
    ]
    on_gospel_list = any(ga in artist_lower for ga in g_artists)
    church_words_in_artist = bool(
        re.search(r'\b(choir|mass choir|gospel|pastor|bishop|rev\.?|reverend|apostle|elder|evangelist)\b', artist_lower))
    # Titles that are strong enough to overrule Apple's genre ("Rev. Luther Barnes" is filed R&B/Soul).
    # "Bishop" is left out on purpose - "Bishop Briggs" is a pop act.
    clergy_or_choir_artist = bool(re.search(r'\b(choir|pastor|rev\.?|reverend|apostle)\b', artist_lower))

    # 1. Apple Music's genre for THIS SONG decides when it knows the song (2026-09-25: 30 of 34 real cases
    #    right, vs 10 of 28 for the old title-keyword + MusicBrainz check, which routed "God's Plan",
    #    "Jesus Walks", Aretha's "Respect" and Al Green's "Let's Stay Together" to Gospel and missed Pastor
    #    Mike Jr., Chandler Moore, Todd Dulaney...). Title keywords (god/jesus/lord/church) are no longer
    #    used at all - they matched too many secular hits.
    from modules.genre_lookup import get_song_genre, is_gospel_genre, get_artist_genres
    song_genre = get_song_genre(artist, title)
    if song_genre:
        if is_gospel_genre(song_genre):
            return True
        # a listed gospel act's crossover single (Mary Mary "Shackles" is filed R&B/Soul) is still gospel.
        # Only clergy/choir titles override Apple here, not every church word - "Bishop Briggs" is a pop act.
        return on_gospel_list or clergy_or_choir_artist

    # 2. Apple doesn't know the song: listed gospel act, or church words in the artist name
    #    ("Rev. Ernest Davis Jr.", "... Mass Choir", "Pastor Mike Jr.")
    if on_gospel_list or church_words_in_artist:
        return True

    # 3. Last resort, MusicBrainz ARTIST genres - only when gospel/christian is most of the artist's genres.
    #    One gospel tag among six (Aretha, Al Green, Sam Cooke, Billy Preston) is a soul artist with gospel roots.
    genres = get_artist_genres(artist)
    if genres:
        churchy = sum(1 for g in genres if any(t in g for t in ("gospel", "christian", "ccm", "worship")))
        if churchy / len(genres) >= 0.5:
            return True

    return False

class SystemState:
    def __init__(self):
        self.url_queue = UrlQueue()
        # Holds jobs that missed on Soulseek and need the Windows scrape worker
        # (real cookies + residential IP) via /api/pull_jobs. Separate save file
        # so it doesn't collide with url_queue's own persistence.
        self.scrape_queue = UrlQueue(save_filename="saved_scrape_queue.json")
        # Job ids already accepted from the broker's scrape_queue, so
        # poll_jobs_worker never re-queues one while it's actively being
        # processed. Checking url_queue.get_all() alone isn't enough - an
        # item is popped off that queue the instant downloader_worker starts
        # working on it, so it looks "not in queue" for the whole duration of
        # a download even though it's still in progress. Grows for the life
        # of the process; never needs pruning since job ids are unique.
        self.claimed_broker_job_ids = set()
        self.vault_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.downloader_threads = []
        self.vault_thread = None
        self.total_in_queue = 0
        self.completed_count = 0
        self.current_status = "Idle"
        self.current_track_url = "None"
        self.logs = collections.deque(["FMP ULTIMATE ONLINE. AUTO-MASTERING ACTIVE."], maxlen=15)
        self.boot_cleared = False
        self.last_stat_update = 0
        self.vault_total = 0
        self.folder_breakdown = {}
        self.pending_iheart_queue = []
        self.rejected_iheart = []
        self.pending_deletions = []
        self.load_pending()
        self.load_rejected()
        self.load_pending_deletions()
        self.load_saved_queue()
        self.load_saved_scrape_queue()
        self.start_spinner()

    def load_pending(self):
        import json
        configs_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        pending_file = os.path.join(configs_dir, "pending_iheart.json")
        if os.path.exists(pending_file):
            try:
                with open(pending_file, "r", encoding="utf-8") as f:
                    self.pending_iheart_queue = json.load(f)
                
                # Automatically fix targets for inspirational/gospel tracks
                updated = False
                for item in self.pending_iheart_queue:
                    artist = item.get('artist', '')
                    title = item.get('title', '')
                    target = item.get('target', '')
                    ts_str = item.get('timestamp', '')
                    
                    is_insp = is_inspirational_track(artist, title)
                    
                    # Parse timestamp (e.g. "05:58 AM")
                    before_9am = False
                    if ts_str:
                        try:
                            parts = ts_str.split(':')
                            if len(parts) >= 2:
                                hour = int(parts[0])
                                is_pm = "pm" in parts[1].lower()
                                if is_pm and hour != 12:
                                    hour += 12
                                elif not is_pm and hour == 12:
                                    hour = 0
                                if hour < 9:
                                    before_9am = True
                        except Exception:
                            pass
                            
                    if is_insp:
                        if target != "Shows/InspirationalChurch":
                            item['target'] = "Shows/InspirationalChurch"
                            updated = True
                    else:
                        if target == "Shows/InspirationalChurch":
                            item['target'] = ""
                            updated = True
                if updated:
                    self.save_pending()
            except Exception as e:
                self.log(f"[ERROR] Failed to load pending discoveries: {e}")
                self.pending_iheart_queue = []
        else:
            self.pending_iheart_queue = []

    def save_pending(self):
        import json
        configs_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        pending_file = os.path.join(configs_dir, "pending_iheart.json")
        try:
            with open(pending_file, "w", encoding="utf-8") as f:
                json.dump(self.pending_iheart_queue, f, indent=4)
        except Exception as e:
            self.log(f"[ERROR] Failed to save pending discoveries: {e}")

    def load_pending_deletions(self):
        import json
        configs_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        pending_file = os.path.join(configs_dir, "pending_deletions.json")
        if os.path.exists(pending_file):
            try:
                with open(pending_file, "r", encoding="utf-8") as f:
                    self.pending_deletions = json.load(f)
            except Exception as e:
                self.log(f"[ERROR] Failed to load pending deletions: {e}")
                self.pending_deletions = []
        else:
            self.pending_deletions = []

    def save_pending_deletions(self):
        import json
        configs_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        pending_file = os.path.join(configs_dir, "pending_deletions.json")
        try:
            with open(pending_file, "w", encoding="utf-8") as f:
                json.dump(self.pending_deletions, f, indent=4)
        except Exception as e:
            self.log(f"[ERROR] Failed to save pending deletions: {e}")

    def enqueue_deletion(self, path):
        with self.lock:
            if path not in self.pending_deletions:
                self.pending_deletions.append(path)
                self.save_pending_deletions()

    def poll_deletions(self):
        with self.lock:
            items = list(self.pending_deletions)
            self.pending_deletions.clear()
            self.save_pending_deletions()
            return items

    def load_rejected(self):
        import json
        configs_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        rejected_file = os.path.join(configs_dir, "rejected_iheart.json")
        if os.path.exists(rejected_file):
            try:
                with open(rejected_file, "r", encoding="utf-8") as f:
                    self.rejected_iheart = json.load(f)
            except Exception as e:
                self.log(f"[ERROR] Failed to load rejected discoveries: {e}")
                self.rejected_iheart = []
        else:
            self.rejected_iheart = []

    def save_rejected(self):
        import json
        configs_dir = os.path.join(BASE_DIR, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        rejected_file = os.path.join(configs_dir, "rejected_iheart.json")
        try:
            with open(rejected_file, "w", encoding="utf-8") as f:
                json.dump(self.rejected_iheart, f, indent=4)
        except Exception as e:
            self.log(f"[ERROR] Failed to save rejected discoveries: {e}")

    def load_saved_queue(self):
        import json
        queue_file = os.path.join(BASE_DIR, "configs", "saved_queue.json")
        if os.path.exists(queue_file):
            try:
                with open(queue_file, "r", encoding="utf-8") as f:
                    items = json.load(f)
                
                updated = False
                for item in items:
                    artist = item.get('artist', '')
                    title = item.get('title', '')
                    target = item.get('target', '')
                    if artist and title:
                        is_insp = is_inspirational_track(artist, title)
                        if is_insp:
                            if target != "Shows/InspirationalChurch":
                                item['target'] = "Shows/InspirationalChurch"
                                updated = True
                        else:
                            if target == "Shows/InspirationalChurch":
                                item['target'] = ""
                                updated = True
                    self.url_queue.items.append(item)
                
                if updated:
                    with open(queue_file, "w", encoding="utf-8") as f:
                        json.dump(items, f, indent=4)
                
                if self.url_queue.items:
                    self.url_queue.counter = max(item.get('id', 0) for item in self.url_queue.items)
                self.log(f"[SYSTEM] Restored {len(self.url_queue.items)} items from saved queue.")
            except Exception as e:
                self.log(f"[ERROR] Failed to restore saved queue: {e}")

    def load_saved_scrape_queue(self):
        import json
        queue_file = os.path.join(BASE_DIR, "configs", "saved_scrape_queue.json")
        if os.path.exists(queue_file):
            try:
                with open(queue_file, "r", encoding="utf-8") as f:
                    items = json.load(f)
                self.scrape_queue.items.extend(items)
                if self.scrape_queue.items:
                    self.scrape_queue.counter = max(item.get('id', 0) for item in self.scrape_queue.items)
                self.log(f"[SYSTEM] Restored {len(self.scrape_queue.items)} items from saved scrape queue.")
            except Exception as e:
                self.log(f"[ERROR] Failed to restore saved scrape queue: {e}")

    def update_count(self):
        with self.lock:
            self.total_in_queue = self.url_queue.qsize() + self.vault_queue.qsize()

    def log(self, message: str):
        with self.lock:
            self.logs.append(message)
            try:
                sys.stdout.write(f"\n[SYSTEM] {message}\n")
                sys.stdout.flush()
            except Exception:
                try:
                    clean_msg = message.encode('ascii', errors='replace').decode('ascii')
                    sys.stdout.write(f"\n[SYSTEM] {clean_msg}\n")
                    sys.stdout.flush()
                except Exception:
                    pass

    def set_status(self, status: str, url: str = None):
        with self.lock:
            self.current_status = status
            if url is not None:
                self.current_track_url = url

    def increment_completed(self):
        with self.lock:
            self.completed_count += 1

    def _update_vault_stats(self):
        vt = 0
        fb = collections.defaultdict(int)
        valid_folders = {"Classics", "Old School 70s80s", "Throwbacks 90s2000s", "New School 2010+", "Shows", "Live"}
        from config import CSV_BLUEPRINT
        import csv, os
        if os.path.exists(CSV_BLUEPRINT):
            try:
                with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        vt += 1
                        path = row.get('File Path', '')
                        if path:
                            parts = path.replace('\\', '/').split('/')
                            if len(parts) > 2 and parts[0].upper() == 'Z:':
                                folder = parts[1]
                                if folder in valid_folders:
                                    fb[folder] += 1
            except:
                pass
        self.vault_total = vt
        self.folder_breakdown = dict(fb)

    def get_snapshot(self):
        with self.lock:
            import time
            if time.time() - self.last_stat_update > 5:
                self.last_stat_update = time.time()
                self._update_vault_stats()
                
            return {
                "total_in_queue": self.total_in_queue,
                "completed_count": self.completed_count,
                "current_status": self.current_status,
                "current_track_url": self.current_track_url,
                "logs": list(self.logs),
                "vault_total": self.vault_total,
                "folder_breakdown": self.folder_breakdown,
                "download_queue": self.url_queue.get_all(),
                "pending_iheart_queue": list(self.pending_iheart_queue),
                "workstation": getattr(self, 'workstation_status', {})
            }

    def start_spinner(self):
        def spin():
            spinner = itertools.cycle(['|', '/', '-', '\\'])
            while True:
                with self.lock:
                    status = self.current_status
                    last_log = self.logs[-1] if self.logs else 'Processing'
                
                if status == "Running":
                    display_log = last_log
                    if len(display_log) > 65:
                        display_log = display_log[:62] + "..."
                    try:
                        sys.stdout.write(f"\r[WORKING] {display_log} {next(spinner)}".ljust(90))
                        sys.stdout.flush()
                    except Exception:
                        try:
                            clean_log = display_log.encode('ascii', errors='replace').decode('ascii')
                            sys.stdout.write(f"\r[WORKING] {clean_log} {next(spinner)}".ljust(90))
                            sys.stdout.flush()
                        except Exception:
                            pass
                time.sleep(0.15)
        threading.Thread(target=spin, daemon=True).start()

state = SystemState()
acoustid_api_lock = threading.Lock()

def is_video_title(title: str) -> bool:
    t = title.lower()
    bad_phrases = ["official video", "music video", "official music video", "video clip", "videoclip", "lyric video", "lyrics video"]
    for phrase in bad_phrases:
        if phrase in t:
            return True
    if "(video)" in t or "[video]" in t:
        return True
    return False

def is_karaoke_or_tribute(title: str, artist: str = "") -> bool:
    t = title.lower()
    a = artist.lower() if artist else ""
    exclude_keywords = ["karaoke", "tribute", "instrumental", "backing track", "originally performed", "originally by", "cover version", "piano cover", "acoustic cover"]
    return any(k in t or k in a for k in exclude_keywords)

SEARCH_STOP_WORDS = {'a', 'an', 'the', 'and', 'or', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'is', 'it', 'my', 'your', 'me', 'you', 'we', 'us', 'clean', 'explicit', 'remix', 'radio', 'edit', 'version', 'feat', 'ft', 'official', 'audio', 'video'}

def is_valid_search_match(candidate_title: str, candidate_artist: str, query_str: str) -> bool:
    if is_karaoke_or_tribute(candidate_title, candidate_artist):
        return False
        
    cand_title_lower = candidate_title.lower()
    cand_artist_lower = candidate_artist.lower()
    
    parts = [p.strip() for p in query_str.split('-') if p.strip()]
    if len(parts) >= 2:
        q_artist, q_title = parts[0].lower(), parts[1].lower()
        q_title_words = set(re.findall(r'\w+', q_title)) - SEARCH_STOP_WORDS
        if not q_title_words:
            q_title_words = set(re.findall(r'\w+', q_title))
            
        title_overlap = q_title_words.intersection(set(re.findall(r'\w+', cand_title_lower)))
        if not title_overlap:
            return False
            
        q_artist_words = set(re.findall(r'\w+', q_artist)) - SEARCH_STOP_WORDS
        if q_artist_words:
            cand_all_words = set(re.findall(r'\w+', cand_artist_lower + " " + cand_title_lower))
            artist_overlap = q_artist_words.intersection(cand_all_words)
            if not artist_overlap:
                return False
    else:
        query_words = set(re.findall(r'\w+', query_str.lower())) - SEARCH_STOP_WORDS
        cand_all_words = set(re.findall(r'\w+', cand_artist_lower + " " + cand_title_lower))
        if query_words and not query_words.intersection(cand_all_words):
            return False
            
    return True


def extract_playlist_urls(playlist_url: str) -> list:
    cmd = YT_DLP_CMD + ["--flat-playlist", "--print", "url", playlist_url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True)
        return [line.strip() for line in result.stdout.split('\n') if 'watch?v=' in line or 'youtu.be/' in line]
    except Exception as e:
        state.log(f"[ERROR] Playlist Extraction Failed: {e}")
        logging.error(f"Playlist extraction error for {playlist_url}: {e}")
        return []

def _vm_basic_auth_b64():
    """Base64 'user:pass' for the nginx basic-auth login on the Oracle VM's Ultimate host.
    Read from .env (ULTIMATE_BASIC_AUTH_USER / ULTIMATE_BASIC_AUTH_PASS) so the secret is
    never hardcoded in source. Raises if the password is missing rather than falling back."""
    import base64
    user = os.getenv("ULTIMATE_BASIC_AUTH_USER", "fmpadmin")
    password = os.getenv("ULTIMATE_BASIC_AUTH_PASS")
    if not password:
        raise RuntimeError("ULTIMATE_BASIC_AUTH_PASS is not set in .env")
    return base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("utf-8")


def _acknowledge_remote_job(job_id):
    """
    Fire-and-forget: immediately tell the remote VM to clear this job from its queue.
    Called when a duplicate track is detected before download to prevent the broker
    from re-serving the same job in a ghost polling loop.
    """
    import base64
    import ssl as _ssl
    remote_host = os.getenv("REMOTE_VM_IP", "149.130.219.114")
    auth_b64 = _vm_basic_auth_b64()
    ssl_ctx = _ssl.create_default_context()
    # check_hostname=False is required since this connects by raw IP while
    # the cert is issued for ultimate.fmpmediagroup.com - but verify_mode
    # stays at its default CERT_REQUIRED (previously forced to CERT_NONE,
    # disabling all certificate validation). The cert here is a real
    # Let's Encrypt cert, not self-signed, so there's no reason to also
    # accept a cert from an untrusted CA - this still stops a MITM
    # presenting a forged/invalid cert.
    ssl_ctx.check_hostname = False
    try:
        req = urllib.request.Request(
            f"https://{remote_host}/api/queue/remove",
            data=json.dumps({"id": job_id}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth_b64}",
                "Host": "ultimate.fmpmediagroup.com"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as res:
            # A non-exception response isn't necessarily success - this route
            # returns HTTP 200 with {"status": "error"} in its body when the
            # item wasn't found, which urlopen doesn't treat as a failure.
            # Previously this logged "Cleared" unconditionally on any 200,
            # masking the queue-removal bug above (which meant this call
            # always silently failed to actually clear the job).
            res_data = json.loads(res.read().decode('utf-8'))
        if res_data.get("status") == "ok":
            state.log(f"[Broker ACK] Cleared duplicate job {job_id} from remote VM queue.")
        else:
            logging.warning(f"[Broker ACK] Remote VM reported job {job_id} not found in queue: {res_data.get('message')}")
    except Exception as e:
        logging.warning(f"[Broker ACK] Failed to remove job {job_id} from remote queue: {e}")


def downloader_worker():
    while not state.stop_event.is_set():
        try:
            task = state.url_queue.get(timeout=1)
        except queue.Empty:
            if state.vault_queue.empty():
                state.set_status("Idle")
            continue

        staging_task_dir = None
        url = task.get('url')
        task_type = task.get('type', 'ingest')
        target_override = task.get('target')
        
        # Check for duplicates before download if artist/title are known and not explicitly overwriting
        expected_artist = task.get("artist")
        expected_title = task.get("title")
        if expected_artist and expected_title and not task.get('overwrite'):
            from modules.storage import VaultManager
            vm = VaultManager()
            is_duplicate = False
            from config import CSV_BLUEPRINT
            if os.path.exists(CSV_BLUEPRINT):
                with vm._csv_lock:
                    try:
                        import csv
                        # When the task says which version it is, only a catalog row with the SAME explicit
                        # flag is a duplicate - an explicit copy must not block downloading its clean version
                        # (names alone can't tell them apart: explicit files are never tagged). 2026-09-25.
                        wanted_explicit = task.get('explicit')
                        with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                            reader = csv.DictReader(f)
                            for row in reader:
                                existing_name = row.get('Track Name')
                                if wanted_explicit is not None and \
                                        ((row.get('Explicit') or '').strip().lower() in ('true', '1')) != bool(wanted_explicit):
                                    continue
                                if existing_name:
                                    is_dup, reason = is_smart_duplicate(existing_name, expected_artist, expected_title, vm=vm)
                                    if is_dup:
                                        is_duplicate = True
                                        break
                    except Exception as ex:
                        logging.error(f"Error checking duplicate in downloader_worker: {ex}")
            if is_duplicate:
                state.log(f"[SKIPPED] Duplicate Track (checked before download): {expected_artist} - {expected_title}")
                # Immediately clear from remote VM queue if this job came from the broker
                job_id = task.get('job_id')
                if job_id:
                    threading.Thread(target=_acknowledge_remote_job, args=(job_id,), daemon=True).start()
                state.url_queue.task_done()
                state.update_count()
                continue
        
        if task_type == 'local':
            state.set_status("Running", os.path.basename(url))
            task_id = task.get('task_id')
            raw_path = url
            bitrate = "320"
            original_is_video = False
            meta = {'release_year': 'Unknown', 'lyrics': 'Not Found', 'url': 'Local Upload', 'item_type': 'Music'}
            staging_task_dir = os.path.dirname(raw_path)
            
            # Skip validation and download phases, go straight to Phase 2.5 (Auto-Mastering)
            am = AutoMaster()
            handoff_success = False
            
            try:
                state.log(f"Phase 2.5: Auto-Mastering Local Upload [{os.path.basename(raw_path)}]")
                mastered_path, updates = am.process_file(raw_path, original_bitrate=bitrate)
                
                if not mastered_path:
                    handoff_success = False
                    continue
                
                meta.update(updates)
                
                # Era-to-Energy Category Folder Interception
                release_year = meta.get('release_year', 'Unknown')
                clean_name = os.path.basename(mastered_path)
                
                track_title = meta.get('title', 'Unknown Title')
                if is_video_title(track_title) or is_video_title(clean_name):
                    state.log(f"[WARNING] Video keyword detected in title or original filename: \"{track_title}\" - routing to Unsorted_Review for manual inspection.")
                    era_folder = "Unsorted_Review"
                    target_override = "Unsorted_Review"
                elif target_override:
                    era_folder = target_override
                else:
                    if "live" in clean_name.lower():
                        era_folder = "Live"
                    elif not release_year or str(release_year).lower() in ['unknown', 'verify year', '']:
                        era_folder = "Unsorted_Review"
                    else:
                        try:
                            year_int = int(str(release_year)[:4])
                            if year_int < 1970:
                                era_folder = "Classics"
                            elif 1970 <= year_int <= 1989:
                                era_folder = "Old School 70s80s"
                            elif 1990 <= year_int <= 2009:
                                era_folder = "Throwbacks 90s2000s"
                            else:
                                era_folder = "New School 2010+"
                        except:
                            era_folder = "Unsorted_Review"
                    target_override = era_folder
                
                # Map the era_folder to a clean era name for energy_category
                clean_cat = "Throwbacks"
                folder_lower = era_folder.lower()
                if "classics" in folder_lower:
                    clean_cat = "Classics"
                elif "old school" in folder_lower:
                    clean_cat = "Old School"
                elif "throwbacks" in folder_lower:
                    clean_cat = "Throwbacks"
                elif "new school" in folder_lower:
                    clean_cat = "New School"
                
                meta['energy_category'] = clean_cat

                state.vault_queue.put({'path': mastered_path, 'meta': meta, 'task_id': task_id, 'job_id': task.get('job_id'), 'target': target_override})
                handoff_success = True
                
            except Exception as e:
                state.log(f"[CRITICAL LOCAL UPLOADER ERROR] {e}")
                logging.exception(f"Local Uploader Worker CRASH for {url}: {e}")
            finally:
                if not handoff_success:
                    if os.path.exists(staging_task_dir):
                        shutil.rmtree(staging_task_dir, ignore_errors=True)
                
                state.url_queue.task_done()
                state.update_count()
            continue

        # YTM-DLP: Custom search interceptor for raw text queries
        if url and not url.startswith("http://") and not url.startswith("https://"):
            query_str = url
            if query_str.startswith("ytsearch1:"):
                query_str = query_str[len("ytsearch1:"):]
            elif query_str.startswith("ytsearch:"):
                query_str = query_str[len("ytsearch:"):]
            
            query_clean = re.sub(r'\[.*?\]', '', query_str).strip()
            query_clean = re.sub(r'\s+', ' ', query_clean)
            
            state.log(f"[YTM-DLP] Resolving query: \"{query_str}\" (Cleaned: \"{query_clean}\") via YouTube Music API...")
            try:
                from ytmusicapi import YTMusic
                ytm = YTMusic()
                best_match = None
                
                # Step 1: Try songs filter with strict title/artist match validation
                results = ytm.search(query_clean, filter="songs")
                if results and isinstance(results, list):
                    for res in results:
                        title_cand = res.get('title', '')
                        artists_list = res.get('artists', [])
                        artist_cand = ", ".join(x.get('name', '') for x in artists_list) if isinstance(artists_list, list) else ""
                        if is_valid_search_match(title_cand, artist_cand, query_clean) and res.get('videoId'):
                            best_match = res
                            break
                            
                # Step 2: If no song match found, try unfiltered search (video/songs) with validation
                if not best_match:
                    unfiltered = ytm.search(query_clean)
                    if unfiltered and isinstance(unfiltered, list):
                        for res in unfiltered:
                            title_cand = res.get('title', '')
                            artists_list = res.get('artists', [])
                            artist_cand = ", ".join(x.get('name', '') for x in artists_list) if isinstance(artists_list, list) else ""
                            if is_valid_search_match(title_cand, artist_cand, query_clean) and res.get('videoId'):
                                best_match = res
                                break
                                
                if best_match and best_match.get('videoId'):
                    video_id = best_match.get('videoId')
                    resolved_url = f"https://music.youtube.com/watch?v={video_id}"
                    title = best_match.get('title', 'Unknown')
                    artists_list = best_match.get('artists', [])
                    artists = ", ".join([a.get('name', 'Unknown') for a in artists_list]) if isinstance(artists_list, list) else "Unknown"
                    duration = best_match.get('duration', '--:--')
                    state.log(f"[YTM-DLP] Resolved to: \"{title}\" by {artists} ({duration}) -> {resolved_url}")
                    url = resolved_url
                else:
                    state.log(f"[WARNING] No valid title/artist match in ytmusicapi results for \"{query_clean}\". Falling back to ytsearch1.")
                    url = f"ytsearch1:{query_clean} (Official Audio)"
            except Exception as e:
                state.log(f"[ERROR] ytmusicapi search failed: {e}. Falling back to ytsearch1.")
                url = f"ytsearch1:{query_clean} (Official Audio)"
        
        state.set_status("Running", url)
        task_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        staging_task_dir = os.path.join(STAGING_DIR, task_id)
        
        gk, tr, am = Gatekeeper(), Transporter(), AutoMaster()
        handoff_success = False

        try:
            state.log(f"Phase 1: Validating {url}")
            v, meta = gk.process_request(url)
            
            # THE GATEKEEPER BLIND PASS-THROUGH OR VETO
            if not v:
                if meta.get("veto"):
                    state.log(f"[VETO] Rejection: {meta.get('error', 'Strict veto applied.')}")
                    handoff_success = False
                    job_id = task.get('job_id')
                    if job_id:
                        threading.Thread(target=_acknowledge_remote_job, args=(job_id,), daemon=True).start()
                    continue
                state.log(f"[WARNING] Gatekeeper blind: {meta.get('error', 'Unknown')}. Forcing SomeDL override.")
                meta = {'release_year': 'Unknown', 'lyrics': 'Not Found', 'url': url}

            if task.get('exact_source'):
                # EXACT-SOURCE MODE: download precisely this link. No artist/title is passed, so
                # download_track never runs its Soulseek search (which returns whatever version
                # Soulseek has - usually the explicit album cut - not the release that was picked).
                state.log(f"Phase 2: Downloading the exact link (exact-source mode, no Soulseek search)")
                raw_path, bitrate = tr.download_track(url, task_id=task_id)
                expected_seconds = task.get('expected_seconds')
                if raw_path and expected_seconds:
                    try:
                        from mutagen.mp3 import MP3 as _MP3ForExactCheck
                        got_seconds = _MP3ForExactCheck(raw_path).info.length
                    except Exception:
                        got_seconds = None
                    if got_seconds is None or abs(got_seconds - float(expected_seconds)) > 6:
                        state.log(f"[EXACT-SOURCE REFUSED] Downloaded length {got_seconds} s does not match the "
                                  f"verified release ({expected_seconds} s) - not saving it: {url}")
                        handoff_success = False
                        continue
            else:
                state.log(f"Phase 2: Downloading via SomeDL")
                raw_path, bitrate = tr.download_track(
                    url, task_id=task_id,
                    artist=task.get('artist') or meta.get('artist'),
                    title=task.get('title') or meta.get('title'),
                )

            # Soulseek missed and we're on the VM - hand off to the Windows scrape
            # worker instead of logging this as a dead link.
            if bitrate == "DELEGATE":
                state.log(f"[SCRAPE QUEUE] No Soulseek match, queued for Windows worker: {url}")
                state.scrape_queue.put({
                    'url': url,
                    'target': task.get('target', ''),
                    'title': task.get('title') or meta.get('title'),
                    'artist': task.get('artist') or meta.get('artist'),
                    'auto_linked': task.get('auto_linked', False),
                    'explicit': task.get('explicit'),
                    'is_radio': task.get('is_radio'),
                    'overwrite': task.get('overwrite', False),
                })
                handoff_success = False
                continue

            original_is_video = False
            if raw_path:
                original_is_video = is_video_title(os.path.basename(raw_path))

            # --- THE GRACEFUL SKIP & DEAD LINK LOGGER ---
            # If the download failed entirely or generated a dummy file
            if not raw_path or "blind_pass_through" in raw_path:
                state.log(f"[SKIP] Track completely blocked. Logging dead link.")
                dead_link_log = os.path.join(LOG_DIR, "failed_downloads.txt")
                with open(dead_link_log, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {url}\n")

                handoff_success = False
                job_id = task.get('job_id')
                if job_id:
                    threading.Thread(target=_acknowledge_remote_job, args=(job_id,), daemon=True).start()
                continue
            # --------------------------------------------

            # --- Phase 2.2: AcoustID Fingerprint Validation Gate ---
            validation_success = True
            api_key = os.getenv("ACOUSTID_API_KEY")
            
            expected_artist = task.get("artist") or meta.get("artist")
            expected_title = task.get("title") or meta.get("title") or meta.get("track_title")
            
            # Normalize expected metadata
            if not expected_title or str(expected_title).lower() in ["unknown", "unknown title", ""]:
                filename_no_ext = os.path.splitext(os.path.basename(raw_path))[0]
                if " - " in filename_no_ext:
                    expected_artist, expected_title = filename_no_ext.split(" - ", 1)
                else:
                    expected_title = filename_no_ext

            # Clean expected artist/title from trailing annotations
            if expected_artist:
                expected_artist = re.sub(r'\s*[([]\s*(?:clean|explicit|radio\s+edit|radio\s+version|album\s+version|main)\s*[)\]]', '', str(expected_artist), flags=re.I).strip()
            if expected_title:
                expected_title = re.sub(r'\s*[([]\s*(?:clean|explicit|radio\s+edit|radio\s+version|album\s+version|main)\s*[)\]]', '', str(expected_title), flags=re.I).strip()

            if task.get('fingerprint_verified'):
                # The caller already proved this exact release is the song (see /add) and AcoustID is known
                # to mislabel it - e.g. 2026-09-25 the Clark Sisters medley (MusicBrainz: 418 s on Bringing
                # It Back Home) comes back from AcoustID as 'Leonard Cohen - Hallelujah' at 418 s.
                state.log(f"[AcoustID INFO] Validation gate skipped - identity verified by caller: {task['fingerprint_verified']}")
            elif api_key and expected_title:
                state.log(f"[AcoustID Validation] Validating '{os.path.basename(raw_path)}' (Expected: '{expected_artist} - {expected_title}')...")
                
                fpcalc_bin = "fpcalc"
                if os.name == 'nt':
                    fpcalc_bin = r"C:\FMP_Ultimate\fpcalc.exe"
                else:
                    fpcalc_bin = "/usr/bin/fpcalc"
                
                cmd_fp = [fpcalc_bin, "-json", raw_path]
                try:
                    res_fp = subprocess.run(cmd_fp, capture_output=True, text=True, check=True)
                    data_fp = json.loads(res_fp.stdout)
                    duration_fp = data_fp.get("duration")
                    fingerprint_fp = data_fp.get("fingerprint")
                    
                    if duration_fp and fingerprint_fp:
                        params = {
                            "client": api_key,
                            "duration": int(round(duration_fp)),
                            "fingerprint": fingerprint_fp,
                            "meta": "recordings",
                            "format": "json"
                        }
                        url_lookup = "https://api.acoustid.org/v2/lookup"
                        req_data = urllib.parse.urlencode(params).encode('utf-8')
                        req_ac = urllib.request.Request(url_lookup, data=req_data, headers={'User-Agent': 'FMP-Ultimate-Validator/1.0'})
                        
                        with acoustid_api_lock:
                            time.sleep(0.4)
                            with urllib.request.urlopen(req_ac, timeout=15) as resp_ac:
                                ac_data = json.loads(resp_ac.read().decode('utf-8'))
                            
                        if ac_data and ac_data.get("status") == "ok":
                            results_ac = ac_data.get("results", [])
                            if results_ac:
                                from thefuzz import fuzz
                                is_match = False
                                best_match_details = "None"
                                
                                for res_item in results_ac:
                                    score = res_item.get("score", 0)
                                    if score < 0.5:
                                        continue
                                    
                                    recordings_ac = res_item.get("recordings", [])
                                    for rec_ac in recordings_ac:
                                        t_title = rec_ac.get("title", "")
                                        artists_ac = rec_ac.get("artists", [])
                                        artist_names_ac = [a.get("name", "") for a in artists_ac]
                                        
                                        artist_matched = False
                                        if not expected_artist or str(expected_artist).lower() in ["unknown", "unknown artist", ""]:
                                            artist_matched = True
                                        else:
                                            for art_name in artist_names_ac:
                                                if fuzz.token_set_ratio(_fold_accents(expected_artist), _fold_accents(art_name)) >= 95:
                                                    artist_matched = True
                                                    break
                                            if not artist_matched and artist_names_ac:
                                                combined_art = " & ".join(artist_names_ac)
                                                if fuzz.token_set_ratio(_fold_accents(expected_artist), _fold_accents(combined_art)) >= 95:
                                                    artist_matched = True
                                                    
                                        title_matched = fuzz.token_set_ratio(_fold_accents(expected_title), _fold_accents(t_title)) >= 95
                                        
                                        if artist_matched and title_matched:
                                            is_match = True
                                            best_match_details = f"'{', '.join(artist_names_ac)} - {t_title}' (Score: {score:.2f})"
                                            break
                                            
                                        # Combined fallback check for multi-hyphen filenames (e.g. 'Channel - Artist - Title')
                                        if artist_names_ac and t_title:
                                            combined_expected = f"{expected_artist} - {expected_title}"
                                            combined_ac = f"{' & '.join(artist_names_ac)} - {t_title}"
                                            if fuzz.token_set_ratio(_fold_accents(combined_expected), _fold_accents(combined_ac)) >= 95:
                                                is_match = True
                                                best_match_details = f"'{', '.join(artist_names_ac)} - {t_title}' (Score: {score:.2f} via combined fallback)"
                                                break
                                    if is_match:
                                        break
                                        
                                if is_match:
                                    state.log(f"[AcoustID Match Verified] Match: {best_match_details}")
                                else:
                                    # Check if the AcoustID database actually contains any metadata for the high score match.
                                    # If AcoustID has a fingerprint match but absolutely no metadata (title/artist tags) associated with it,
                                    # we cannot verify the track name, so we bypass rejection to prevent deleting valid files.
                                    has_any_metadata = False
                                    for res_item in results_ac:
                                        if res_item.get("score", 0) < 0.5:
                                            continue
                                        for rec_ac in res_item.get("recordings", []):
                                            if rec_ac.get("title") or rec_ac.get("artists"):
                                                has_any_metadata = True
                                                break
                                        if has_any_metadata:
                                            break
                                    
                                    if not has_any_metadata:
                                        state.log(f"[AcoustID WARNING] Match score {results_ac[0].get('score', 0):.2f} has no metadata in AcoustID. Bypassing validation gate.")
                                        is_match = True
                                        best_match_details = f"Unknown / No Metadata in AcoustID (Score: {results_ac[0].get('score', 0):.2f})"
                                        state.log(f"[AcoustID Match Verified] Match: {best_match_details}")
                                    else:
                                        best_guess = "Unknown / Other"
                                        best_rec = results_ac[0].get("recordings", [])
                                        if best_rec:
                                            g_artists = [a.get("name", "") for a in best_rec[0].get("artists", [])]
                                            g_title = best_rec[0].get("title", "")
                                            best_guess = f"'{', '.join(g_artists)} - {g_title}' (Score: {results_ac[0].get('score', 0):.2f})"
                                        
                                        state.log(f"[AcoustID REJECTED] Mismatch! Expected: '{expected_artist} - {expected_title}', Download actually is: {best_guess}")
                                        validation_success = False
                            else:
                                state.log(f"[AcoustID WARNING] No matches in catalog. Routing to Unsorted_Review folder.")
                                target_override = "Unsorted_Review"
                        else:
                            state.log(f"[AcoustID WARNING] Lookup returned error status: {ac_data.get('status')}")
                    else:
                        state.log(f"[AcoustID WARNING] fpcalc returned empty duration or fingerprint.")
                except Exception as ve:
                    state.log(f"[AcoustID WARNING] Validation gate skipped due to error: {ve}")
            else:
                state.log(f"[AcoustID INFO] Validation gate skipped (no API key or missing expected title metadata).")
                
            if not validation_success:
                if os.path.exists(raw_path):
                    try:
                        os.remove(raw_path)
                    except:
                        pass
                if os.path.exists(staging_task_dir):
                    shutil.rmtree(staging_task_dir, ignore_errors=True)
                # Same as the duplicate-skip path above - if this came from
                # the broker, clear it from the VM's queue. Without this the
                # job just sits there forever: claimed_broker_job_ids means
                # Windows will never re-poll it, and nothing else ever tells
                # the VM this job is done, so it becomes a permanent zombie
                # entry in /api/pull_jobs - confirmed live 2026-09-03 (Q-Tip
                # - Vivrant Thing, rejected for a mislabeled YouTube source).
                job_id = task.get('job_id')
                if job_id:
                    threading.Thread(target=_acknowledge_remote_job, args=(job_id,), daemon=True).start()
                state.url_queue.task_done()
                state.update_count()
                continue

            state.log(f"Phase 2.5: Auto-Mastering (Fingerprinting)")
            mastered_path, updates = am.process_file(raw_path, original_bitrate=bitrate)
            
            if not mastered_path:
                handoff_success = False
                job_id = task.get('job_id')
                if job_id:
                    threading.Thread(target=_acknowledge_remote_job, args=(job_id,), daemon=True).start()
                continue
            
            # Protect the yt-dlp year
            if updates.get('release_year') in ["Unknown", "Verify Year", ""]:
                gk_year = meta.get('release_year')
                if gk_year and gk_year not in ["Unknown", "Verify Year", ""]:
                    updates['release_year'] = gk_year
            
            meta.update(updates)
            
            # Era-to-Energy Category Folder Interception
            release_year = meta.get('release_year', 'Unknown')
            clean_name = os.path.basename(mastered_path)
            
            track_title = meta.get('title', 'Unknown Title')
            if original_is_video or is_video_title(track_title) or is_video_title(clean_name):
                state.log(f"[WARNING] Video keyword detected in title or original filename: \"{track_title}\" - routing to Unsorted_Review for manual inspection.")
                era_folder = "Unsorted_Review"
                target_override = "Unsorted_Review"
            elif target_override:
                era_folder = target_override
            else:
                if "live" in clean_name.lower():
                    era_folder = "Live"
                elif not release_year or str(release_year).lower() in ['unknown', 'verify year', '']:
                    era_folder = "Unsorted_Review"
                else:
                    try:
                        year_int = int(str(release_year)[:4])
                        if year_int < 1970:
                            era_folder = "Classics"
                        elif 1970 <= year_int <= 1989:
                            era_folder = "Old School 70s80s"
                        elif 1990 <= year_int <= 2009:
                            era_folder = "Throwbacks 90s2000s"
                        else:
                            era_folder = "New School 2010+"
                    except:
                        era_folder = "Unsorted_Review"
                target_override = era_folder
            
            # Map the era_folder to a clean era name for energy_category
            clean_cat = "Throwbacks"
            folder_lower = era_folder.lower()
            if "classics" in folder_lower:
                clean_cat = "Classics"
            elif "old school" in folder_lower:
                clean_cat = "Old School"
            elif "throwbacks" in folder_lower:
                clean_cat = "Throwbacks"
            elif "new school" in folder_lower:
                clean_cat = "New School"
            
            meta['energy_category'] = clean_cat

            # Auto-assign a Pool from data we already compute here but were
            # previously discarding -- clean_cat (from release_year, above)
            # for decade pools, and the same gospel/genre signal iHeart
            # Discoveries already uses for the Gospel specialty pool.
            # Nothing upstream ever set this before: confirmed live
            # 2026-09-14, 744 of 2070 catalog tracks (36%) had no Pool at
            # all. Left unset (for manual assignment, same as before) when
            # neither signal applies, rather than guessing.
            pool_artist = meta.get('artist', '')
            pool_title = meta.get('title', track_title)
            POOL_ID_BY_ERA = {"Classics": 2, "Old School": 7, "Throwbacks": 3, "New School": 1}
            # Sent to the gospel folder on purpose (discovery routing / the owner's choice) -> Gospel pool.
            # Before 2026-09-25 this fell through to the era default ("Throwbacks") whenever the genre
            # check didn't recognise the artist (Dr. Marcus Cosby "One Word" got pool 3).
            from config import IHEART_CHURCH_FOLDER as _CHURCH_FOLDER
            if target_override == _CHURCH_FOLDER or is_inspirational_track(pool_artist, pool_title):
                meta['music_pool_id'] = 5
            elif clean_cat in POOL_ID_BY_ERA:
                meta['music_pool_id'] = POOL_ID_BY_ERA[clean_cat]

            # Pass explicit status from queue item
            if 'explicit' in task:
                meta['explicit'] = task['explicit']
            else:
                # Fallback: check track title for keywords
                title_lower = track_title.lower()
                if 'explicit' in title_lower:
                    meta['explicit'] = True
                elif 'clean' in title_lower:
                    meta['explicit'] = False

            # Verify Explicit against real editorial sources (iTunes +
            # Deezer) instead of trusting a UI checkbox or filename guess
            # alone -- only overrides when BOTH sources confidently agree
            # on the same recording; otherwise keeps whatever the logic
            # above already decided. This is the same cross-check the
            # library accuracy audit already does, now running
            # automatically at ingest instead of only after the fact.
            try:
                from mutagen.mp3 import MP3 as _MP3ForDuration
                mastered_duration_ms = int(_MP3ForDuration(mastered_path).info.length * 1000)
            except Exception:
                mastered_duration_ms = None
            try:
                from modules.explicit_verify import verify_explicit
                verified_explicit = verify_explicit(pool_artist, pool_title, mastered_duration_ms, name_hint=f"{track_title} {os.path.basename(mastered_path)}")
                # A download queued as already verified from its own audio (e.g. compared against the
                # official clean/explicit releases) keeps that answer - the store lookup can only
                # match by title + length and can't tell a clean edit from the album cut.
                if verified_explicit is not None and not task.get('explicit_verified'):
                    meta['explicit'] = verified_explicit
            except Exception as e:
                logging.warning(f"Explicit verification failed for {pool_title}: {e}")

            # Pass is_radio status from queue item
            if 'is_radio' in task:
                meta['is_radio'] = task['is_radio']

            if 'auto_linked' in task:
                meta['auto_linked'] = task['auto_linked']

            state.vault_queue.put({
                'path': mastered_path,
                'meta': meta,
                'task_id': task_id,
                'job_id': task.get('job_id'),
                'target': target_override,
                'auto_linked': task.get('auto_linked', False),
                'overwrite': task.get('overwrite', False)
            })
            handoff_success = True
            
        except Exception as e:
            state.log(f"[CRITICAL DOWNLOADER ERROR] {e}")
            logging.exception(f"Downloader Worker CRASH for {url}: {e}")
        finally:
            if not handoff_success:
                if staging_task_dir and os.path.exists(staging_task_dir):
                    shutil.rmtree(staging_task_dir, ignore_errors=True)
            
            state.url_queue.task_done()
            state.update_count()

def _fold_accents(s):
    """Lower-case and strip accents, so AcoustID's 'Mýa' matches our 'Mya' (2026-09-25: an exact-source
    Mya download was rejected as a mismatch at fingerprint score 1.00 over the accent alone)."""
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFKD', str(s or '')) if not unicodedata.combining(c)).lower()


_ALT_MIX_WORDS = re.compile(r'\b(remix|mix|club|extended|dub|live|acoustic|instrumental|a ?cappella|rework|re-?edit|'
                            r'vip|bootleg|demo|unplugged|session|reprise|interlude|strip to the bone)\b', re.I)
# Version tags that ARE a counterpart of the same recording ("Radio Mix" is how some labels name the clean edit,
# e.g. Outkast "So Fresh, So Clean (Radio Mix)").
_COUNTERPART_TAGS = re.compile(r'\b(radio edit|radio version|radio mix|clean version|clean edit|clean|explicit version|explicit)\b', re.I)


def counterpart_rejection(original_title, candidate_title, original_seconds, candidate_seconds, radio_involved):
    """Why a search result is NOT a clean/explicit/radio-edit counterpart of our recording, or None if it may be.
    A counterpart is the same recording with words changed: a remix, club mix, live take etc. is a different
    recording, and so is anything whose length is far off (2026-09-25: the 7:32 Basement Boy club mix was
    auto-linked as the 'clean version' of the 3:49 Gypsy Woman radio single)."""
    orig_words = {w.lower() for w in _ALT_MIX_WORDS.findall(_COUNTERPART_TAGS.sub(' ', original_title or ''))}
    cand_words = {w.lower() for w in _ALT_MIX_WORDS.findall(_COUNTERPART_TAGS.sub(' ', candidate_title or ''))}
    extra = cand_words - orig_words
    if extra:
        return f"different mix/version ({', '.join(sorted(extra))})"
    if original_seconds and candidate_seconds:
        # clean and explicit edits of one recording run within seconds of each other; radio edits cut intros,
        # skits and verses, so allow more whenever a radio edit is on either side
        limit = 90 if radio_involved else 20
        gap = abs(float(original_seconds) - float(candidate_seconds))
        if gap > limit:
            return f"length differs by {gap:.0f}s (limit {limit}s)"
    return None


def trigger_single_song_counterpart_search(artist, title, is_explicit, target_folder, original_duration_seconds=0):
    def bg_search():
        try:
            import re
            import csv
            from ytmusicapi import YTMusic
            from modules.storage import VaultManager
            from config import CSV_BLUEPRINT
            
            ytm = YTMusic()
            vm = VaultManager()
            
            title_lower = title.lower()
            is_radio = 'radio edit' in title_lower or 'radio version' in title_lower
            
            if is_radio:
                current_category = 'radioedit'
            elif is_explicit:
                current_category = 'explicit'
            else:
                current_category = 'clean'
                
            clean_title = re.sub(r'\((?:explicit|clean|radio edit|radio version)\)', '', title, flags=re.I).strip()
            clean_title = re.sub(r'\s+', ' ', clean_title)
            
            # Formulate the queries we want to run based on current category
            targets = []
            if current_category == 'explicit':
                targets.append({'category': 'clean', 'query': f"{artist} {clean_title} Clean", 'explicit': False})
                targets.append({'category': 'radioedit', 'query': f"{artist} {clean_title} Radio Edit", 'explicit': False})
            elif current_category == 'radioedit':
                targets.append({'category': 'explicit', 'query': f"{artist} {clean_title} Explicit", 'explicit': True})
                targets.append({'category': 'clean', 'query': f"{artist} {clean_title} Clean", 'explicit': False})
            else:
                targets.append({'category': 'explicit', 'query': f"{artist} {clean_title} Explicit", 'explicit': True})
                targets.append({'category': 'radioedit', 'query': f"{artist} {clean_title} Radio Edit", 'explicit': False})
                
            for target in targets:
                search_query = target['query']
                target_cat = target['category']
                target_explicit = target['explicit']
                
                state.log(f"[Counterpart Search] Searching for {target_cat} version of '{artist} - {clean_title}'...")
                
                other_versions_songs = []
                try:
                    current_search = ytm.search(f"{artist} - {clean_title}", filter="songs")
                    if current_search:
                        song_item = current_search[0]
                        album_info = song_item.get("album", {}) or {}
                        album_id = album_info.get("id")
                        if album_id:
                            album_details = ytm.get_album(browseId=album_id)
                            other_versions = album_details.get("other_versions", [])
                            for ver in other_versions:
                                ver_explicit = ver.get("isExplicit", False)
                                if ver_explicit == target_explicit:
                                    ver_browse_id = ver.get("browseId")
                                    if ver_browse_id:
                                        ver_details = ytm.get_album(browseId=ver_browse_id)
                                        ver_tracks = ver_details.get("tracks", [])
                                        for t in ver_tracks:
                                            t_title = t.get("title", "")
                                            clean_t_title = re.sub(r'[^a-z0-9]', '', t_title.lower())
                                            clean_target_title = re.sub(r'[^a-z0-9]', '', clean_title.lower())
                                            core_item = re.sub(r'(feat|with|remix|mono|single|version|radio|edit|album).*', '', clean_t_title)
                                            core_target = re.sub(r'(feat|with|remix|mono|single|version|radio|edit|album).*', '', clean_target_title)
                                            
                                            title_match = (clean_t_title == clean_target_title) or (core_item == core_target and core_item)
                                            if title_match and t.get("isExplicit", False) == target_explicit:
                                                other_versions_songs.append({
                                                    'videoId': t.get('videoId'),
                                                    'title': t_title,
                                                    'isExplicit': t.get('isExplicit', False),
                                                    'artists': song_item.get('artists', [])
                                                })
                except Exception as ove:
                    sys.stdout.write(f"\n[WARNING] other_versions lookup failed: {ove}\n")
                    sys.stdout.flush()

                results = []
                if other_versions_songs:
                    results = other_versions_songs
                    state.log(f"[Counterpart Search] Found official counterpart in other_versions: '{results[0].get('title')}'")
                else:
                    state.log(f"[Counterpart Search] Falling back to query search: '{search_query}'")
                    results = ytm.search(search_query, filter="songs")

                if not results:
                    state.log(f"[Counterpart Search] No results found for query: '{search_query}'")
                    continue
                    
                enqueued = False
                for song in results[:8]:
                    video_id = song.get('videoId')
                    if not video_id:
                        continue
                        
                    song_title = song.get('title', '')
                    song_explicit = song.get('isExplicit', False)
                    artists_list = song.get('artists', [])
                    song_artist = ", ".join([a.get('name', '') for a in artists_list])
                    
                    # Skip karaoke, tribute, instrumental, sped-up, slowed versions always
                    bad_keywords = ['karaoke', 'tribute', 'instrumental', 'backing track',
                                    'originally performed', 'cover version', 'sped-up',
                                    'sped up', 'slowed', 'nightcore', 'pitched up', 'pitched down']
                    if any(k in song_title.lower() for k in bad_keywords):
                        continue

                    # Verify candidate belongs to the target category
                    song_title_lower = song_title.lower()
                    candidate_radio = 'radio edit' in song_title_lower or 'radio version' in song_title_lower

                    # Duration-based radio edit detection:
                    # Radio edits strip skits/intros so they are typically 20-90s shorter.
                    # If candidate has no "radio edit" label but is meaningfully shorter,
                    # treat it as a radio edit candidate when searching for radioedit category.
                    candidate_duration_seconds = song.get('duration_seconds', 0)
                    duration_gap = 0
                    if original_duration_seconds > 0 and candidate_duration_seconds > 0:
                        duration_gap = original_duration_seconds - candidate_duration_seconds
                    is_shorter_unlabeled_radio = (
                        target_cat == 'radioedit'
                        and not candidate_radio
                        and 20 <= duration_gap <= 90
                        and not song_explicit
                    )

                    if target_cat == 'radioedit':
                        if not candidate_radio and not is_shorter_unlabeled_radio:
                            continue
                    elif target_cat == 'explicit':
                        if not song_explicit:
                            continue
                    elif target_cat == 'clean':
                        if song_explicit or candidate_radio:
                            continue

                    why_not = counterpart_rejection(title, song_title, original_duration_seconds, candidate_duration_seconds,
                                                    radio_involved=(is_radio or target_cat == 'radioedit'))
                    if why_not:
                        state.log(f"[Counterpart Search] Not a counterpart: '{song_artist} - {song_title}' - {why_not}")
                        continue

                    # Match normalized key (without version details)
                    existing_key = vm._normalize_track_key(f"{artist} - {clean_title}")
                    candidate_title_clean = re.sub(r'\((?:explicit|clean|radio edit|radio version)\)', '', song_title, flags=re.I).strip()
                    candidate_key = vm._normalize_track_key(f"{song_artist} - {candidate_title_clean}")
                    
                    # Get base keys (without the version suffixes)
                    existing_base = existing_key.split('_')[0] + '_' + existing_key.split('_')[1] if len(existing_key.split('_')) >= 2 else existing_key
                    candidate_base = candidate_key.split('_')[0] + '_' + candidate_key.split('_')[1] if len(candidate_key.split('_')) >= 2 else candidate_key
                    
                    if existing_base == candidate_base:
                        # Check duplicate with specific version category
                        duplicate = False
                        if os.path.exists(CSV_BLUEPRINT):
                            with vm._csv_lock:
                                with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                                    reader = csv.DictReader(f)
                                    candidate_track_key = vm._normalize_track_key(f"{song_artist} - {song_title}", explicit_val=song_explicit)
                                    
                                    def extract_vid(url_str):
                                        if not url_str:
                                            return ""
                                        vid_match = re.search(r'(?:v=|\/)([\w-]{11})(?:&|\?|$)', url_str)
                                        return vid_match.group(1) if vid_match else ""
                                        
                                    candidate_vid = video_id
                                    for row in reader:
                                        existing_name = row.get('Track Name')
                                        if existing_name:
                                            ex_expl = (row.get('Explicit') or '').strip().lower() in ['true', '1']
                                            existing_track_key = vm._normalize_track_key(existing_name, explicit_val=ex_expl)
                                            if existing_track_key == candidate_track_key:
                                                duplicate = True
                                                break
                                                
                                        # Also match by duplicate video ID in Source_URL
                                        existing_url = row.get('Source_URL')
                                        if existing_url and candidate_vid:
                                            if extract_vid(existing_url) == candidate_vid:
                                                duplicate = True
                                                state.log(f"[Counterpart Search] Video ID {candidate_vid} already in library as '{existing_name}'. Skipping counterpart.")
                                                break
                                                
                        if duplicate:
                            state.log(f"[Counterpart Search] Counterpart ({target_cat}) '{song_artist} - {song_title}' is already in the library. Skipping.")
                            break
                            
                        # Queue download!
                        counterpart_url = f"https://music.youtube.com/watch?v={video_id}"
                        state.log(f"[Counterpart Auto-Link] Found counterpart ({target_cat}): '{song_artist} - {song_title}' ({counterpart_url}) -> Enqueuing download...")
                        counterpart_task = {
                            'url': counterpart_url,
                            'type': 'ingest',
                            'target': target_folder,
                            'title': song_title,
                            'artist': song_artist,
                            'explicit': song_explicit,
                            'auto_linked': True,
                            # Download THIS release (picked by its explicit badge), not whatever a
                            # Soulseek search for artist+title returns - otherwise the "clean" or
                            # "radio edit" counterpart could arrive as the explicit album cut (2026-09-25).
                            'exact_source': True,
                        }
                        if song.get('duration_seconds'):
                            counterpart_task['expected_seconds'] = float(song['duration_seconds'])
                        state.url_queue.put(counterpart_task)
                        state.update_count()
                        enqueued = True
                        break
                        
                if not enqueued:
                    state.log(f"[Counterpart Search] No matching {target_cat} version found for '{artist} - {title}'")
                    
        except Exception as e:
            sys.stdout.write(f"\n[ERROR] Counterpart search failed: {e}\n")
            sys.stdout.flush()
            
    threading.Thread(target=bg_search, daemon=True).start()

def vault_worker():
    while not state.stop_event.is_set():
        try:
            task = state.vault_queue.get(timeout=1)
        except queue.Empty:
            continue
        
        try:
            clean_name = os.path.basename(task['path'])
            dest = task.get('target') or "Eras"
            job_id = task.get('job_id')

            # Broker-delegated jobs (Windows downloaded this because the VM's
            # own Soulseek attempt missed) used to also call store_track()
            # here, vaulting a full local copy on Windows in addition to the
            # copy the VM vaults after transport below - two independent
            # catalog rows (and two independent git commits) for the exact
            # same track, every single time a job got delegated. Confirmed
            # live 2026-09-03: this is what was producing a real merge
            # conflict on almost every delegated download. Windows's job for
            # a delegated download is download + master + transport only -
            # the VM is the one and only machine that vaults it.
            import platform
            if job_id and platform.system() == "Windows":
                state.log(f"Phase 3: Transporting [{clean_name}] to VM for vaulting (delegated job)")
                t = threading.Thread(
                    target=wait_and_scp,
                    args=(task['path'], clean_name, job_id, task.get('target', 'Music'), task.get('overwrite', False), task.get('meta', {}).get('url', '')),
                    kwargs={'cleanup_dir': os.path.dirname(task['path'])},
                    daemon=True
                )
                t.start()
                state.increment_completed()
                continue

            vm = VaultManager()
            state.log(f"Phase 3: Vaulting [{clean_name}] to -> {dest}")

            status, message = vm.store_track(task['path'], task['meta'], task['task_id'], task.get('target'), overwrite=task.get('overwrite', False))
            
            if status:
                state.log(f"Complete: {clean_name}")
                state.increment_completed()

                # store_track() deletes its own staging directory as its
                # last step on success, so task['path'] (the original
                # staging location) no longer exists by the time this runs -
                # use the real vaulted G: drive path it hands back instead
                # (falls back to task['path'] only if that attribute is
                # somehow missing, so this can't behave worse than before).
                transport_path = getattr(vm, 'last_vaulted_path', None) or task['path']

                job_id = task.get('job_id')
                if job_id:
                    t = threading.Thread(
                        target=wait_and_scp,
                        args=(transport_path, clean_name, job_id, task.get('target', 'Music'), task.get('overwrite', False), task.get('meta', {}).get('url', '')),
                        daemon=True
                    )
                    t.start()
                elif platform.system() == "Windows":
                    # Direct (non-delegated) download vaulted right here on Windows: the file is already
                    # on Google Drive (the VM's 3-minute rclone pull copies it) and the catalog row goes
                    # out via this vault's own git push (which the VM's app merges). Handing it to the VM
                    # as well ('local_upload') made the VM vault it a SECOND time with its own catalog row
                    # - two rows per download, the VM's without a pool (found 2026-09-25).
                    state.log(f"[Vault] '{clean_name}' saved on Google Drive - the VM picks it up via Drive sync + git.")
                else:
                    # LOCAL-ONLY Additions directly from ultimate.fmpmediagroup.com
                    # They MUST reach the VM as well. We pass 'local_upload' to bypass the 400 error on the VM.
                    t = threading.Thread(
                        target=wait_and_scp,
                        args=(transport_path, clean_name, "local_upload", task.get('target', 'Music'), task.get('overwrite', False), task.get('meta', {}).get('url', '')),
                        daemon=True
                    )
                    t.start()
                    
                # Trigger immediate background rclone sync to Google Drive on Linux
                import platform
                if platform.system() != "Windows":
                    try:
                        subprocess.Popen(
                            ["rclone", "copy", "/home/ubuntu/music/", "gdrive:FMP MUSIC/BASE/MUSIC", 
                             "--ignore-existing", "--transfers=4", "--checkers=8", 
                             "--exclude", "/staging/**", "--exclude", "/Shows_to_delete/**"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True
                        )
                        state.log("[SYNC] Triggered instant Google Drive background sync.")
                    except Exception as sync_err:
                        logging.error(f"Failed to trigger instant rclone sync: {sync_err}")

                # Check for explicit/clean counterpart to auto-download
                if not task.get('auto_linked') and not task.get('meta', {}).get('auto_linked'):
                    artist = task['meta'].get('artist')
                    title = task['meta'].get('title')
                    is_explicit = str(task['meta'].get('explicit', 'False')).strip().lower() in ['true', '1']
                    target_folder = task.get('target')
                    # Derive original duration from cue points (cue_out - cue_in), fallback to 0
                    cue_in = task['meta'].get('cue_in', 0) or 0
                    cue_out = task['meta'].get('cue_out', 0) or 0
                    original_duration_seconds = int((cue_out - cue_in) / 1000) if cue_out > cue_in else 0
                    if artist and title:
                        trigger_single_song_counterpart_search(artist, title, is_explicit, target_folder, original_duration_seconds)
            elif "Duplicate" in message:
                state.log(f"[SKIPPED] Duplicate Track: {clean_name}")
            else:
                state.log(f"[VAULT ERROR] {clean_name}: {message}")
                logging.error(f"Vaulting failure for {clean_name}: {message}")
        except Exception as e:
            state.log(f"[VAULT EXCEPTION] {e}")
            logging.error(f"Vaulting exception: {e}")
        finally:
            state.vault_queue.task_done()
            state.update_count()

def iheart_poller_worker():
    import urllib.request
    import json
    import urllib.error
    import csv
    import re
    from datetime import datetime
    from config import IHEART_STATION_ID, IHEART_POLL_INTERVAL, IHEART_CHURCH_FOLDER
    
    state.log("[iHeart Sync] Listening for missing tracks")
    
    last_track_id = None
    last_track_key = None
    
    api_url = f"https://us.api.iheart.com/api/v3/live-meta/stream/{IHEART_STATION_ID}/currentTrackMeta?defaultMetadata=true"
    
    while not state.stop_event.is_set():
        try:
            req = urllib.request.Request(
                api_url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    track_id = data.get('trackId')
                    artist = data.get('artist', '').strip()
                    title = data.get('title', '').strip()
                    album = data.get('album', '').strip()
                    
                    # Create a unique key for the currently playing track
                    current_key = f"{artist} - {title}".lower().strip()
                    
                    if current_key and (track_id != last_track_id or current_key != last_track_key):
                        last_track_id = track_id
                        last_track_key = current_key
                        
                        if not artist or not title or artist.lower() in ["unknown artist", ""] or title.lower() in ["unknown title", ""]:
                            continue
                            
                        logging.info(f"[iHeart Sync] Now Playing on WVAZ V103: \"{title}\" by {artist}")
                        
                        # 1. Determine Target Routing (Gospel/Church, via is_inspirational_track's
                        # keyword/artist-list + MusicBrainz genre fallback). The old Sunday
                        # 6am-12pm time-window fallback is gone -- it only ever covered 1 of 7
                        # days and is now superseded by the MusicBrainz genre signal, which
                        # covers every day.
                        is_sunday_church = False

                        if is_inspirational_track(artist, title, album):
                            is_sunday_church = True
                            logging.info(f"[iHeart Sync] Inspirational track matched -> Routing to Gospel/Church.")

                        if is_sunday_church:
                            target_override = IHEART_CHURCH_FOLDER
                            logging.info(f"[iHeart Sync] Routing \"{title}\" to Church folder -> {IHEART_CHURCH_FOLDER}")
                        else:
                            target_override = ""
                            
                        # 2. Check for duplicates in library using smart matching
                        from modules.storage import VaultManager
                        vm = VaultManager()
                        
                        is_duplicate = False
                        duplicate_reason = ""
                        
                        from config import CSV_BLUEPRINT
                        if os.path.exists(CSV_BLUEPRINT):
                            with vm._csv_lock:
                                try:
                                    import csv
                                    with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                                        reader = csv.DictReader(f)
                                        for row in reader:
                                            existing_name = row.get('Track Name')
                                            if existing_name:
                                                is_dup, reason = is_smart_duplicate(existing_name, artist, title, vm=vm)
                                                if is_dup:
                                                    is_duplicate = True
                                                    duplicate_reason = reason
                                                    break
                                except Exception as e:
                                    logging.error(f"Error checking CSV in iHeart poller: {e}")
                                    
                        if not is_duplicate:
                            from config import MUSIC_DIR
                            g_drive_base = MUSIC_DIR
                            if os.path.exists(g_drive_base):
                                search_folders = vm.era_folders + ["Slow Jams", "Gospel", "Deep Cuts", "Blues", "Music"]
                                for folder in search_folders:
                                    for subf in ["", "Clean", "Explicit", "Radio Edit"]:
                                        if subf:
                                            folder_path = os.path.join(g_drive_base, folder, subf)
                                        else:
                                            folder_path = os.path.join(g_drive_base, folder)
                                            
                                        if os.path.exists(folder_path):
                                            try:
                                                for f in os.listdir(folder_path):
                                                    f_lower = f.lower()
                                                    if f_lower.endswith((".mp3", ".flac", ".m4a")):
                                                        if f_lower.endswith(".flac") or f_lower.endswith(".m4a"):
                                                            f_name_without_ext = f[:-5]
                                                        else:
                                                            f_name_without_ext = f[:-4]
                                                            
                                                        is_dup, reason = is_smart_duplicate(f_name_without_ext, artist, title)
                                                        if is_dup:
                                                            is_duplicate = True
                                                            duplicate_reason = f"Already exists on G Drive: {folder}/{subf}/{f} ({reason})" if subf else f"Already exists on G Drive: {folder}/{f} ({reason})"
                                                            break
                                            except Exception as scan_err:
                                                logging.error(f"Error scanning folder {folder_path}: {scan_err}")
                                        if is_duplicate:
                                            break
                                    if is_duplicate:
                                        break
                                        
                        if is_duplicate:
                            logging.info(f"[iHeart Sync] Skipped (Duplicate): \"{title}\" by {artist} ({duplicate_reason})")
                        else:
                            query_clean = re.sub(r'\[.*?\]', '', f"{artist} - {title}").strip()
                            query_clean = re.sub(r'\s+', ' ', query_clean)
                            
                            # Check if already in pending_iheart_queue, download_queue, or rejected
                            already_pending = False
                            with state.lock:
                                for item in state.pending_iheart_queue:
                                    if item.get('url', '').lower() == query_clean.lower():
                                        already_pending = True
                                        break
                                for item in state.url_queue.get_all():
                                    if item.get('url', '').lower() == query_clean.lower():
                                        already_pending = True
                                        break
                                for rej_url in state.rejected_iheart:
                                    if rej_url.lower() == query_clean.lower():
                                        already_pending = True
                                        break
                                        
                            if already_pending:
                                continue
                                
                            state.log(f"[iHeart Sync] Missing Track! Staging in Discoveries: \"{query_clean}\"")
                            with state.lock:
                                state.pending_iheart_queue.append({
                                    'url': query_clean,
                                    'target': target_override,
                                    'artist': artist,
                                    'title': title,
                                    'timestamp': datetime.now().strftime("%I:%M %p")
                                })
                                state.save_pending()
                            state.update_count()
                            
        except urllib.error.URLError as e:
            logging.debug(f"iHeart API Poll network error: {e}")
        except Exception as e:
            logging.error(f"Unexpected error in iheart_poller_worker: {e}")
            
        time.sleep(IHEART_POLL_INTERVAL)

def git_sync_worker():
    while not state.stop_event.is_set():
        # Previously gated to Linux-only (if os.name != 'nt') with no comment
        # explaining why - now that downloader_worker/vault_worker run on both
        # platforms, both sides can go stale between their own vault-triggered
        # pulls, so both get the periodic pull.
        try:
            with git_operation_lock(timeout=60):
                res_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True)
                branch_name = res_branch.stdout.strip()
                # Merge, not rebase+autostash - see config.git_safe_pull's
                # docstring for the 2026-09-03 incident this replaces.
                git_safe_pull(branch_name)
            state.log("[SYSTEM] Periodic GitHub database pull successful.")
        except Exception as e:
            logging.warning(f"Periodic git pull failed with exception: {e}")
        # Wait 15 minutes (900 seconds) in 10-second intervals to check stop_event
        for _ in range(90):
            if state.stop_event.is_set():
                break
            time.sleep(10)


def wait_and_scp(filepath, filename, job_id, target, overwrite, source_url, cleanup_dir=None):
    import subprocess
    import urllib.request
    import base64
    import json
    import ssl
    import time

    state.log(f"[Broker] Transporting '{filename}' to Oracle VM...")

    # Wait for file to stabilize if it was just written
    last_size = -1
    for _ in range(45):
        try:
            curr_size = os.path.getsize(filepath)
        except Exception:
            curr_size = -2
        if curr_size == last_size and curr_size > 0:
            break
        last_size = curr_size
        time.sleep(2)

    remote_host = os.getenv("REMOTE_VM_IP", "149.130.219.114")
    remote_user = "ubuntu"
    remote_dest_path = f"/home/ubuntu/FMP-Radio/staging/{filename}"
    ssh_key_path = "C:/Users/chito/.ssh/id_ed25519"
    
    mkdir_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10", "-i", ssh_key_path,
        f"{remote_user}@{remote_host}",
        "mkdir -p /home/ubuntu/FMP-Radio/staging"
    ]
    try:
        subprocess.run(mkdir_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15)
    except Exception as e:
        state.log(f"[Broker Error] Remote staging folder creation failed: {e}")

    scp_cmd = [
        "scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10", "-i", ssh_key_path,
        filepath,
        f"{remote_user}@{remote_host}:{remote_dest_path}"
    ]
    try:
        subprocess.run(scp_cmd, check=True, timeout=90)
        state.log(f"[Broker] SUCCESS: Copied '{filename}' to remote VM staging!")
    except Exception as e:
        state.log(f"[Broker Error] SCP transfer failed: {e}")
        return

    state.log(f"[Broker] Notifying VM to complete job ID {job_id}...")
    complete_url = f"https://{remote_host}/api/pull_jobs/complete"
    pull_jobs_url = f"https://{remote_host}/api/pull_jobs"
    payload = {
        "item_id": job_id,
        "staging_path": remote_dest_path,
        "target": target,
        "overwrite": overwrite,
        "source_url": source_url
    }
    auth_b64 = _vm_basic_auth_b64()
    ssl_ctx = ssl.create_default_context()
    # check_hostname=False only (connects by IP against a domain cert) -
    # verify_mode stays CERT_REQUIRED, see _acknowledge_remote_job() above for why.
    ssl_ctx.check_hostname = False

    def _job_already_vaulted():
        # Confirmed live 2026-09-21: nginx can give up on this request (504 to us) and hand back what
        # looks like total failure, while the VM's own handler keeps running and actually finishes the
        # vault a few seconds later. Before treating a failed attempt as a real failure, ask whether the
        # job is still sitting in the queue - if the VM already removed it, the vault already succeeded.
        if not isinstance(job_id, int):
            return False  # not a real pull_jobs id (e.g. "local_upload") - nothing to check against
        try:
            req = urllib.request.Request(pull_jobs_url, headers={
                'Authorization': f'Basic {auth_b64}', 'Host': 'ultimate.fmpmediagroup.com',
                'X-Internal-Key': INTERNAL_API_KEY}, method='GET')
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as res:
                jobs = json.loads(res.read().decode('utf-8'))
            return not any(j.get('id') == job_id for j in jobs)
        except Exception:
            return False  # can't tell either way - don't assume success

    vaulted = False
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                complete_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Basic {auth_b64}',
                    'Host': 'ultimate.fmpmediagroup.com',
                    'X-Internal-Key': INTERNAL_API_KEY
                },
                method='POST'
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=300) as res:
                response_data = json.loads(res.read().decode('utf-8'))
            if response_data.get('status') == 'ok':
                state.log(f"[Broker] SUCCESS: Remote VM vaulted {filename}.")
                vaulted = True
            else:
                state.log(f"[Broker Error] Remote VM failed to vault: {response_data.get('message')}")
            break  # got a real answer either way - retrying would just duplicate-vault it
        except Exception as e:
            state.log(f"[Broker Error] Completion notification attempt {attempt}/3 failed ({e}) - "
                      f"checking whether the VM finished it anyway before retrying...")
            if _job_already_vaulted():
                state.log(f"[Broker] The VM had already vaulted {filename} despite the failed response - no retry needed.")
                vaulted = True
                break
            if attempt < 3:
                time.sleep(10)

    if vaulted:
        # Only cleaned up on confirmed remote success - if the VM failed to vault, this local staging
        # copy stays put since it's the only copy left (the caller skipped its own local vault for
        # delegated jobs, see vault_worker).
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
    else:
        state.log(f"[Broker Error] Could not confirm '{filename}' was vaulted after 3 attempts. It is still "
                  f"safe at {remote_dest_path} on the VM, but needs manual attention (check VM connectivity, "
                  f"then re-run the completion step or vault it by hand).")

def poll_jobs_worker():
    import urllib.request
    import json
    import base64
    import ssl
    import time
    
    remote_host = os.getenv("REMOTE_VM_IP", "149.130.219.114")
    poll_url = f"https://{remote_host}/api/pull_jobs"
    
    ssl_ctx = ssl.create_default_context()
    # check_hostname=False only, see _acknowledge_remote_job() above for why.
    ssl_ctx.check_hostname = False

    state.log(f"[Broker] Outbound polling loop started against {poll_url}")
    heartbeat_fail_streak = 0  # only log on the transition in/out of failing, not every ~10s tick

    while not state.stop_event.is_set():
        try:
            auth_b64 = _vm_basic_auth_b64()

            # Send local status heartbeat to remote VM - this is what the VM dashboard's "workstation" panel
            # reads; if it goes quiet the dashboard shows "WORKSTATION OFFLINE" even though this loop (and
            # downloading) is running fine, so a failing streak is worth a visible log line rather than
            # silently swallowing it forever.
            remote_status_url = f"https://{remote_host}/api/workstation/status"
            req_status = urllib.request.Request(
                remote_status_url,
                data=json.dumps(state.get_snapshot()).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Basic {auth_b64}',
                    'Host': 'ultimate.fmpmediagroup.com'
                },
                method='POST'
            )
            try:
                with urllib.request.urlopen(req_status, context=ssl_ctx, timeout=5) as remote_res:
                    pass
                if heartbeat_fail_streak >= 3:
                    state.log("[Broker] Heartbeat to VM recovered.")
                heartbeat_fail_streak = 0
            except Exception as e:
                heartbeat_fail_streak += 1
                if heartbeat_fail_streak == 3:
                    state.log(f"[Broker Error] Heartbeat to VM has failed {heartbeat_fail_streak} times in a "
                              f"row ({e}) - the VM dashboard may show WORKSTATION OFFLINE until this recovers.")

            # Poll for jobs
            req = urllib.request.Request(
                poll_url,
                headers={
                    'Authorization': f'Basic {auth_b64}',
                    'Host': 'ultimate.fmpmediagroup.com',
                    'X-Internal-Key': INTERNAL_API_KEY
                },
                method='GET'
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as res:
                jobs = json.loads(res.read().decode('utf-8'))
                
                for job in jobs:
                    job_id = job.get('id')
                    
                    # Checking url_queue alone isn't enough - an item is
                    # popped off it the instant downloader_worker starts
                    # processing it, so a job that's actively downloading
                    # (which can easily take longer than this loop's ~10s
                    # poll interval) looks "not in queue" and gets re-added
                    # and reprocessed in parallel with itself, every cycle,
                    # for as long as it takes to finish. claimed_broker_job_ids
                    # tracks every job id ever accepted, for the life of the
                    # process, so once claimed it's never re-added regardless
                    # of whether it's queued, in progress, or done.
                    already_in_queue = job_id in state.claimed_broker_job_ids
                    if not already_in_queue:
                        for existing_item in state.url_queue.get_all():
                            if existing_item.get('job_id') == job_id or existing_item.get('url') == job.get('url'):
                                already_in_queue = True
                                break

                    if not already_in_queue:
                        state.log(f"[Broker] Found new pending job: ID {job_id} - '{job.get('artist')} - {job.get('title')}'")

                        local_payload = {
                            "job_id": job_id,
                            "url": job['url'],
                            "target": job.get('target', ''),
                            "title": job.get('title'),
                            "artist": job.get('artist'),
                            "auto_linked": job.get('auto_linked', False),
                            "explicit": job.get('explicit'),
                            "is_radio": job.get('is_radio'),
                            "overwrite": job.get('overwrite', False),
                            "type": "ingest"
                        }

                        state.claimed_broker_job_ids.add(job_id)
                        state.url_queue.put(local_payload)
                            
        except Exception as e:
            pass
            
        for _ in range(10):
            if state.stop_event.is_set(): break
            time.sleep(1)

def poll_deletions_worker():
    import urllib.request
    import json
    import base64
    import ssl
    import csv
    import time
    
    remote_host = os.getenv("REMOTE_VM_IP", "149.130.219.114")
    poll_url = f"https://{remote_host}/api/deletions/poll"
    
    ctx = ssl.create_default_context()
    # check_hostname=False only, see _acknowledge_remote_job() above for why.
    ctx.check_hostname = False
    
    auth_b64 = _vm_basic_auth_b64()
    
    local_music_dir = os.getenv("WINDOWS_AUDIO_PATH", "G:/My Drive/FMP MUSIC/BASE/MUSIC")
    from config import CSV_BLUEPRINT
    
    while not state.stop_event.is_set():
        try:
            req = urllib.request.Request(poll_url, method='GET')
            req.add_header("Authorization", f"Basic {auth_b64}")
            req.add_header("Host", "ultimate.fmpmediagroup.com")
            req.add_header("X-Internal-Key", INTERNAL_API_KEY)
            
            with urllib.request.urlopen(req, context=ctx, timeout=10) as res:
                deletions = json.loads(res.read().decode('utf-8'))
                
            if deletions:
                state.log(f"[Broker] Polled {len(deletions)} pending deletions from remote VM.")
                csv_cleaned = False
                
                for file_path in deletions:
                    phys_path = os.path.join(local_music_dir, file_path.replace("Music/", "").replace("Music\\", ""))
                    if os.path.exists(phys_path):
                        try:
                            os.remove(phys_path)
                            state.log(f"[Broker] Deleted local physical file: {phys_path}")
                        except Exception as e:
                            pass
                        
                    if os.path.exists(CSV_BLUEPRINT):
                        try:
                            with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                                reader = csv.DictReader(f)
                                fieldnames = reader.fieldnames
                                rows = []
                                for r in reader:
                                    # Was matching if file_path appeared ANYWHERE in the
                                    # stringified row (any column) - a row got dropped
                                    # whenever the target path happened to be a substring
                                    # of another row's Source_URL/Track Name/etc. Match
                                    # the File Path column specifically instead.
                                    if r.get('File Path', '').strip() != file_path.strip():
                                        rows.append(r)
                            
                            with open(CSV_BLUEPRINT, 'w', encoding='utf-8', newline='') as f:
                                writer = csv.DictWriter(f, fieldnames=fieldnames)
                                writer.writeheader()
                                writer.writerows(rows)
                                
                            state.log(f"[Broker] Purged {file_path} from local CSV.")
                            csv_cleaned = True
                        except Exception:
                            pass
                            
                if csv_cleaned:
                    try:
                        import subprocess
                        # No hardcoded cwd (relies on the process's own working
                        # directory, same as every other git-sync site in this
                        # file) - the old hardcoded Windows path silently made
                        # this a no-op on the VM. Pathspec-restricted commit +
                        # dynamic branch + --autostash, matching the fix already
                        # applied to background_rename_task/sync_library_db.py.
                        with git_operation_lock(timeout=60):
                            res_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True)
                            branch_name = res_branch.stdout.strip()
                            subprocess.run(["git", "add", "configs/fmp_data_7718.csv"], check=True, capture_output=True)
                            subprocess.run(["git", "commit", "configs/fmp_data_7718.csv", "--no-verify", "-m", "Broker auto-sync: Purge deleted tracks"], check=True, capture_output=True)
                            # Merge, not rebase+autostash - see config.git_safe_pull's
                            # docstring for the 2026-09-03 incident this replaces.
                            git_safe_pull(branch_name)
                            subprocess.run(["git", "push", "origin", branch_name], check=True, capture_output=True)
                    except Exception as git_err:
                        state.log(f"[Broker Error] Git sync of deletion purge failed: {git_err}")
                        
        except Exception:
            pass
            
        for _ in range(30):
            if state.stop_event.is_set(): break
            time.sleep(1)

def start_engines():

    state.stop_event.clear()
    
    if not state.boot_cleared:
        state.log("System Hard Reset: Purging Staging...")
        if os.path.exists(STAGING_DIR):
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        os.makedirs(STAGING_DIR, exist_ok=True)
        
        # Purge stale git lock on boot if it exists
        lock_file = os.path.join(os.path.dirname(CSV_BLUEPRINT), "git_commit.lock")
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                state.log("Purged stale git_commit.lock file on startup.")
            except Exception as e:
                logging.warning(f"Could not purge stale git_commit.lock: {e}")
        
        if os.path.exists(CSV_BLUEPRINT):
            backup_path = os.path.join(BACKUP_DIR, "fmp_data_7718_backup.csv")
            try:
                shutil.copy2(CSV_BLUEPRINT, backup_path)
                state.log("Database backup verified.")
            except Exception as e:
                state.log(f"!! Backup Failed: {e}")
                logging.error(f"CSV Backup routine failed: {e}")
        
        state.boot_cleared = True

    # Runs on both platforms now: the VM needs its own engine loop to attempt
    # Soulseek (download_track's non-Windows branch delegates to scrape_queue on
    # a miss instead of scraping YouTube locally - see Transporter.download_track).
    from config import DOWNLOAD_CONCURRENCY
    if not any(t.is_alive() for t in state.downloader_threads):
        state.downloader_threads = []
        for _ in range(DOWNLOAD_CONCURRENCY):
            t = threading.Thread(target=downloader_worker, daemon=True)
            t.start()
            state.downloader_threads.append(t)
    if not state.vault_thread or not state.vault_thread.is_alive():
        state.vault_thread = threading.Thread(target=vault_worker, daemon=True)
        state.vault_thread.start()

    # Start the new polling and syncing threads
    # Windows-only: this is the scrape worker pulling jobs FROM the VM. Starting
    # it on the VM itself would have it poll its own public endpoint for the same
    # scrape_queue jobs its own engine just delegated - an infinite self-loop
    # (re-queue -> retry Soulseek -> miss -> re-delegate -> re-discover -> ...).
    import platform
    if platform.system() == "Windows" and (not hasattr(state, 'poll_jobs_thread') or not state.poll_jobs_thread or not state.poll_jobs_thread.is_alive()):
        state.poll_jobs_thread = threading.Thread(target=poll_jobs_worker, daemon=True)
        state.poll_jobs_thread.start()
        
    # poll_deletions_worker deletes from local_music_dir (WINDOWS_AUDIO_PATH,
    # a Windows path) - same Windows-only reasoning as poll_jobs_worker above.
    # Previously started unconditionally on both platforms; on the VM it would
    # poll its own public endpoint and always no-op on the physical-delete step.
    if platform.system() == "Windows" and (not hasattr(state, 'poll_deletions_thread') or not state.poll_deletions_thread or not state.poll_deletions_thread.is_alive()):
        state.poll_deletions_thread = threading.Thread(target=poll_deletions_worker, daemon=True)
        state.poll_deletions_thread.start()

    # Start the periodic git sync puller
    if not hasattr(state, 'gitsync_thread') or not state.gitsync_thread or not state.gitsync_thread.is_alive():
        state.gitsync_thread = threading.Thread(target=git_sync_worker, daemon=True)
        state.gitsync_thread.start()
        
    # Start the iHeart Sync engine if enabled
    from config import IHEART_SYNC_ENABLED
    if IHEART_SYNC_ENABLED:
        if not hasattr(state, 'iheart_thread') or not state.iheart_thread or not state.iheart_thread.is_alive():
            state.iheart_thread = threading.Thread(target=iheart_poller_worker, daemon=True)
            state.iheart_thread.start()

def get_rclone_path():
    import platform
    import shutil
    if platform.system() == "Windows":
        path = os.path.join(BASE_DIR, "rclone.exe")
        if os.path.exists(path):
            return path
    resolved = shutil.which("rclone")
    if resolved:
        return resolved
    return "rclone"

# --- WEB ROUTES ---
@app.route('/')
def index(): return render_template('index.html', internal_key=INTERNAL_API_KEY)
@app.route('/api/status')
def status(): return jsonify(state.get_snapshot())

@app.route('/api/workstation/status', methods=['POST'])
def update_workstation_status():
    payload = request.json
    if not payload:
        return jsonify({"status": "error", "message": "Missing payload"}), 400
    state.workstation_status = payload
    return jsonify({"status": "ok"})


@app.route('/library-eraser')
def library_eraser(): return render_template('library_eraser.html', internal_key=INTERNAL_API_KEY)

@app.route('/api/shows')
def get_shows():
    try:
        rclone_path = get_rclone_path()
        cmd = [rclone_path, "lsf", "citrus3:/Shows/", "--dirs-only"]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True)
        shows = [line.strip().rstrip('/') for line in result.stdout.split('\n') if line.strip()]
        return jsonify(shows)
    except Exception as e:
        state.log(f"[ERROR] Could not fetch shows via Rclone: {e}")
        logging.error(f"Rclone Shows fetch error: {e}")
        return jsonify([])

@app.route('/api/new_show', methods=['POST'])
def new_show():
    data = request.get_json(silent=True) or {}
    show_name = data.get('show_name')
    if show_name:
        safe_name = "".join(c for c in show_name if c.isalnum() or c in (' ', '_', '-')).strip()
        try:
            rclone_path = get_rclone_path()
            cmd = [rclone_path, "mkdir", f"citrus3:/Shows/{safe_name}"]
            subprocess.run(cmd, check=True)
        except Exception as e:
            state.log(f"[ERROR] Failed to create remote show folder via rclone: {e}")
            logging.error(f"Remote folder creation failed for {safe_name}: {e}")
    return jsonify({"status": "ok"})

def replace_show_in_path(path_val, old_name, new_name):
    if not path_val:
        return path_val
    norm_path = path_val.replace('\\', '/')
    parts = norm_path.split('/')
    try:
        shows_idx = parts.index('Shows')
        if shows_idx < len(parts) - 1 and parts[shows_idx + 1] == old_name:
            parts[shows_idx + 1] = new_name
            sep = '\\' if '\\' in path_val else '/'
            return sep.join(parts)
    except ValueError:
        pass
    return path_val

def background_rename_task(old_name, new_name):
    import csv
    try:
        # 1. Run Rclone moveto
        rclone_path = get_rclone_path()
        state.log(f"[RENAME] Starting remote Citrus3 FTP folder move: Shows/{old_name} -> Shows/{new_name}")
        cmd = [rclone_path, "moveto", f"citrus3:/Shows/{old_name}", f"citrus3:/Shows/{new_name}"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if res.returncode != 0:
            state.log(f"[WARNING] Remote move returned non-zero code. Folder may not exist on FTP yet. Output: {res.stderr.strip()}")
        else:
            state.log(f"[RENAME] Remote Citrus3 FTP folder move completed successfully.")

        # 1b. Rename folder on remote Google Drive if on Linux/VM
        if os.name != "nt":
            state.log(f"[RENAME] Starting remote Google Drive folder move: Shows/{old_name} -> Shows/{new_name}")
            cmd_gdrive = [rclone_path, "moveto", f"gdrive:FMP MUSIC/BASE/MUSIC/Shows/{old_name}", f"gdrive:FMP MUSIC/BASE/MUSIC/Shows/{new_name}"]
            res_gd = subprocess.run(cmd_gdrive, capture_output=True, text=True, encoding='utf-8')
            if res_gd.returncode != 0:
                state.log(f"[WARNING] Remote Google Drive move returned non-zero code. Output: {res_gd.stderr.strip()}")
            else:
                state.log(f"[RENAME] Remote Google Drive folder move completed successfully.")

        # 2. Backup configs/fmp_data_7718.csv
        state.log(f"[RENAME] Creating database backup before rewrite...")
        backup_filename = f"fmp_data_7718_rename_{old_name}_to_{new_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        backup_path = os.path.join(BACKUP_DIR, backup_filename)
        
        vm = VaultManager()
        updated_count = 0
        with vm._csv_lock:
            if os.path.exists(CSV_BLUEPRINT):
                shutil.copy2(CSV_BLUEPRINT, backup_path)
                
                # 3. Rewrite matching rows
                rows = []
                fieldnames = []
                with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        path_val = row.get('File Path')
                        if path_val:
                            new_path_val = replace_show_in_path(path_val, old_name, new_name)
                            if new_path_val != path_val:
                                row['File Path'] = new_path_val
                                updated_count += 1
                        rows.append(row)
                
                if updated_count > 0:
                    with open(CSV_BLUEPRINT, 'w', encoding='utf-8', newline='') as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(rows)
                    state.log(f"[RENAME] Database updated: Modified path for {updated_count} tracks.")
                else:
                    state.log(f"[RENAME] No matching tracks found in database to update.")
            else:
                state.log(f"[ERROR] CSV database file not found at {CSV_BLUEPRINT}")
                return

        # 4. Trigger Git Auto Push
        from config import AUTO_GIT_PUSH
        if AUTO_GIT_PUSH and updated_count > 0:
            state.log(f"[RENAME] Synchronizing updated database to GitHub...")
            # vm._git_lock is a threading.Lock - process-local only, doesn't
            # exclude the cron script or a manual deploy. Use the cross-process
            # filelock-based lock for the actual pull+push, same as every other
            # git-sync site in this file.
            with git_operation_lock(timeout=60):
                try:
                    # Get current branch name dynamically
                    res_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True)
                    branch_name = res_branch.stdout.strip()

                    subprocess.run(["git", "add", "configs/fmp_data_7718.csv"], check=True, capture_output=True)

                    # Check if there are any staged changes for the CSV file
                    status_res = subprocess.run(["git", "status", "--porcelain", "configs/fmp_data_7718.csv"], capture_output=True, text=True, check=True)
                    if status_res.stdout.strip():
                        commit_msg = f"Rename show {old_name} to {new_name}"
                        # Pathspec-restricted so this can never sweep up whatever else
                        # happens to be staged (same bug/fix as storage.py's
                        # _git_auto_push and sync_library_db.py's end-of-run commit).
                        commit_res = subprocess.run(["git", "commit", "configs/fmp_data_7718.csv", "-m", commit_msg], capture_output=True, text=True)
                        if commit_res.returncode != 0:
                            stdout_lower = commit_res.stdout.lower()
                            stderr_lower = commit_res.stderr.lower()
                            clean_messages = ["nothing to commit", "working tree clean", "no changes added to commit", "nothing added to commit"]
                            if not any(msg in stdout_lower or msg in stderr_lower for msg in clean_messages):
                                raise Exception(f"git commit failed with code {commit_res.returncode}: {commit_res.stderr or commit_res.stdout}")
                            else:
                                state.log(f"[RENAME] Commit skipped (clean message match). Proceeding to pull and push.")
                    else:
                        state.log(f"[RENAME] CSV database file has no changes to commit. Proceeding to pull and push.")

                    # Merge, not rebase+autostash - see config.git_safe_pull's
                    # docstring for the 2026-09-03 incident this replaces.
                    git_safe_pull(branch_name)
                    subprocess.run(["git", "push", "origin", branch_name], check=True, capture_output=True)
                    state.log(f"[RENAME] GitHub database synchronization successful.")
                except Exception as git_err:
                    state.log(f"[ERROR] GitHub synchronization failed: {git_err}")

    except Exception as e:
        state.log(f"[ERROR] Background rename task failed: {e}")
        logging.exception(f"Background rename crash: {e}")

@app.route('/api/rename_show', methods=['POST'])
def rename_show():
    auth_err = _require_internal_key()
    if auth_err: return auth_err
    data = request.get_json(silent=True) or {}
    old_name = data.get('old_name', '').strip()
    new_name = data.get('new_name', '').strip()
    
    if not old_name or not new_name:
        return jsonify({"status": "error", "message": "Show names cannot be empty"}), 400
        
    safe_old = "".join(c for c in old_name if c.isalnum() or c in (' ', '_', '-')).strip()
    safe_new = "".join(c for c in new_name if c.isalnum() or c in (' ', '_', '-')).strip()
    
    if not safe_old or not safe_new:
        return jsonify({"status": "error", "message": "Invalid show names"}), 400

    if safe_old == safe_new:
        return jsonify({"status": "error", "message": "Old name and new name are identical"}), 400

    # Local G: drive folder rename
    from config import MUSIC_DIR
    g_drive_base = MUSIC_DIR
    old_local_dir = os.path.join(g_drive_base, "Shows", safe_old)
    new_local_dir = os.path.join(g_drive_base, "Shows", safe_new)
    
    if os.path.exists(g_drive_base):
        if os.path.exists(old_local_dir):
            try:
                os.makedirs(os.path.dirname(new_local_dir), exist_ok=True)
                os.rename(old_local_dir, new_local_dir)
                state.log(f"[RENAME] Local directory renamed: Shows/{safe_old} -> Shows/{safe_new}")
            except Exception as e:
                state.log(f"[ERROR] Failed to rename local folder: {e}")
                return jsonify({"status": "error", "message": f"Local rename failed: {str(e)}"}), 500
        else:
            state.log(f"[RENAME] Local folder Shows/{safe_old} not found. Skipping local rename.")
    else:
        state.log(f"[WARNING] G: drive base path not found. Skipping local rename.")

    # Start background thread to rename remote and update CSV + Git
    threading.Thread(
        target=background_rename_task,
        args=(safe_old, safe_new),
        daemon=True
    ).start()
    
    return jsonify({
        "status": "ok",
        "message": f"Rename from '{safe_old}' to '{safe_new}' initiated successfully."
    })

def resolve_yt_meta_bg(item_id, url):
    try:
        cmd = YT_DLP_CMD + ["--skip-download", "--dump-json", "--no-warnings", url]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True)
        import json
        data = json.loads(result.stdout.strip().split('\n')[0])
        title = data.get('title')
        artist = data.get('artist') or data.get('creator') or data.get('uploader')
        
        # Check title for explicit/clean keywords
        explicit_status = None
        title_lower = (title or '').lower()
        if 'explicit' in title_lower:
            explicit_status = True
        elif 'clean' in title_lower:
            explicit_status = False

        with state.url_queue.lock:
            for item in state.url_queue.items:
                if item.get('id') == item_id:
                    item['title'] = title
                    item['artist'] = artist
                    item['is_yt'] = True
                    if explicit_status is not None and item.get('explicit') is None:
                        item['explicit'] = explicit_status
                    break
            state.url_queue._save_to_disk()
    except Exception as e:
        logging.error(f"Failed to resolve meta for queue item {item_id}: {e}")

def get_playlist_explicit_statuses_and_other_versions(playlist_url: str):
    """
    Downloads the playlist page, extracts:
    1. A list of booleans representing track explicit status by index.
    2. The other version playlist ID and title if present in the 'Other versions' shelf.
    """
    import urllib.request
    import re
    import json
    import codecs

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    req = urllib.request.Request(playlist_url, headers=headers)
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', errors='ignore')
        
        # Parse initialData
        scripts = re.findall(r'<script.*?>.*?</script>', html, re.DOTALL)
        data_script = None
        for script in scripts:
            if 'initialData' in script and ('MUSIC_EXPLICIT_BADGE' in script or 'musicResponsiveListItemRenderer' in script):
                data_script = script
                break
                
        if not data_script:
            return [], None, None
            
        matches = re.findall(r"data:\s*'([\s\S]*?)'", data_script)
        if len(matches) < 2:
            return [], None, None
            
        decoded = matches[1].encode('utf-8').decode('unicode_escape')
        data = json.loads(decoded)
        
        def find_nodes(node, key):
            results = []
            if isinstance(node, dict):
                if key in node:
                    results.append(node[key])
                for k, v in node.items():
                    results.extend(find_nodes(v, key))
            elif isinstance(node, list):
                for item in node:
                    results.extend(find_nodes(item, key))
            return results

        # Extract explicit statuses
        items = find_nodes(data, 'musicResponsiveListItemRenderer')
        statuses = []
        for item in items:
            is_explicit = False
            badges = item.get('badges', [])
            for badge in badges:
                try:
                    badge_type = badge['musicInlineBadgeRenderer']['icon']['iconType']
                    if badge_type == 'MUSIC_EXPLICIT_BADGE':
                        is_explicit = True
                except:
                    pass
            statuses.append(is_explicit)
            
        # Find other versions
        other_version_id = None
        other_version_title = None
        
        current_album_title = None
        try:
            current_album_title = data['header']['musicDetailHeaderRenderer']['title']['runs'][0]['text']
        except:
            pass
            
        if not current_album_title:
            try:
                current_album_title = data.get('title', '')
            except:
                pass
                
        shelves = find_nodes(data, 'musicCarouselShelfRenderer')
        for shelf in shelves:
            try:
                shelf_title = shelf['header']['musicCarouselShelfBasicHeaderRenderer']['title']['runs'][0]['text']
                if 'Other versions' in shelf_title:
                    for version_item in shelf.get('contents', []):
                        renderer = version_item.get('musicTwoRowItemRenderer', {})
                        version_title = "Unknown"
                        try:
                            version_title = renderer['title']['runs'][0]['text']
                        except:
                            pass
                            
                        if not current_album_title or version_title.strip().lower() == current_album_title.strip().lower():
                            playlist_ids = find_nodes(renderer, 'playlistId')
                            if playlist_ids:
                                other_version_id = playlist_ids[0]
                                other_version_title = version_title
                                break
                    if other_version_id:
                        break
            except:
                pass
                
        return statuses, other_version_id, other_version_title
    except Exception as e:
        logging.error(f"Error parsing playlist details from URL {playlist_url}: {e}")
        return [], None, None

def process_playlist_addition(u, target, auto_linked):
    """Parses playlist details, enqueues tracks with explicit status, and auto-links counterpart album."""
    explicit_statuses, counterpart_id, counterpart_title = get_playlist_explicit_statuses_and_other_versions(u)
    track_urls = extract_playlist_urls(u)
    
    for idx, track_url in enumerate(track_urls):
        is_explicit = False
        if idx < len(explicit_statuses):
            is_explicit = explicit_statuses[idx]
            
        item_id = state.url_queue.put({
            'url': track_url, 
            'type': 'ingest', 
            'target': target,
            'explicit': is_explicit
        })
        threading.Thread(target=resolve_yt_meta_bg, args=(item_id, track_url), daemon=True).start()
        
    if counterpart_id and not auto_linked:
        counterpart_url = f"https://music.youtube.com/playlist?list={counterpart_id}"
        state.log(f"[Auto-Link] Found counterpart album: \"{counterpart_title}\" ({counterpart_id}) -> Auto-queuing counterpart...")
        def enqueue_counterpart():
            process_playlist_addition(counterpart_url, target, auto_linked=True)
            state.update_count()
        threading.Thread(target=enqueue_counterpart, daemon=True).start()

@app.route('/add', methods=['POST'])
def add():
    data = request.get_json(silent=True) or {}
    raw_urls = data.get('urls', '').split('\n')
    target = data.get('target', '')
    title = data.get('title')
    artist = data.get('artist')
    auto_linked = data.get('auto_linked', False)
    explicit = data.get('explicit')
    is_radio = data.get('is_radio')
    overwrite = data.get('overwrite', False)
    
    for u in raw_urls:
        u = u.strip()
        if not u: continue
        
        item_data = {'url': u, 'type': 'ingest', 'target': target}
        if auto_linked:
            item_data['auto_linked'] = auto_linked
        if explicit is not None:
            item_data['explicit'] = explicit
        if is_radio is not None:
            item_data['is_radio'] = is_radio
        if overwrite:
            item_data['overwrite'] = overwrite
        # exact_source: download exactly this link (see downloader_worker). Only for real links.
        exact_source = bool(data.get('exact_source')) and u.startswith(('http://', 'https://'))
        if exact_source:
            item_data['exact_source'] = True
            if data.get('expected_seconds'):
                item_data['expected_seconds'] = float(data['expected_seconds'])
        # explicit_verified (keep the caller's explicit answer over the store lookup) is only
        # honoured together with exact_source - otherwise the file that arrives may not be the
        # version that was verified.
        if data.get('explicit_verified') and explicit is not None and exact_source:
            item_data['explicit_verified'] = True
        # fingerprint_verified: a written reason (the evidence) that this exact release is the song even though
        # AcoustID mislabels it. Only with exact_source and an expected length, so it can't wave through a search hit.
        fp_reason = str(data.get('fingerprint_verified') or '').strip()
        if fp_reason and exact_source and item_data.get('expected_seconds'):
            item_data['fingerprint_verified'] = fp_reason

        if len(raw_urls) == 1 and title and artist:
            item_data['title'] = title
            item_data['artist'] = artist
            item_data['is_yt'] = True
            
        if "list=" in u or "playlist" in u.lower():
            process_playlist_addition(u, target, auto_linked)
        elif not u.startswith("http://") and not u.startswith("https://") and not u.startswith("ytsearch"):
            item_data['url'] = f"ytsearch1:{u}"
            state.url_queue.put(item_data)
        else: 
            item_id = state.url_queue.put(item_data)
            # Skip background metadata resolution for direct video URLs (worker YTM-DLP handles these)
            if 'title' not in item_data and 'watch?v=' not in u:
                threading.Thread(target=resolve_yt_meta_bg, args=(item_id, u), daemon=True).start()

    state.update_count()
    start_engines()
    return jsonify({"status": "ok"})

@app.route('/upload_local', methods=['POST'])
def upload_local():
    auth_err = _require_internal_key()
    if auth_err:
        return auth_err
        
    target = request.form.get('target', '')
    if 'file' not in request.files: return jsonify({"status": "error"})
    
    file = request.files['file']
    if file.filename == '': return jsonify({"status": "error"})
    
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S_local")
    staging_path = os.path.join(STAGING_DIR, task_id)
    os.makedirs(staging_path, exist_ok=True)
    
    safe_filename = secure_filename(file.filename)
    if not safe_filename:
        safe_filename = f"upload_{datetime.now():%Y%m%d_%H%M%S}.mp3"
    raw_path = os.path.join(staging_path, safe_filename)
    file.save(raw_path)
    
    # Enqueue in state.url_queue for thread-safe sequential processing
    state.url_queue.put({
        'url': raw_path,
        'type': 'local',
        'task_id': task_id,
        'target': target
    })
    
    state.update_count()
    start_engines()
    return jsonify({"status": "ok"})

@app.route('/stop', methods=['POST'])
def stop(): 
    state.stop_event.set()
    state.set_status("Stopping...")
    return jsonify({"status": "ok"})

@app.route('/api/queue/remove', methods=['POST'])
def queue_remove():
    data = request.get_json(silent=True) or {}
    item_id = data.get('id')
    if item_id is not None:
        removed = state.url_queue.remove(int(item_id))
        if not removed:
            # scrape_queue is a separate UrlQueue instance (the broker
            # delegation queue /api/pull_jobs serves from) - a caller
            # acknowledging/clearing a delegated job only knows the job_id,
            # not which queue it actually lives in. Previously this route
            # only ever checked url_queue, so clearing a scrape_queue item
            # (e.g. a duplicate caught by the Windows worker before it
            # would re-download) always silently failed, leaving the same
            # job to be re-served to Windows on every subsequent poll.
            removed = state.scrape_queue.remove(int(item_id))
        state.update_count()
        if removed:
            state.log(f"[QUEUE] Removed item ID {item_id} from active download queue.")
            return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Item not found in queue"})

@app.route('/api/pending/approve_all', methods=['POST'])
def pending_approve_all():
    approved_count = 0
    req_data = request.get_json(silent=True) or {}
    override_target = req_data.get('target')
    
    if os.name != 'nt':
        # On remote VM, forward all items to local broker via SSH reverse tunnel
        import requests
        items_to_forward = []
        with state.lock:
            items_to_forward = list(state.pending_iheart_queue)
            state.pending_iheart_queue.clear()
            state.save_pending()
            
        success_count = 0
        failed_items = []
        for item in items_to_forward:
            url = item.get('url')
            target = override_target if override_target else item.get('target', '')
            payload = {
                "urls": url,
                "target": target,
                "title": item.get('title'),
                "artist": item.get('artist')
            }
            try:
                requests.post("http://127.0.0.1:58000/add", json=payload, timeout=5.0)
                success_count += 1
            except Exception as e:
                failed_items.append(item)
                
        if failed_items:
            # Restore failed items
            with state.lock:
                state.pending_iheart_queue.extend(failed_items)
                state.save_pending()
                
        if success_count > 0:
            state.log(f"[iHeart Sync] Approved all {success_count} discoveries -> Forwarded to local broker.")
            return jsonify({"status": "ok", "message": f"Successfully forwarded {success_count} tracks to local broker."})
        else:
            return jsonify({"status": "error", "message": "Local downloader bridge is offline."}), 503

    with state.lock:
        for item in state.pending_iheart_queue:
            url = item.get('url')
            target = override_target if override_target else item.get('target', '')
            state.url_queue.put({'url': url, 'type': 'ingest', 'target': target})
            approved_count += 1
        state.pending_iheart_queue.clear()
        state.save_pending()
    if approved_count > 0:
        state.update_count()
        start_engines()
        state.log(f"[iHeart Sync] Approved all {approved_count} discoveries -> Sent to download queue.")
        return jsonify({"status": "ok", "message": f"Successfully enqueued {approved_count} tracks."})
    return jsonify({"status": "ok", "message": "No discoveries to approve."})

@app.route('/api/stream_health', methods=['GET'])
def api_stream_health():
    import requests
    citrus_url = "https://hello.citrus3.com:8256/stream"
    citrus_json_url = "https://hello.citrus3.com:8256/status-json.xsl"
    takeover_url = "http://hello.citrus3.com:7048/fmpultimate"

    main_online = False
    main_code = 0
    try:
        r = requests.get(citrus_url, stream=True, timeout=3)
        main_code = r.status_code
        main_online = (r.status_code == 200)
    except Exception:
        main_online = False

    takeover_online = False
    try:
        r2 = requests.get(takeover_url, stream=True, timeout=2)
        takeover_online = (r2.status_code == 200)
    except Exception:
        takeover_online = False

    listeners = 0
    now_playing = "Unknown"
    bitrate = "128"

    try:
        r_stats = requests.get(citrus_json_url, timeout=3)
        if r_stats.status_code == 200:
            data = r_stats.json()
            sources = data.get('icestats', {}).get('source', [])
            if isinstance(sources, dict):
                sources = [sources]
            for s in sources:
                if '8256/stream' in s.get('listenurl', '') or 'stream' in s.get('listenurl', ''):
                    listeners = s.get('listeners', 0)
                    now_playing = s.get('title', 'Unknown')
                    bitrate = str(s.get('bitrate', 128))
                    break
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "streams": [
            {
                "name": "Citrus3 Main / Live365 Stream",
                "url": citrus_url,
                "online": main_online,
                "status_code": main_code,
                "listeners": listeners,
                "now_playing": now_playing,
                "bitrate": bitrate
            },
            {
                "name": "AutoDJ Takeover Mount (Port 7048)",
                "url": takeover_url,
                "online": takeover_online,
                "status_code": 200 if takeover_online else 0,
                "listeners": 0,
                "now_playing": "Liquidsoap Live Stream" if takeover_online else "Takeover Idle (Fallback Active)",
                "bitrate": bitrate
            }
        ]
    })

@app.route('/api/pending/approve', methods=['POST'])
def pending_approve():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    target = data.get('target', '')
    found = False
    item_to_forward = None
    with state.lock:
        for i, item in enumerate(state.pending_iheart_queue):
            if item['url'] == url:
                item_to_forward = item
                state.pending_iheart_queue.pop(i)
                found = True
                state.save_pending()
                break
    if found:
        if os.name != 'nt':
            # Forward to local broker via SSH reverse tunnel
            import requests
            payload = {
                "urls": url,
                "target": target,
                "title": item_to_forward.get('title'),
                "artist": item_to_forward.get('artist')
            }
            try:
                requests.post("http://127.0.0.1:58000/add", json=payload, timeout=10.0)
                state.log(f"[iHeart Sync] Approved track: {url} -> Forwarded to local broker.")
                return jsonify({"status": "ok"})
            except Exception as e:
                # Put it back in pending list if forward fails so we don't lose it
                with state.lock:
                    state.pending_iheart_queue.append(item_to_forward)
                    state.save_pending()
                state.log(f"[Proxy Error] Failed to forward approved track to local broker: {e}")
                return jsonify({"status": "error", "message": f"Local downloader bridge is offline: {e}"}), 503
        else:
            state.url_queue.put({'url': url, 'type': 'ingest', 'target': target})
            state.update_count()
            start_engines()
            state.log(f"[iHeart Sync] Approved track: {url} -> Sent to download queue.")
            return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Track not found in pending list"})

@app.route('/api/pending/approve_selected', methods=['POST'])
def pending_approve_selected():
    data = request.get_json(silent=True) or {}
    selected_urls = set(data.get('urls', []))
    override_target = data.get('target', '')
    approved_count = 0
    with state.lock:
        remaining = []
        for item in state.pending_iheart_queue:
            u = item.get('url')
            if u in selected_urls:
                target = override_target if override_target else item.get('target', '')
                state.url_queue.put({'url': u, 'type': 'ingest', 'target': target})
                approved_count += 1
            else:
                remaining.append(item)
        state.pending_iheart_queue = remaining
        state.save_pending()
    if approved_count > 0:
        state.update_count()
        start_engines()
        state.log(f"[iHeart Sync] Approved {approved_count} selected discoveries -> Sent to download queue.")
        return jsonify({"status": "ok", "message": f"Successfully enqueued {approved_count} tracks."})
    return jsonify({"status": "ok", "message": "No matching discoveries found."})

@app.route('/api/pending/reject', methods=['POST'])
def pending_reject():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    found = False
    with state.lock:
        for i, item in enumerate(state.pending_iheart_queue):
            if item['url'] == url:
                state.pending_iheart_queue.pop(i)
                found = True
                if url not in state.rejected_iheart:
                    state.rejected_iheart.append(url)
                    state.save_rejected()
                state.save_pending()
                break
    if found:
        state.log(f"[iHeart Sync] Rejected track: {url}")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Track not found in pending list"})

@app.route('/api/pending/reject_selected', methods=['POST'])
def pending_reject_selected():
    data = request.get_json(silent=True) or {}
    selected_urls = set(data.get('urls', []))
    rejected_count = 0
    with state.lock:
        remaining = []
        for item in state.pending_iheart_queue:
            u = item.get('url')
            if u in selected_urls:
                if u not in state.rejected_iheart:
                    state.rejected_iheart.append(u)
                rejected_count += 1
            else:
                remaining.append(item)
        state.pending_iheart_queue = remaining
        state.save_rejected()
        state.save_pending()
    state.log(f"[iHeart Sync] Rejected {rejected_count} selected discoveries.")
    return jsonify({"status": "ok", "message": f"Rejected {rejected_count} tracks."})

@app.route('/api/pending/clear', methods=['POST'])
def pending_clear():
    with state.lock:
        count = len(state.pending_iheart_queue)
        state.pending_iheart_queue.clear()
        state.save_pending()
    state.log(f"[iHeart Sync] Cleared all {count} discoveries.")
    return jsonify({"status": "ok"})

@app.route('/api/search_yt', methods=['POST'])
def search_yt():
    data = request.get_json(silent=True) or {}
    query = data.get('query', '')
    if not query:
        return jsonify([])
    try:
        # If the user pasted a direct link, just dump its metadata.
        if query.startswith("http://") or query.startswith("https://"):
            cmd = YT_DLP_CMD + ["--skip-download", "--dump-json", "--no-warnings", query]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True)
            results = []
            for line in result.stdout.strip().split('\n'):
                if not line: continue
                try:
                    import json
                    d = json.loads(line)
                    title = d.get('title', 'Unknown Title')
                    desc = d.get('description', '') or ''
                    is_explicit = 'explicit' in title.lower() or 'explicit' in desc.lower()
                    
                    thumbnail_url = d.get('thumbnail', '')
                    if not thumbnail_url and d.get('thumbnails'):
                        thumbnail_url = d.get('thumbnails')[-1].get('url', '')
                        
                    results.append({
                        'title': title,
                        'uploader': d.get('uploader', 'Unknown Artist'),
                        'duration_string': d.get('duration_string', '--:--'),
                        'url': d.get('webpage_url', ''),
                        'explicit': is_explicit,
                        'thumbnail': thumbnail_url
                    })
                except Exception as ex:
                    logging.error(f"Failed parsing direct link metadata: {ex}")
            return jsonify(results)
        else:
            # Use ytmusicapi search for fast, official song results
            import re
            query_clean = re.sub(r'\[.*?\]', '', query).strip()
            query_clean = re.sub(r'\s+', ' ', query_clean)
            
            from ytmusicapi import YTMusic
            ytm = YTMusic()
            songs = ytm.search(query_clean, filter="songs")
            results = []
            for song in songs[:5]:
                video_id = song.get('videoId')
                if not video_id:
                    continue
                artists = ", ".join([a.get('name', 'Unknown') for a in song.get('artists', [])])
                
                thumbnails = song.get('thumbnails', [])
                thumbnail_url = thumbnails[-1].get('url', '') if thumbnails else ''
                
                results.append({
                    'title': song.get('title', 'Unknown Title'),
                    'uploader': artists,
                    'duration_string': song.get('duration', '--:--'),
                    'url': f"https://music.youtube.com/watch?v={video_id}",
                    'explicit': song.get('isExplicit', False),
                    'thumbnail': thumbnail_url
                })
            return jsonify(results)
    except subprocess.CalledProcessError as e:
        state.log(f"[SEARCH ERROR] yt-dlp failed: {e.stderr}")
        logging.error(f"Search failed for {query}. Stderr: {e.stderr}")
        return jsonify([])
    except Exception as e:
        state.log(f"[SEARCH ERROR] {e}")
        logging.error(f"Search exception for {query}: {e}")
        return jsonify([])

@app.route('/api/resolve_audio', methods=['GET', 'POST'])
def resolve_audio():
    url = ""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        url = data.get('url', '')
    else:
        url = request.args.get('url', '')
        
    if not url:
        return jsonify({"error": "No URL provided"}), 400
        
    try:
        # Resolve direct audio stream link using yt-dlp with cookies (via YT_DLP_CMD)
        cmd = YT_DLP_CMD + ["-g", "-f", "bestaudio", url]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True)
        stream_url = result.stdout.strip()
        return jsonify({"url": stream_url})
    except subprocess.CalledProcessError as e:
        app.logger.error(f"Failed to resolve audio URL: {e.stderr}")
        return jsonify({"error": e.stderr}), 500
    except Exception as e:
        app.logger.error(f"Failed to resolve audio URL: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/audition', methods=['GET'])
def audition_track():
    url = request.args.get('url', '')
    artist = request.args.get('artist', '')
    title = request.args.get('title', '')
    if not url and (artist or title):
        # Resolve via ytmusicapi
        try:
            from ytmusicapi import YTMusic
            ytm = YTMusic()
            res = ytm.search(f"{artist} - {title}".strip(), filter="songs")
            if res and res[0].get('videoId'):
                url = f"https://music.youtube.com/watch?v={res[0]['videoId']}"
        except Exception:
            pass
    if not url:
        return jsonify({"error": "No URL or track info provided"}), 400
    try:
        cmd = YT_DLP_CMD + ["-g", "-f", "bestaudio", url]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True)
        return jsonify({"url": result.stdout.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/search_scrub', methods=['POST'])
def search(): 
    try:
        data = request.get_json(silent=True) or {}
        return jsonify(VaultManager().find_candidates(data.get('query', '')))
    except Exception as e:
        print(f"\n[CRITICAL SEARCH ERROR] >>> {e} <<<\n")
        logging.error(f"Dashboard search crash: {e}")
        return jsonify([])

@app.route('/execute_scrub', methods=['POST'])
def execute():
    auth_err = _require_internal_key()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    name = data.get('track_name', '')
    file_path = data.get('file_path', '')
    target = file_path if file_path else name
    s, m = VaultManager().scrub_track(target)
    state.log(f"[ERASED] {name} ({file_path.replace('\\\\', '/').split('/')[-1] if file_path else ''})" if s else f"[FAILED] {m}")
    if not s:
        logging.error(f"Scrub failed for {target}: {m}")
    return jsonify({"status": "ok" if s else "error", "message": m})

@app.route('/api/stream_local')
def stream_local():
    auth_err = _require_internal_key()
    if auth_err:
        return auth_err
        
    file_path = request.args.get('file_path', '')
    track_name = request.args.get('track_name', '')
    
    import csv
    from config import resolve_physical_path, CSV_BLUEPRINT
    from modules.storage import VaultManager
    
    if not file_path and track_name:
        with VaultManager._csv_lock:
            if os.path.exists(CSV_BLUEPRINT):
                try:
                    with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            if row.get('Track Name') == track_name:
                                file_path = row.get('File Path')
                                break
                except Exception as e:
                    pass
                    
    if not file_path:
        return jsonify({"error": "No file path provided"}), 400
        
    physical_path = resolve_physical_path(file_path)
    if not physical_path or not os.path.exists(physical_path):
        return jsonify({"error": f"Physical file not found on disk: {file_path}"}), 404
        
    music_root = os.path.abspath(MUSIC_DIR)
    resolved = os.path.abspath(physical_path)
    if os.path.commonpath([music_root, resolved]) != music_root:
        return jsonify({"error": "Invalid path"}), 403
        
    from flask import send_file
    return send_file(physical_path, mimetype="audio/mpeg")

@app.route('/api/artwork')
def get_artwork():
    auth_err = _require_internal_key()
    if auth_err:
        return auth_err
        
    track_name = request.args.get('track_name', '')
    file_path = request.args.get('file_path', '')
    placeholder_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
        <rect width="100%" height="100%" fill="#1a1a1a"/>
        <circle cx="50" cy="50" r="25" fill="none" stroke="#ff3e3e" stroke-width="2"/>
        <circle cx="50" cy="50" r="8" fill="#ff3e3e"/>
        <path d="M50 25 L50 42 L65 35 Z" fill="#ff3e3e"/>
    </svg>"""
    
    if not file_path and not track_name:
        from flask import Response
        return Response(placeholder_svg, mimetype="image/svg+xml")
        
    import csv
    from config import resolve_physical_path, CSV_BLUEPRINT
    from modules.storage import VaultManager
    
    if not file_path and track_name:
        with VaultManager._csv_lock:
            if os.path.exists(CSV_BLUEPRINT):
                try:
                    with open(CSV_BLUEPRINT, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            if row.get('Track Name') == track_name:
                                file_path = row.get('File Path')
                                break
                except Exception as e:
                    pass
                
    if file_path:
        physical_path = resolve_physical_path(file_path)
        if physical_path and os.path.exists(physical_path):
            music_root = os.path.abspath(MUSIC_DIR)
            resolved = os.path.abspath(physical_path)
            if os.path.commonpath([music_root, resolved]) != music_root:
                return jsonify({"error": "Invalid path"}), 403
                
            from mutagen.mp3 import MP3
            from mutagen.id3 import APIC, ID3
            try:
                audio = MP3(physical_path, ID3=ID3)
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        from flask import Response
                        return Response(tag.data, mimetype=tag.mime)
            except Exception:
                pass
                
    from flask import Response
    return Response(placeholder_svg, mimetype="image/svg+xml")

def extract_metadata_from_file(file_path):
    from mutagen.mp3 import MP3
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, TXXX
    
    meta = {
        "artist": "Unknown Artist",
        "title": "Unknown Title",
        "explicit": False,
        "release_year": "Unknown",
        "duration_ms": 0,
        "bpm": 0,
        "intro_duration": 10000,
        "punch_ms": 0,
        "outro_duration": 20000,
        "bitrate": "320k",
        "lyrics": "Not Found",
        "fingerprint": ""
    }
    
    try:
        audio = MP3(file_path)
        meta["duration_ms"] = int(audio.info.length * 1000)
        meta["bitrate"] = f"{int(audio.info.bitrate / 1000)}k"
    except Exception as e:
        logging.warning(f"Error reading MP3 properties from {file_path}: {e}")
        
    try:
        tags = EasyID3(file_path)
        if "artist" in tags:
            meta["artist"] = tags["artist"][0]
        if "title" in tags:
            meta["title"] = tags["title"][0]
        if "date" in tags:
            meta["release_year"] = tags["date"][0]
    except Exception as e:
        logging.warning(f"Error reading EasyID3 tags from {file_path}: {e}")
        
    try:
        id3 = ID3(file_path)
        # Check custom TXXX tags
        for key in id3.keys():
            if key.startswith("TXXX:"):
                txxx = id3[key]
                txxx_desc = key.split(":", 1)[1].upper()
                val = txxx.text[0] if txxx.text else ""
                if txxx_desc == "INTRO_DURATION":
                    try: meta["intro_duration"] = int(float(val))
                    except: pass
                elif txxx_desc == "PUNCH_MS":
                    try: meta["punch_ms"] = int(float(val))
                    except: pass
                elif txxx_desc == "OUTRO_DURATION":
                    try: meta["outro_duration"] = int(float(val))
                    except: pass
                elif txxx_desc == "AUDIO_FINGERPRINT":
                    meta["fingerprint"] = val
        # Check standard TBPM tag
        if "TBPM" in id3:
            try: meta["bpm"] = int(float(id3["TBPM"].text[0]))
            except: pass
            
        # Check USLT tag for lyrics
        for key in id3.keys():
            if key.startswith("USLT"):
                meta["lyrics"] = id3[key].text
                break
    except Exception as e:
        logging.warning(f"Error reading raw ID3 tags from {file_path}: {e}")
        
    # Check filename for (Explicit) tag if not already set
    if "explicit" in os.path.basename(file_path).lower():
        meta["explicit"] = True
        
    return meta

@app.route('/api/pull_jobs', methods=['GET'])
def get_pull_jobs():
    auth_err = _require_internal_key()
    if auth_err: return auth_err
    # Serves the Soulseek-miss scrape queue, not the general url_queue -
    # the Windows worker only needs to handle what Soulseek couldn't find.
    with state.scrape_queue.lock:
        items = list(state.scrape_queue.items)
    return jsonify(items)

@app.route('/api/deletions/enqueue', methods=['POST'])
def enqueue_deletion_api():
    auth_err = _require_internal_key()
    if auth_err: return auth_err
    payload = request.get_json(silent=True) or {}
    if not payload or not payload.get("file_path"):
        return jsonify({"status": "error", "message": "Missing file_path"}), 400
    state.enqueue_deletion(payload["file_path"])
    return jsonify({"status": "success"})

@app.route('/api/deletions/poll', methods=['GET'])
def poll_deletions_api():
    auth_err = _require_internal_key()
    if auth_err: return auth_err
    items = state.poll_deletions()
    return jsonify(items)

@app.route('/api/pull_jobs/complete', methods=['POST'])
def complete_pull_job():
    auth_err = _require_internal_key()
    if auth_err: return auth_err
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"status": "error", "message": "Missing JSON payload"}), 400
        
    item_id = payload.get("item_id")
    staging_path = payload.get("staging_path")
    target = payload.get("target")
    overwrite = payload.get("overwrite", False)
    source_url = payload.get("source_url", "")
    
    if not item_id or not staging_path:
        return jsonify({"status": "error", "message": "Missing item_id or staging_path"}), 400
        
    if not os.path.exists(staging_path):
        return jsonify({"status": "error", "message": f"Staging file not found: {staging_path}"}), 400
        
    # Extract metadata using mutagen from the staging path
    meta = extract_metadata_from_file(staging_path)
    if source_url:
        meta['url'] = source_url
    if target:
        meta['target'] = target
        
    state.log(f"[Pull Broker] Vaulting {os.path.basename(staging_path)}...")
    
    vm = VaultManager()
    status, msg = vm.store_track(
        staging_path, 
        meta, 
        task_id=f"pull_{item_id}", 
        target_override=target, 
        overwrite=overwrite
    )
    
    if status:
        removed = state.scrape_queue.remove(item_id)
        state.increment_completed()
        
        # Trigger immediate background rclone sync to Google Drive on Linux
        if os.name != 'nt':
            try:
                subprocess.Popen(
                    ["rclone", "copy", "/home/ubuntu/music/", "gdrive:FMP MUSIC/BASE/MUSIC", 
                     "--ignore-existing", "--transfers=4", "--checkers=8", 
                     "--exclude", "/staging/**", "--exclude", "/Shows_to_delete/**"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                state.log("[SYNC] Triggered instant Google Drive background sync.")
            except Exception as sync_err:
                logging.error(f"Failed to trigger instant rclone sync: {sync_err}")
                
        return jsonify({"status": "ok", "message": "Track successfully vaulted."})
    else:
        # Same zombie-queue bug as downloader_worker()'s early-exit paths
        # (app.py, fixed same session) - this only ever removed the item on
        # success, so a permanent rejection (duplicate, quality gate, etc.)
        # left it stuck in scrape_queue forever: Windows already claimed
        # this job_id via claimed_broker_job_ids, so it will never re-poll
        # it, and nothing else ever clears the VM-side entry. Confirmed
        # live 2026-09-03 (The Isley Brothers - Make Me Say It Again Girl,
        # rejected as a duplicate already in the catalog).
        state.scrape_queue.remove(item_id)
        state.log(f"[Pull Broker Error] Vaulting failed: {msg}")
        return jsonify({"status": "error", "message": msg}), 500

@app.route('/api/sync/pull', methods=['POST'])
def sync_pull_gdrive():
    def run_pull():
        try:
            if os.name == 'nt':
                state.log("[GDrive Sync] Running on Windows, local Google Drive client handles pulls automatically.")
                return
                
            state.log("[GDrive Sync] Triggering background GDrive-to-VM pull sync...")
            
            cmd = [
                "rclone", "copy", "gdrive:FMP MUSIC/BASE/MUSIC", "/home/ubuntu/music",
                "--size-only",
                "--transfers=4",
                "--checkers=8",
                "--exclude", "/staging/**",
                "-v"
            ]
            
            res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
            if res.returncode == 0:
                state.log("[GDrive Sync] GDrive-to-VM pull sync completed successfully.")
            else:
                state.log(f"[GDrive Sync Error] Pull sync failed with code {res.returncode}: {res.stderr.strip()[:200]}")
        except Exception as e:
            state.log(f"[GDrive Sync Error] Failed to run sync command: {e}")
            
    threading.Thread(target=run_pull, daemon=True).start()
    return jsonify({"status": "success", "message": "Google Drive pull sync started in the background."})

@app.route('/api/public/shoutout', methods=['POST', 'OPTIONS'])
def public_submit_shoutout():
    """
    Public shout-out submission API.
    Securely forwards listener shout-outs to Discord (and optionally Sheets).
    Protects Webhook and API credentials from browser inspection.
    """
    origin = request.headers.get('Origin', '*')
    
    if request.method == 'OPTIONS':
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Allow-Methods'] = 'POST'
        return response

    data = {}
    if request.is_json:
        data = request.json or {}
    else:
        data = request.form or {}

    name = data.get('name', '').strip()
    message = data.get('message', '').strip()

    if not name or not message:
        response = jsonify({"error": "Name and message cannot be empty."})
        response.headers['Access-Control-Allow-Origin'] = origin
        return response, 400

    if len(name) > 50:
        response = jsonify({"error": "Name must not exceed 50 characters."})
        response.headers['Access-Control-Allow-Origin'] = origin
        return response, 400

    if len(message) > 1000:
        response = jsonify({"error": "Message must not exceed 1000 characters."})
        response.headers['Access-Control-Allow-Origin'] = origin
        return response, 400

    discord_url = os.environ.get('DISCORD_WEBHOOK_URL') or os.getenv('DISCORD_WEBHOOK_URL')
    if not discord_url:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(BASE_DIR, ".env"))
            discord_url = os.getenv('DISCORD_WEBHOOK_URL')
        except Exception:
            pass

    if not discord_url:
        state.log("[Shout-out Error] DISCORD_WEBHOOK_URL is not set.")
        response = jsonify({"error": "Serverless function missing credentials."})
        response.headers['Access-Control-Allow-Origin'] = origin
        return response, 500

    discord_payload = {
        "username": "FMP Shout-Out",
        "avatar_url": "https://i.imgur.com/SjeIgZV.png",
        "embeds": [{
            "color": 15381256,
            "title": "📢 New Shout-Out!",
            "fields": [
                {"name": "From", "value": name, "inline": True},
                {"name": "Message", "value": message, "inline": False}
            ],
            "footer": {"text": "Securely proxied via VM FMP Ultimate Gateway"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }]
    }

    sheets_url = os.environ.get('SHEETS_ENDPOINT_URL') or os.getenv('SHEETS_ENDPOINT_URL')

    success = False
    try:
        req_disc = urllib.request.Request(
            discord_url,
            data=json.dumps(discord_payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'FMP-Ultimate-Shoutout/1.0'}
        )
        with urllib.request.urlopen(req_disc, timeout=10) as resp_disc:
            if resp_disc.status in (200, 204):
                success = True

        if sheets_url:
            try:
                sheets_payload = {"name": name, "message": message}
                req_sheets = urllib.request.Request(
                    sheets_url,
                    data=json.dumps(sheets_payload).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'User-Agent': 'FMP-Ultimate-Shoutout/1.0'}
                )
                with urllib.request.urlopen(req_sheets, timeout=10) as resp_sheets:
                    pass
            except Exception as e_sheets:
                state.log(f"[Shout-out Warning] Google Sheets submit failed: {e_sheets}")
    except Exception as e_disc:
        state.log(f"[Shout-out Error] Discord submit failed: {e_disc}")

    if success:
        response = jsonify({"status": "success"})
        response.headers['Access-Control-Allow-Origin'] = origin
        return response
    else:
        response = jsonify({"error": "Failed to deliver shout-out to Discord."})
        response.headers['Access-Control-Allow-Origin'] = origin
        return response, 500

if __name__ == '__main__':
    os.system('') 
    
    red_banner = "\033[91m" + r"""================================================================================
                                 ChiTownSounds'
--------------------------------------------------------------------------------
   ______ __  __  _____     _    _  _  _    _                    _       
  |  ____|  \/  ||  __ \   | |  | || || |  (_)                  | |      
  | |__  | \  / || |__) |  | |  | || || |_  _  _ __ ___   __ _  | |_  ___
  |  __| | |\/| ||  ___/   | |  | || || __|| || '_ ` _ \ / _` | | __|/ _ \
  | |    | |  | || |       | |__| || || |_ | || | | | | | (_| | | |_|  __/
  |_|    |_|  |_||_|        \____/ |_| \__||_||_| |_| |_|\__,_|  \__|\___|

================================================================================
  PROGRAM DIRECTOR: ChiTownSounds aka Will
  SYSTEM: FMP Ultimate Download System
  STATUS: Initializing...
================================================================================""" + "\033[0m"

    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        print(red_banner)
        
    state.update_count()
    start_engines()
    app.run(host=APP_HOST, port=APP_PORT, debug=False)