"""
auth.py — одноразова авторизація в Spotify (OAuth 2.0 Authorization Code flow).
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

import spotify_client as sp

REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = " ".join([
    "playlist-read-private",    # get_playlist, пошук власних плейлистів
    "playlist-modify-private",  # створення/дописування приватних плейлистів
    "playlist-modify-public",   # потрібен DELETE /me/library для плейлистів (reset.py)
    "user-library-modify",      # DELETE /me/library
])


def main() -> None:
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("Заповни SPOTIFY_CLIENT_ID і SPOTIFY_CLIENT_SECRET у .env")

    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Готово, можна закрити вкладку.".encode())

        def log_message(self, *args):
            pass

    u = urllib.parse.urlparse(REDIRECT_URI)
    server = HTTPServer((u.hostname, u.port), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state})
    print("Відкриваю браузер. Якщо не відкрився — перейди вручну:\n", url)
    webbrowser.open(url)
    thread.join()
    server.server_close()

    if result.get("state") != state:
        raise SystemExit("state не збігся — спробуй ще раз.")
    if "error" in result:
        raise SystemExit(f"Spotify повернув помилку: {result['error']}")

    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = httpx.post(sp.TOKEN_URL, headers={"Authorization": f"Basic {basic}"}, timeout=20,
                   data={"grant_type": "authorization_code", "code": result["code"],
                         "redirect_uri": REDIRECT_URI})
    r.raise_for_status()
    sp.save_token(r.json())

    user = sp.me()
    print(f"OK. Авторизовано як: {user.get('display_name') or user.get('id')}. "
          f"Токен збережено в {sp.TOKEN_FILE.name}")


if __name__ == "__main__":
    main()
