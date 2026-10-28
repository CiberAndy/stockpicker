from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
import gzip
import base64
import re
import json
import os
import subprocess
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_INDEX = "https://mfs.deutsche-boerse.com/DETR-posttrade"
APP_VERSION = "Stockpicker-Feed-2026-09-17"
YAHOO_CACHE = {}
YAHOO_LOCK = threading.Lock()

def fetch_url(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 SignalLab/1.0", "Accept": "*/*"})
    with urlopen(req, timeout=20) as response:
        return response.read()

def fetch_yahoo(url):
    """Fetch Yahoo data while respecting the machine's proxy configuration.

    Some environments can only reach the public internet through HTTP(S)_PROXY.
    The previous implementation removed those variables, which made every
    Yahoo request fail on such machines.
    """
    # Be tolerant if a URL was copied from a Markdown link by mistake.
    if url.startswith("[") and "](" in url:
        url = url[1:].split("](", 1)[0].replace("\\&", "&")
    # Keep the inherited proxy settings. Set SIGNALLAB_DISABLE_PROXY=1 only
    # when the machine explicitly requires a direct connection.
    net_env = os.environ.copy()
    if os.environ.get("SIGNALLAB_DISABLE_PROXY") == "1":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            net_env.pop(key, None)
    now = time.time()
    with YAHOO_LOCK:
        cached = YAHOO_CACHE.get(url)
        if cached and now - cached[0] < 60:
            return cached[1]
    ps_command = "$ProgressPreference='SilentlyContinue'; $c=(Invoke-WebRequest -UseBasicParsing -Uri '" + url + "').Content; [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($c))"
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
                                capture_output=True, timeout=30, check=True, env=net_env)
        body = base64.b64decode(result.stdout.strip())
        if body.lstrip().startswith(b"{"):
            with YAHOO_LOCK:
                YAHOO_CACHE[url] = (time.time(), body)
            return body
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    args = ["curl.exe", "-L", "--fail", "--silent", "--show-error", "--compressed", "--max-time", "20",
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "-H", "Accept: application/json,text/plain,*/*", url]
    last_error = None
    for attempt in range(3):
        try:
            result = subprocess.run(args, capture_output=True, timeout=25, check=True, env=net_env)
            with YAHOO_LOCK:
                YAHOO_CACHE[url] = (time.time(), result.stdout)
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131", "Accept": "application/json"})
        with urlopen(req, timeout=20) as response:
            body = response.read()
        with YAHOO_LOCK:
            YAHOO_CACHE[url] = (time.time(), body)
        return body
    except Exception as exc:
        if last_error is None:
            last_error = exc
        detail = str(last_error) or str(exc)
        raise RuntimeError(f"Yahoo Finance nicht erreichbar: {detail}") from exc

def flatten_values(value, out=None):
    out = out or {}
    if isinstance(value, dict):
        for key, item in value.items():
            out[str(key).lower()] = item
            flatten_values(item, out)
    elif isinstance(value, list):
        for item in value:
            flatten_values(item, out)
    return out

def number_from(values, names):
    for name in names:
        value = values.get(name)
        if isinstance(value, (int, float)) and value == value:
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(',', '.'))
            except ValueError:
                pass
    return None

def delayed_quote(symbol):
    """Read the newest official Xetra post-trade minute file.

    The service can add fields over time, so matching and price extraction are
    deliberately tolerant of harmless schema changes.
    """
    page = fetch_url(DB_INDEX).decode("utf-8", "replace")
    links = re.findall(r'https://mfs\.deutsche-boerse\.com/api/download/[^"<> ]+\.json\.gz', page)
    if not links:
        raise RuntimeError("Kein aktuelles Xetra-Post-Trade-File gefunden")
    file_url = links[0]
    raw = gzip.decompress(fetch_url(file_url))
    payload = json.loads(raw.decode("utf-8-sig", "replace"))
    needle = symbol.upper().removesuffix(".DE")
    candidates = []

    def walk(node):
        if isinstance(node, dict):
            values = flatten_values(node)
            texts = [str(values.get(k, "")).upper() for k in ("symbol", "ticker", "mnemonic", "isin", "instrument", "securityid", "security")]
            matched = needle in texts or any(needle and needle in text for text in texts)
            price = number_from(values, ("price", "lastprice", "tradeprice", "last", "close", "value"))
            if matched and price is not None:
                candidates.append({"price": price, "record": node})
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(payload)
    if not candidates:
        raise RuntimeError("Asset im aktuellen Xetra-File nicht gefunden; ISIN oder Xetra-Kürzel versuchen")
    return {"source": "Deutsche Börse Xetra Post-Trade", "delayedMinutes": 15,
            "file": file_url.rsplit("/", 1)[-1], "price": candidates[-1]["price"],
            "record": candidates[-1]["record"]}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/delayed":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0].strip()
            if not symbol or len(symbol) > 40 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_^=" for c in symbol):
                self.send_error(400, "Ungueltiges Symbol")
                return
            try:
                payload = json.dumps(delayed_quote(symbol), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/history":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0].strip()
            if not symbol or len(symbol) > 40 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_^=" for c in symbol):
                self.send_error(400, "Ungueltiges Symbol")
                return
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1h&events=history&includeAdjustedClose=true"
            try:
                body = fetch_yahoo(url)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                print(f"Yahoo-Abruf fehlgeschlagen: {exc!r}")
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(payload)
            return
        super().do_GET()

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("SIGNALLAB_PORT", "8765"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"StockPicker by Andreas Metzner {APP_VERSION}: http://{host}:{port}/index.html")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
