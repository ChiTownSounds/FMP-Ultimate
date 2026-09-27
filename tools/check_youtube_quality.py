#!/usr/bin/env python3
"""
Daily check that YouTube still gives FMP Ultimate the Premium audio formats (~256k AAC / Opus).

Why: when the YouTube sign-in behind the downloader lapses, downloads don't fail - they quietly drop to the free
128k formats. That went unnoticed until 2026-09-27. This asks YouTube for a known song with the SAME cookie
settings the downloader uses (config.YT_DLP_CMD) and raises a Windows notification when the best audio is
below 200 kbps or YouTube asks to sign in.

    py tools/check_youtube_quality.py            # prints the result, notifies on failure, exit 1 on failure
Log: C:\\FMP_Broadcaster\\logs\\youtube_quality.log (one line per run). Run daily by the "FMP YouTube Quality Check" task.
"""
import os, re, sys, subprocess, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from config import YT_DLP_CMD, YT_AUDIO_FORMAT  # noqa: E402

TEST_URL = "https://music.youtube.com/watch?v=2yOtVlq8qGc"   # Mya - Movin' On (official release)
MIN_KBPS = 200
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # run helpers without flashing a console
LOG = r"C:\FMP_Broadcaster\logs\youtube_quality.log"


def notify(title, text):
    """Windows balloon notification (no extra modules needed)."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Warning; "
        f"$n.BalloonTipTitle = '{title}'; $n.BalloonTipText = '{text}'; $n.Visible = $true; "
        "$n.ShowBalloonTip(20000); Start-Sleep -Seconds 20; $n.Dispose()"
    )
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], creationflags=NO_WINDOW)
    except Exception:
        pass


def main():
    # Ask which format a real download would pick (same cookies, same format rule), without downloading.
    cmd = YT_DLP_CMD + ["-f", YT_AUDIO_FORMAT, "-s", "--print", "PICKED %(format_id)s %(abr)s", "--no-warnings", TEST_URL]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                             creationflags=NO_WINDOW)
        out = (res.stdout or "") + (res.stderr or "")
    except Exception as e:
        out, res = f"could not run yt-dlp: {e}", None
    m = re.search(r"PICKED (\S+) ([0-9.]+)", out)
    best = int(float(m.group(2))) if m else 0
    picked = m.group(1) if m else "none"
    source = "firefox" if "--cookies-from-browser" in YT_DLP_CMD else "cookies.txt"
    signin = "sign in" in out.lower()
    ok = best >= MIN_KBPS and not signin
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if ok:
        line = f"{stamp}  OK    downloads get format {picked} at {best}k (Premium) via {source}"
    else:
        why = "YouTube asked to sign in" if signin else (f"downloads would get format {picked} at only {best}k" if best else "no format selected")
        line = f"{stamp}  FAIL  {why} via {source} - downloads are at FREE quality until the YouTube sign-in is fixed"
        notify("FMP Ultimate: YouTube quality dropped",
               f"{why}. Sign Firefox back into the station YouTube Music account.")
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
