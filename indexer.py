"""파일 찾기 — 색인 만들기와 검색.

명령줄:  python indexer.py index        바뀐 파일만 다시 색인
         python indexer.py search 검색어  터미널에서 바로 검색
"""
import json, os, re, sys, sqlite3, struct, time, zipfile, zlib
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).parent
LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME)) / "파일찾기"


def expand(p):
    return Path(os.path.expandvars(os.path.expanduser(p)))


# 설정은 config.json (없으면 config.example.json) — 사람마다 다른 폴더·규칙은 여기에
_cfg = HERE / "config.json"
CONFIG = json.loads((_cfg if _cfg.exists() else HERE / "config.example.json").read_text(encoding="utf-8"))
ROOTS = {k: expand(v) for k, v in CONFIG["검색할 폴더"].items()}
# 개발 폴더(코드 저장소)와 부품 폴더는 건너뛴다
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "__pycache__", ".venv", "venv",
             ".firebase", ".cache", ".turbo", "out"} | set(CONFIG.get("검색에서 뺄 폴더", []))
OCR_PAGES = CONFIG.get("글자 인식 최대 쪽수", 5)
# 민감한 문서(학교폭력 사안 등)는 이름으로만 찾고 내용은 읽지 않는다
_NOREAD = CONFIG.get("내용을 읽지 않을 것", {})
NOREAD_DIRS = set(_NOREAD.get("폴더", []))
NOREAD_WORDS = [w.lower() for w in _NOREAD.get("이름 낱말", [])]


def readable(p: Path, root: Path) -> bool:
    if NOREAD_DIRS & set(p.relative_to(root).parts[:-1]):
        return False
    return not any(w in p.name.lower() for w in NOREAD_WORDS)

TEXT_EXT = {"hwp", "hwpx", "pdf", "docx", "xlsx", "xlsm", "pptx", "odt", "ods", "odp", "txt", "csv", "html", "htm"}
NAME_EXT = {"doc", "xls", "ppt", "show", "cell", "jpg", "jpeg", "png", "gif", "heic", "bmp",
            "mp4", "mov", "avi", "wmv", "mp3", "m4a", "wav", "zip", "7z", "egg", "alz", "rar"}
MAX_TEXT = 200_000

DB_PATH = LOCAL / "index.db"


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, name TEXT, ext TEXT, root TEXT, folder TEXT,
        size INTEGER, mtime REAL, text TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con


# ---------- 글자 뽑기 ----------

def _xml_text(data: bytes) -> str:
    s = data.decode("utf-8", "ignore")
    s = re.sub(r"<(/?)(?:\w+:)?(p|br|tr|row|si)\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", s)


def _zip_text(path, pattern):
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.fullmatch(pattern, n))
        return "\n".join(_xml_text(z.read(n)) for n in names)


def _hwp_text(path):
    import olefile
    with olefile.OleFileIO(path) as ole:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack("<I", header[36:40])[0]
        compressed, encrypted = flags & 1, flags & 2
        sections = sorted((s for s in ole.listdir() if s[0] == "BodyText"),
                          key=lambda s: int(re.sub(r"\D", "", s[1]) or 0))
        out = []
        if not encrypted:
            for s in sections:
                data = ole.openstream(s).read()
                if compressed:
                    data = zlib.decompress(data, -15)
                out.append(_hwp_records(data))
        text = "\n".join(out)
        if not text.strip() and ole.exists("PrvText"):  # 배포용 문서는 미리보기 글자만
            text = ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
        return text


