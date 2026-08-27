# Hosting retypeset online, privately, by invitation

Four options, cheapest first. All keep the app private — nobody without an
invitation can reach it.

**Before anything else, read the two constraints at the bottom.** One of them
(unpublished manuscripts on someone else's server) may decide this for you.

---

## 1. Streamlit Community Cloud — free, invite by email

Best starting point: free, no server to run, and access control is a list of
email addresses.

1. Put the project in a **private** GitHub repository.
2. Sign in at `share.streamlit.io` with that GitHub account, click *New app*,
   point it at `app.py`.
3. Under *Settings → Sharing*, set the app to **private** and add the email
   addresses of the people you invite. They sign in with Google or GitHub and
   see nothing until you add them.

Add a `packages.txt` next to `requirements.txt` so the system-level tools are
installed:

```
pandoc
libcairo2
libpango-1.0-0
fonts-liberation
```

> **`packages.txt` must contain bare package names only — no comments, no blank
> structure, nothing clever.** Streamlit Cloud passes every line straight to
> `apt-get install`. A comment reading `# SVG -> PDF conversion` makes apt read
> `->` as a command-line option and the whole build dies with
> `E: Command line option '>' [from ->] is not understood`, which names neither
> the file nor the line. `requirements.txt` is different — that one is read by
> pip, which does support `#` comments.

Why those four, and what is deliberately absent:

| Package | Needed for |
|---|---|
| `pandoc` | the only mature OMML → LaTeX converter; the parser cannot run without it |
| `libcairo2`, `libpango-1.0-0` | `cairosvg` links against system Cairo and fails at **import** time without it, so SVG figures would break before any conversion is attempted |
| `fonts-liberation` | metric-compatible substitutes for Times New Roman and Arial; a server has no Microsoft fonts |

Not installed, on purpose:

- **`libreoffice-writer`** — roughly 400 MB against a 1 GB limit, and the app
  uses it for exactly one thing: converting EMF/WMF figures to PDF for the LaTeX
  route. Without it those figures are *reported* rather than silently dropped,
  and the fix is to re-export them from Word anyway. Add it only if you hit that
  case often and the build still fits.
- **`poppler-utils`** — not used by the application at all.

Limits: 1 GB RAM per app and no persistent disk. Fine for the parser and both
renderers; the app sleeps after inactivity and takes ~30 s to wake. Your
`models/corrections.jsonl` will **not** survive a restart — see §5.

---

## 2. Hugging Face Spaces — free, private, no GitHub needed

Create a Space with SDK *Streamlit*, set visibility **Private**, then invite
collaborators by username. Same `packages.txt`, same no-comments rule.

Advantage over Streamlit Cloud: a Space can have **persistent storage** (paid
tier), which is what you want if you intend to accumulate training data.

---

## 3. A small VPS with a login wall — full control, ~$6/month

Hetzner, DigitalOcean or Scaleway. This is the option to choose if you want
real accounts, persistent data and no third party holding the manuscripts.

```bash
# on the server
sudo apt update && sudo apt install -y python3-pip pandoc libreoffice-writer \
     poppler-utils nginx apache2-utils
pip install -r requirements.txt

# run the app bound to localhost only, so it is not reachable directly
python3 -m streamlit run app.py \
    --server.port 8501 --server.address 127.0.0.1 --server.headless true
```

Put nginx in front with HTTP basic auth and TLS:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd chouaib     # repeat per invited user
```

```nginx
server {
    listen 443 ssl;
    server_name retypeset.example.org;

    ssl_certificate     /etc/letsencrypt/live/retypeset.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/retypeset.example.org/privkey.pem;

    client_max_body_size 64M;          # manuscripts with figures are large

    location / {
        auth_basic           "retypeset";
        auth_basic_user_file /etc/nginx/.htpasswd;

        proxy_pass         http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;    # Streamlit needs websockets
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_read_timeout 3600;
    }
}
```

`sudo certbot --nginx -d retypeset.example.org` issues the certificate free.

Keep it running with systemd:

```ini
# /etc/systemd/system/retypeset.service
[Unit]
Description=retypeset review console
After=network.target

[Service]
User=retypeset
WorkingDirectory=/opt/retypeset
ExecStart=/usr/bin/python3 -m streamlit run app.py \
          --server.port 8501 --server.address 127.0.0.1 --server.headless true
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 4. Docker — reproducible, runs anywhere

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc libreoffice-writer poppler-utils fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
```

```bash
docker build -t retypeset .
docker run -p 8501:8501 -v $(pwd)/models:/app/models retypeset
```

The `-v` mount is what makes training data survive a container restart.

---

## 5. Two things that will bite you

**Manuscripts are unpublished and often confidential.** Uploading a paper under
review to a free third-party host means it sits on someone else's disk. Before
choosing options 1 or 2, check what your co-authors and your institution accept.
Option 3 or 4 on university infrastructure avoids the question entirely. Also
add a line telling users that uploads are deleted when the session ends — and
make that true: the app writes to `tempfile.mkdtemp()`, which most hosts clear
on restart, but a long-lived VPS does not. A daily
`find /tmp -name 'retypeset_*' -mtime +1 -exec rm -rf {} +` cron job closes that gap.

**Streamlit has no multi-user isolation.** One process serves everyone, and
`st.session_state` is per browser session, not per account. That is fine for a
handful of invited colleagues. It is not fine for public sign-up: there is no
per-user quota, no audit trail, and a crash affects everyone. If this grows past
a small invited group, the honest next step is a real backend (FastAPI plus a
job queue) with Streamlit reduced to a front end.

---

## Recommended path

Start with **Streamlit Community Cloud, private, three or four invited
colleagues**. It costs nothing and tells you within a week whether the tool is
actually used. Move to **option 3 on a university VM** the moment either
confidentiality or accumulated training data starts to matter.
