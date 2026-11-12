# CineLanka VIP

This is a small, single-owner movie catalog. Visitors can browse and download
published titles; adding titles, viewing payment receipts, and approving paid
access require the administrator password.

## Public GitHub Pages preview

GitHub Pages can serve the public, static website preview at
<https://lakshancreations26-crypto.github.io/cinelanka-vip/>. GitHub Pages
cannot run this project's Python server. The preview intentionally disables
admin login and clearly states that uploads, downloads and payment verification
are unavailable until the Python backend is deployed separately.

## Run locally on Windows

1. Install Python 3.13 if it is not already installed.
2. From this folder, run `.\start.ps1` in PowerShell.
3. Enter an admin password with at least 14 characters when prompted. Use the
   same password on later starts; the script does not save it to a file.
4. Open <http://127.0.0.1:8000/>. Choose **Admin login** to add movies or series.

The server stores title details in `data\cinelanka.sqlite3`, videos and posters
in `uploads\`, and payment receipts in the private `data\receipts\` folder.
Those folders are created automatically and excluded by `.gitignore`.

The title upload limit is 20 GB per video; posters and receipts are limited to
12 MB each. Only MP4, M4V, MOV, WebM, MKV, AVI, MPG, MPEG and TS video files,
and JPG, PNG and WebP posters are accepted.

## Important deployment note

The included Python standard-library server binds to `127.0.0.1` and is for
local testing or development, not direct public production hosting. A public
site needs a host, HTTPS, persistent private storage, backups, and a production
server/reverse proxy configured to preserve HTTPS and set secure cookies. Do not
expose this development server directly to the internet. Admin sessions end
when the server stops and expire after 12 hours.

Only upload movies and series that you own or are licensed to distribute.
Receipt approval is manual: inspect the EZ Cash receipt before approving a
request in **Admin mode**. Approval unlocks the title for that visitor's browser.