def _hwp_records(data):
    i, out = 0, []
    while i + 4 <= len(data):
        h = struct.unpack("<I", data[i:i + 4])[0]
        tag, size = h & 0x3FF, (h >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:
            size = struct.unpack("<I", data[i:i + 4])[0]
            i += 4
        if tag == 67:  # 문단 글자
            out.append(_hwp_para(data[i:i + size]))
        i += size
    return "\n".join(out)


def _hwp_para(b):
    chars, j = [], 0
    while j + 2 <= len(b):
        c = struct.unpack("<H", b[j:j + 2])[0]
        if c < 32:
            if c in (10, 13):
                chars.append("\n")
            elif c == 9:
                chars.append(" ")
            if c not in (0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31):
                j += 14  # 8칸짜리 조판 부호
        else:
            chars.append(chr(c))
        j += 2
    return "".join(chars)


def _pdf_text(path):
    import pymupdf
    out, n = [], 0
    with pymupdf.open(path) as doc:
        for page in doc:
            t = page.get_text()
            out.append(t)
            n += len(t)
            if n > MAX_TEXT:
                break
        if n < 30 * min(len(doc), OCR_PAGES) and OCR_PAGES:  # 스캔본 — 글자 인식
            pngs = [doc[i].get_pixmap(dpi=200).tobytes("png") for i in range(min(len(doc), OCR_PAGES))]
            out.append(_ocr(pngs))
    return "\n".join(out)


_engine = None


def _ocr(pngs):
    """윈도우에 들어 있는 글자 인식(한국어)을 쓴다. 없으면 빈 글자."""
    global _engine
    try:
        import asyncio
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    except ImportError:
        return ""
    if _engine is None:
        _engine = OcrEngine.try_create_from_language(Language("ko")) or False
    if not _engine:
        return ""

    async def one(png):
        s = InMemoryRandomAccessStream()
        w = DataWriter(s)
        w.write_bytes(png)
        await w.store_async()
        w.detach_stream()
        s.seek(0)
        bmp = await (await BitmapDecoder.create_async(s)).get_software_bitmap_async()
        r = await _engine.recognize_async(bmp)
        return "\n".join(l.text for l in r.lines)

    async def all_pages():
        return [await one(p) for p in pngs]
    return "\n".join(asyncio.run(all_pages()))


def extract(path: Path, ext: str) -> str:
    if ext == "hwp":
        return _hwp_text(path)
    if ext == "hwpx":
        return _zip_text(path, r"Contents/section\d+\.xml")
    if ext == "pdf":
        return _pdf_text(path)
    if ext == "docx":
        return _zip_text(path, r"word/document\.xml")
    if ext in ("xlsx", "xlsm"):
        return _zip_text(path, r"xl/sharedStrings\.xml|xl/worksheets/sheet\d+\.xml")
    if ext == "pptx":
        return _zip_text(path, r"ppt/slides/slide\d+\.xml")
    if ext in ("odt", "ods", "odp"):
        return _zip_text(path, r"content\.xml")
    raw = path.read_bytes()[:MAX_TEXT * 2]
    for enc in ("utf-8", "cp949"):
        try:
            s = raw.decode(enc)
            break
        except UnicodeDecodeError:
            s = raw.decode("utf-8", "ignore")
    if ext in ("html", "htm"):
        s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
        s = re.sub(r"<[^>]+>", " ", s)
    return s


# ---------- 색인 ----------

def walk():
    for root_name, root in ROOTS.items():
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for f in filenames:
                ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                if (ext in TEXT_EXT or ext in NAME_EXT) and not f.startswith("~$"):
                    yield root_name, root, Path(dirpath) / f, ext


def index(progress=None):
    con = db()
    known = {p: (s, m) for p, s, m in con.execute("SELECT path, size, mtime FROM files")}
    # 읽지 않기로 한 파일의 내용이 남아 있으면 지운다 (설정을 나중에 바꾼 경우)
    for sp, rt in con.execute("SELECT path, root FROM files WHERE text != ''").fetchall():
        if rt in ROOTS and not readable(Path(sp), ROOTS[rt]):
            con.execute("UPDATE files SET text='' WHERE path=?", (sp,))
    con.commit()
    seen, done = set(), 0
    for root_name, root, p, ext in walk():
        sp = str(p)
        seen.add(sp)
        try:
            st = p.stat()
        except OSError:
            continue
        if known.get(sp) == (st.st_size, st.st_mtime):
            continue
        text = ""
        if ext in TEXT_EXT and readable(p, root):
            try:
                text = re.sub(r"\s*\n\s*", "\n", extract(p, ext))[:MAX_TEXT]
                text = re.sub(r"[\ud800-\udfff\x00]", "", text)  # 깨진 글자 빼기
            except Exception:
                text = ""  # 암호 걸린 파일 등은 이름으로만 찾는다
        folder = str(p.parent.relative_to(root)).replace("\\", " › ")
        con.execute("REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)",
                    (sp, p.name, ext, root_name, "" if folder == "." else folder,
                     st.st_size, st.st_mtime, text))
        done += 1
        if done % 25 == 0:
            con.commit()
            if progress:
                progress(done, p.name)
    gone = set(known) - seen
    con.executemany("DELETE FROM files WHERE path=?", [(g,) for g in gone])
    con.execute("REPLACE INTO meta VALUES('indexed_at', ?)", (str(time.time()),))
    con.commit()
    return done, len(gone)


# ---------- 검색 ----------

# 줄임말 → 함께 찾을 말
SYNONYMS = {
    "학폭": ["학교폭력"], "수평": ["수행평가"], "수행": ["수행평가"], "회의록": ["회의록", "협의록"],
    "교과협": ["교과협의", "교과회의"], "가정통신문": ["가정통신문", "안내문"], "명단": ["명단", "명렬"],
    "연락망": ["연락망", "비상연락"], "생기부": ["생활기록부", "생기부", "세특"], "품의": ["품의"],
}


def search(con, q, root="", kind="", limit=200):
    terms = [t for t in q.lower().split() if t]
    if not terms:
        return []
    where, args = [], []
    for t in terms:
        alts = [t] + SYNONYMS.get(t, [])
        ors = []
        for a in alts:
            ors.append("(lower(name) LIKE ? OR lower(folder) LIKE ? OR lower(text) LIKE ?)")
            args += [f"%{a}%"] * 3
        where.append("(" + " OR ".join(ors) + ")")
    if root:
        where.append("root=?")
        args.append(root)
    if kind:
        exts = KINDS.get(kind, [])
        where.append(f"ext IN ({','.join('?' * len(exts))})")
        args += exts
    rows = con.execute(f"SELECT path,name,ext,root,folder,size,mtime,text FROM files WHERE {' AND '.join(where)}", args).fetchall()
    out = []
    for path, name, ext, rt, folder, size, mtime, text in rows:
        lname, lfolder, ltext = name.lower(), folder.lower(), (text or "").lower()
        score, snippet = 0, ""
        for t in terms:
            alts = [t] + SYNONYMS.get(t, [])
            if any(a in lname for a in alts):
                score += 10
            elif any(a in lfolder for a in alts):
                score += 3
            for a in alts:
                k = ltext.find(a)
                if k >= 0:
                    score += 1 + min(ltext.count(a), 5) * 0.2
                    if not snippet:
                        s = max(0, k - 45)
                        snippet = ("…" if s else "") + text[s:k + len(a) + 70].replace("\n", " ") + "…"
                    break
        out.append({"path": path, "name": name, "ext": ext, "root": rt, "folder": folder,
                    "size": size, "mtime": mtime, "snippet": snippet, "score": score})
    out.sort(key=lambda r: (-r["score"], -r["mtime"]))
    return out[:limit]


KINDS = {
    "한글": ["hwp", "hwpx"], "PDF": ["pdf"], "엑셀": ["xlsx", "xlsm", "xls", "csv", "cell", "ods"],
    "사진·영상": ["jpg", "jpeg", "png", "gif", "heic", "bmp", "mp4", "mov", "avi", "wmv"],
}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        for r in search(db(), " ".join(sys.argv[2:]), limit=30):
            print(f"{r['score']:5.1f}  {r['path']}")
            if r["snippet"]:
                print("       " + r["snippet"][:120])
    else:
        t = time.time()
        n, g = index(lambda d, name: print(f"{d}개… {name}", flush=True))
        print(f"새로 색인 {n}개, 지운 것 {g}개, {time.time() - t:.0f}초")
