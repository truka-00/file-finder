"""다운로드 폴더 자동 정리 — 파일 이름의 낱말을 보고 바탕화면 번호 폴더로 옮긴다.

python sorter.py           정리하기 (작업 스케줄러가 하루 한 번 실행)
python sorter.py --dry     옮기지 않고 어디로 갈지만 보여주기
python sorter.py undo      마지막 정리를 되돌리기
"""
import hashlib, shutil, sys, time
from pathlib import Path

from indexer import CONFIG, LOCAL, expand

C = CONFIG["다운로드 정리"]
DESK = expand(C["옮길 곳"])
SRC = expand(C["정리할 폴더"])
LOG = LOCAL / "정리기록.tsv"
WAIT = C["분류 대기 폴더"]
RULES = [(r["폴더"], r["낱말"]) for r in C["규칙"]]

INSTALL_EXT = {"exe", "msi", "msu", "winmd"}
MEDIA_EXT = {"jpg", "jpeg", "png", "gif", "heic", "webp", "bmp", "mp4", "mov", "avi", "m4a", "mp3", "wav"}
SKIP_EXT = {"crdownload", "part", "tmp", "download"}


def classify(p: Path) -> str:
    name = p.name.lower()
    ext = name.rsplit(".", 1)[-1] if "." in name and p.is_file() else ""
    if name.startswith("~$") or ext == "base64":
        return C["임시 파일 폴더"]
    if ext in INSTALL_EXT:
        return C["설치 파일 폴더"]
    for dest, words in RULES:
        if any(w.lower() in name for w in words):
            return dest
    if ext in MEDIA_EXT:
        return C["사진·영상 폴더"]
    return WAIT


def _hash(p: Path):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_exists(p: Path, folder: Path):
    if not p.is_file() or not folder.exists():
        return False
    size = p.stat().st_size
    for q in folder.iterdir():
        if q.is_file() and q.stat().st_size == size and _hash(q) == _hash(p):
            return True
    return False


def _free_name(folder: Path, name: str) -> Path:
    t = folder / name
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    n = 2
    while t.exists():
        t = folder / (f"{stem} ({n}).{ext}" if dot else f"{name} ({n})")
        n += 1
    return t


def plan():
    out = []
    now = time.time()
    for p in sorted(SRC.iterdir(), key=lambda x: x.name):
        name = p.name
        if name.lower() == "desktop.ini" or name.rsplit(".", 1)[-1].lower() in SKIP_EXT:
            continue
        if now - p.stat().st_mtime < 600:  # 방금 받은 파일은 다음 번에
            continue
        out.append((p, classify(p)))
    return out


def run(dry=False):
    moves = []
    for p, dest in plan():
        folder = DESK / dest
        if not dry and _same_exists(p, folder):
            folder = DESK / C["중복 파일 폴더"]
        if dry:
            moves.append((p, folder / p.name))
            continue
        folder.mkdir(parents=True, exist_ok=True)
        target = _free_name(folder, p.name)
        try:
            shutil.move(str(p), str(target))
            moves.append((p, target))
        except OSError:
            pass  # 열려 있는 파일은 다음 번에
    if not dry and moves:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with open(LOG, "a", encoding="utf-8") as f:
            for a, b in moves:
                f.write(f"{stamp}\t{a}\t{b}\n")
    return moves


def undo():
    if not LOG.exists():
        return 0
    lines = LOG.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0
    last = lines[-1].split("\t")[0]
    keep, n = [], 0
    for line in lines:
        stamp, a, b = line.split("\t")
        if stamp == last and Path(b).exists() and not Path(a).exists():
            shutil.move(b, a)
            n += 1
        elif stamp != last:
            keep.append(line)
    LOG.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return n


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "undo" in sys.argv:
        print(f"{undo()}개를 다운로드로 되돌렸어요")
        sys.exit()
    dry = "--dry" in sys.argv
    moves = run(dry)
    for a, b in moves:
        print(f"{b.parent.relative_to(DESK)}\t{a.name}")
    if not dry:
        import indexer
        indexer.index()
