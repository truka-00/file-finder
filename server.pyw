"""바탕화면 아이콘이 실행하는 파일. 검색 서버를 켜고 브라우저로 검색창을 연다.
이미 켜져 있으면 검색창만 다시 연다. 이 컴퓨터(127.0.0.1)에서만 접속된다."""
import json, os, secrets, subprocess, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import socket

import indexer

PORT = 8765
HERE = Path(__file__).parent
TOKEN = secrets.token_hex(16)
con = indexer.db()
lock = threading.Lock()
state = {"running": False, "done": 0, "current": ""}


def reindex():
    if state["running"]:
        return
    state.update(running=True, done=0, current="")

    def work():
        try:
            indexer.index(lambda d, name: state.update(done=d, current=name))
        finally:
            state["running"] = False
    threading.Thread(target=work, daemon=True).start()


def stale():
    row = con.execute("SELECT v FROM meta WHERE k='indexed_at'").fetchone()
    return not row or time.time() - float(row[0]) > 600


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _ok(self):  # 다른 사이트가 몰래 부르지 못하게
        return self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            if stale():
                reindex()
            html = (HERE / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            html = html.replace("__ROOTS__", json.dumps(list(indexer.ROOTS), ensure_ascii=False))
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if not self._ok():
            return self._send(403, {"error": "forbidden"})
        if u.path == "/api/search":
            with lock:
                rows = indexer.search(con, q.get("q", ""), q.get("root", ""), q.get("kind", ""))
            return self._send(200, rows)
        if u.path == "/api/related":
            with lock:
                rows = indexer.related(con, q.get("path", ""), q.get("kind", "gm"))
            return self._send(200, rows)
        if u.path == "/api/pii":
            with lock:
                rows = indexer.pii_list(con, q.get("root", ""))
            return self._send(200, rows)
        if u.path == "/api/status":
            with lock:
                n = con.execute("SELECT count(*) FROM files").fetchone()[0]
                row = con.execute("SELECT v FROM meta WHERE k='indexed_at'").fetchone()
            return self._send(200, {**state, "count": n, "indexed_at": float(row[0]) if row else None})
        self._send(404, {})

    def do_POST(self):
        if not self._ok():
            return self._send(403, {"error": "forbidden"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/reindex":
            reindex()
            return self._send(200, {"ok": True})
        if self.path == "/api/open":
            p = body.get("path", "")
            with lock:
                known = con.execute("SELECT 1 FROM files WHERE path=?", (p,)).fetchone()
            if not known or not os.path.exists(p):
                return self._send(404, {"error": "파일이 없어요. 옮겨졌거나 지워졌을 수 있어요."})
            if body.get("mode") == "folder":
                subprocess.Popen(["explorer", "/select,", p])
            else:
                os.startfile(p)
            return self._send(200, {"ok": True})
        self._send(404, {})


def running():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def open_window(url):
    """주소창 없는 앱 창으로 연다 (엣지·크롬 앱 모드). 둘 다 없으면 보통 브라우저."""
    for exe in [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]:
        exe = os.path.expandvars(exe)
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1100,860"])
            return
    webbrowser.open(url)


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}/"
    if running():
        open_window(url)
    else:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        threading.Timer(0.3, open_window, [url]).start()
        srv.serve_forever()
