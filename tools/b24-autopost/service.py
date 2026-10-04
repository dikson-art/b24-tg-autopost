#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автопостинг «Вопросы с лекций»: Битрикс24 (Диск) -> Telegram-канал.

Что делает:
  1) Находит папку с видео в Битрикс24 (входящий вебхук, право «Диск»).
  2) Смотрит подпапки лекций и находит новые видеофайлы (сверяет с state.json — без дублей).
  3) Скачивает новый клип.
  4) Сопоставляет его со страницей лекции на medinfoservice.org (по фамилии лектора).
  5) Если клип больше лимита Telegram — сжимает его (ffmpeg) до заданного размера.
  6) Собирает пост (подпись + кнопка «Смотреть на сайте») и публикует в Telegram-канал.

Команды:
  python service.py --check       проверить доступы (вебхук Б24 + бот TG)
  python service.py --test-site   протестировать сопоставление с сайтом (без Б24/TG)
  python service.py --dry-run     полный прогон без публикации (показать, что было бы отправлено)
  python service.py               боевой запуск (публикует новые клипы)
  python service.py --max-posts 1 ограничить число постов за запуск (0 = без ограничения)

Зависимости: стандартная библиотека Python 3.9+; ffmpeg/ffprobe — для сжатия больших клипов.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "logs", "publish.log")
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) B24-TG-Publisher/0.1"
FFMPEG = os.environ.get("B24TG_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("B24TG_FFPROBE", "ffprobe")

RU_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def translit(text):
    return "".join(RU_LAT.get(ch.lower(), ch) for ch in text)


def load_config(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    example = os.path.join(BASE_DIR, "config.example.json")
    if os.path.exists(example):
        log("config.json не найден — использую config.example.json (только для тестов).")
        with open(example, encoding="utf-8") as f:
            return json.load(f)
    return {"bitrix": {}, "telegram": {}, "site": {"base_url": "https://medinfoservice.org"}, "state_file": "state.json"}


def http_request(url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    req.add_header("User-Agent", DEFAULT_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_err_text(exc):
    try:
        body = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
            return data.get("description") or data.get("error_description") or body[:300]
        except Exception:
            return body[:300]
    except Exception:
        return str(exc)


# ----------------------------- Bitrix24 -----------------------------

def b24_call(cfg, method, params=None):
    webhook = (cfg.get("bitrix") or {}).get("webhook_url") or ""
    if not webhook:
        raise RuntimeError("Не заполнен bitrix.webhook_url в config.json")
    url = webhook.rstrip("/") + "/" + method + ".json"
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    try:
        raw = http_request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Bitrix24 HTTP: %s" % _http_err_text(exc))
    resp = json.loads(raw.decode("utf-8", "replace"))
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError("Bitrix24: %s — %s" % (resp.get("error"), resp.get("error_description", "")))
    return resp.get("result") if isinstance(resp, dict) else resp


def b24_list_children(cfg, kind, node_id):
    method = "disk.storage.getchildren" if kind == "storage" else "disk.folder.getchildren"
    items = []
    seen = set()
    start = 0
    while True:
        page = b24_call(cfg, method, {"id": node_id, "start": start}) or []
        fresh = 0
        for item in page:
            iid = item.get("ID")
            if iid in seen:
                continue
            seen.add(iid)
            items.append(item)
            fresh += 1
        if len(page) < 50 or fresh == 0:
            break
        start += 50
        if start > 10000:
            log("  ... слишком много объектов в %s %s — пагинация остановлена" % (kind, node_id))
            break
    return items


def b24_find_folder(cfg):
    bcfg = cfg.get("bitrix") or {}
    explicit = bcfg.get("folder_id")
    if explicit:
        return int(explicit)
    target = (bcfg.get("folder_name") or "Вопросы с лекций").strip().lower()
    storage_id = bcfg.get("storage_id")
    if storage_id:
        storages = [{"ID": storage_id}]
    else:
        storages = b24_call(cfg, "disk.storage.getlist") or []
    queue = [("storage", s.get("ID"), 0) for s in storages if s.get("ID")]
    visited = set()
    while queue:
        kind, node_id, depth = queue.pop(0)
        if (kind, node_id) in visited or depth > 4:
            continue
        visited.add((kind, node_id))
        try:
            children = b24_list_children(cfg, kind, node_id)
        except Exception as exc:
            log("  ... не удалось получить содержимое %s %s: %s" % (kind, node_id, exc))
            continue
        for item in children:
            if (item.get("TYPE") or "").upper() == "FOLDER":
                if (item.get("NAME") or "").strip().lower() == target:
                    return int(item.get("ID"))
                queue.append(("folder", item.get("ID"), depth + 1))
    raise RuntimeError("Папка «%s» не найдена — укажите bitrix.folder_id в config.json" % target)


def _question_num(item):
    m = re.search(r"\((\d+)\)", item.get("NAME") or "")
    return int(m.group(1)) if m else 9999


def _sort_time(item):
    t = item.get("CREATE_TIME")
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str) and t:
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def b24_subfolders(cfg, folder_id):
    items = b24_list_children(cfg, "folder", folder_id)
    subs = [i for i in items if (i.get("TYPE") or "").upper() == "FOLDER"]
    subs.sort(key=_sort_time)
    return subs


def b24_files(cfg, folder_id):
    items = b24_list_children(cfg, "folder", folder_id)
    files = [i for i in items if (i.get("TYPE") or "").upper() == "FILE"]
    files = [f for f in files if (f.get("NAME") or "").lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm"))]
    files.sort(key=_question_num)
    return files


def b24_download(cfg, file_id, dest_path, attempts=3):
    last_exc = None
    for attempt in range(1, attempts + 1):
        info = b24_call(cfg, "disk.file.get", {"id": file_id}) or {}
        url = info.get("DOWNLOAD_URL")
        if not url:
            raise RuntimeError("Нет DOWNLOAD_URL у файла %s" % file_id)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", DEFAULT_UA)
            start = time.time()
            total = 0
            with urllib.request.urlopen(req, timeout=90) as resp, open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
                    el = time.time() - start
                    if el > 120 and total < 1024 * 1024:
                        raise RuntimeError("слишком медленно: %.0f КБ за %.0f с" % (total / 1024.0, el))
            if total <= 0:
                raise RuntimeError("получено 0 байт")
            return info
        except Exception as exc:
            last_exc = exc
            log("  ... загрузка не удалась (попытка %d/%d): %s" % (attempt, attempts, exc))
            try:
                os.remove(dest_path)
            except OSError:
                pass
            if attempt < attempts:
                time.sleep(45)
    raise RuntimeError("Не удалось скачать файл %s: %s" % (file_id, last_exc))


# ------------------------------- Сайт -------------------------------

def site_load_links(base_url):
    url = base_url.rstrip("/") + "/"
    raw = http_request(url, timeout=60).decode("utf-8", "replace")
    links = set()
    for m in re.finditer(r'href="([^"]+)"', raw):
        href = m.group(1)
        if href.startswith("/") and not href.startswith("//"):
            href = urllib.parse.urljoin(url, href)
        if "medinfoservice.org" in href:
            links.add(href.split("#")[0])
    return sorted(links)


def site_find_lecture_page(cfg, subfolder_name):
    manual = ((cfg.get("site") or {}).get("manual_map") or {})
    if subfolder_name in manual:
        return manual[subfolder_name]
    base_url = (cfg.get("site") or {}).get("base_url") or "https://medinfoservice.org"
    links = site_load_links(base_url)
    surname = subfolder_name.split()[0].strip()
    slug = translit(surname).lower()
    cands = [u for u in links if slug and slug in u.lower()]
    if not cands:
        return None

    def score(u):
        ul = u.lower()
        s = 0
        if "povtornmo" in ul:
            s += 1
        elif "povtor" in ul:
            s += 3
        return (s, len(ul))

    cands.sort(key=score, reverse=True)
    if len(cands) > 1:
        log("  ... для «%s» найдено несколько страниц, беру: %s" % (subfolder_name, cands[0]))
    return cands[0]


def site_fetch_lecture_info(url):
    raw = http_request(url, timeout=60).decode("utf-8", "replace")
    title = None
    lector = None
    m = re.search(r"Запись (?:вебинара|конференции|доклада)\s*:?\s*«([^»]+)»", raw)
    if m:
        title = m.group(1).strip()
    if not title:
        m = re.search(r'<meta property="og:title" content="([^"]+)"', raw)
        if m:
            title = m.group(1).strip()
    m = re.search(r"Лектор:\s*([^<]{3,300})", raw)
    if m:
        lector = re.split(r"\s+[—-]\s+", m.group(1).strip())[0].strip()
    return {"title": title, "lector": lector, "url": url}


def build_caption(info, question_num):
    t = info.get("title") or "Лекция"
    l = info.get("lector") or ""
    u = info.get("url") or ""
    if question_num:
        head = "🎥 Вопрос №%s с лекции «%s»" % (question_num, t)
    else:
        head = "🎥 Ответ на вопрос с лекции «%s»" % t
    lines = [head]
    if l:
        lines += ["", "Лектор: " + l]
    lines += ["", "▶️ Полная запись лекции — на сайте: " + u]
    caption = "\n".join(lines)
    if len(caption) > 1024:
        caption = caption[:1020] + " …"
        log("  ... подпись обрезана до 1024 символов")
    return caption


# --------------------------- Видео и сжатие ---------------------------

def video_duration(path):
    """Длительность видео (сек) через ffprobe."""
    res = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=120,
    )
    out = (res.stdout or "").strip().replace(",", ".")
    try:
        return float(out)
    except ValueError:
        return 0.0


def compress_for_upload(cfg, path, max_mb, target_mb):
    """Если файл больше лимита — перекодируем ffmpeg до target_mb (H.264/AAC)."""
    size = os.path.getsize(path)
    if size <= max_mb * 1024 * 1024:
        return path
    dur = video_duration(path)
    if dur <= 0:
        raise RuntimeError("ffprobe не смог определить длительность: %s" % path)
    out = os.path.splitext(path)[0] + ".tg.mp4"
    target_bytes = target_mb * 1024 * 1024
    attempt = 0
    while True:
        attempt += 1
        factor = 0.97 if attempt == 1 else 0.90
        total_kbps = target_bytes * 8.0 / dur / 1000.0 * factor
        audio_kbps = 128
        video_kbps = max(int(total_kbps - audio_kbps - 8), 150)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            "-i", path,
            "-c:v", "libx264", "-preset", "medium",
            "-b:v", "%dk" % video_kbps,
            "-maxrate", "%dk" % int(video_kbps * 1.05),
            "-bufsize", "%dk" % int(video_kbps * 2),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "%dk" % audio_kbps, "-ac", "2",
            "-movflags", "+faststart",
            out,
        ]
        log("  сжатие (попытка %d): цель ~%d МБ, видео ~%d кбит/с" % (attempt, target_mb, video_kbps))
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if res.returncode != 0:
            raise RuntimeError("ffmpeg: %s" % (res.stderr or "")[-600:])
        newsize = os.path.getsize(out)
        log("  сжатие: %.1f МБ -> %.1f МБ" % (size / 1048576.0, newsize / 1048576.0))
        if newsize <= max_mb * 1024 * 1024:
            return out
        if attempt >= 2:
            log("  !! сжатый файл всё ещё больше лимита (%.1f МБ)" % (newsize / 1048576.0))
            return out
        log("  всё ещё выше лимита — повтор с меньшим битрейтом…")


# ----------------------------- Telegram -----------------------------

def _multipart(fields, files):
    boundary = "----b24tg" + hashlib.md5(str(time.time()).encode()).hexdigest()
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8"))
    for k, (fn, content, ctype) in files.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (boundary, k, fn, ctype)).encode("utf-8"))
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return boundary, b"".join(parts)


def tg_call(cfg, method, fields=None, files=None):
    tg = cfg.get("telegram") or {}
    tok = tg.get("bot_token")
    base = (tg.get("api_base") or "https://api.telegram.org").rstrip("/")
    if not tok:
        raise RuntimeError("Не заполнен telegram.bot_token в config.json")
    if files:
        boundary, body = _multipart(fields or {}, files)
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
    else:
        body = json.dumps(fields or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    try:
        raw = http_request("%s/bot%s/%s" % (base, tok, method), data=body, headers=headers, timeout=900)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Telegram: %s" % _http_err_text(exc))
    resp = json.loads(raw.decode("utf-8", "replace"))
    if not resp.get("ok"):
        raise RuntimeError("Telegram: %s" % resp.get("description"))
    return resp.get("result")


def tg_send_video(cfg, path, caption, button_text, button_url):
    tg = cfg.get("telegram") or {}
    with open(path, "rb") as f:
        content = f.read()
    fields = {
        "chat_id": str(tg.get("chat_id") or ""),
        "caption": caption,
        "supports_streaming": "true",
        "disable_notification": "true" if tg.get("disable_notification") else "false",
    }
    if button_url:
        fields["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": button_text or "Смотреть на сайте", "url": button_url}]]},
            ensure_ascii=False,
        )
    return tg_call(cfg, "sendVideo", fields=fields, files={"video": (os.path.basename(path), content, "video/mp4")})


# ------------------------------ State -------------------------------

def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"files": {}}


def save_state(path, state):
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------ Команды ------------------------------

def cmd_check(cfg):
    print("== Проверка доступов ==")
    ok = True
    webhook = ((cfg.get("bitrix") or {}).get("webhook_url") or "").strip()
    if webhook:
        try:
            storages = b24_call(cfg, "disk.storage.getlist") or []
            print("Bitrix24: ок, хранилищ доступно — %d" % len(storages))
            fid = b24_find_folder(cfg)
            subs = b24_subfolders(cfg, fid)
            print("Папка найдена: ID=%s, подпапок: %d" % (fid, len(subs)))
            for s in subs[:10]:
                print("   - %s" % s.get("NAME"))
        except Exception as exc:
            ok = False
            print("Bitrix24: ОШИБКА — %s" % exc)
    else:
        ok = False
        print("Bitrix24: вебхук не заполнен (bitrix.webhook_url)")
    tok = ((cfg.get("telegram") or {}).get("bot_token") or "").strip()
    if tok:
        try:
            me = tg_call(cfg, "getMe") or {}
            print("Telegram: ок, бот @%s" % me.get("username"))
        except Exception as exc:
            ok = False
            print("Telegram: ОШИБКА — %s" % exc)
    else:
        ok = False
        print("Telegram: токен бота не заполнен (telegram.bot_token)")
    chat_id = str((cfg.get("telegram") or {}).get("chat_id") or "").strip()
    if chat_id and tok:
        try:
            me = tg_call(cfg, "getMe") or {}
            m = tg_call(cfg, "getChatMember", {"chat_id": chat_id, "user_id": me.get("id")}) or {}
            st = m.get("status")
            if st in ("administrator", "creator"):
                print("Канал: ок — бот администратор (%s)" % chat_id)
            else:
                ok = False
                print("Канал: бот в статусе «%s» — нужны права администратора на публикацию" % st)
        except Exception as exc:
            ok = False
            print("Канал: ОШИБКА — %s (бот добавлен в канал и назначен админом?)" % exc)
    return 0 if ok else 1


def cmd_test_site(cfg):
    samples = [("Кравчук 28.09", 9), ("Гуров 29.09", 3), ("Зайцева 28.09", 1)]
    lines = []
    for name, num in samples:
        lines.append("=" * 70)
        lines.append(name)
        try:
            url = site_find_lecture_page(cfg, name)
        except Exception as exc:
            lines.append("ОШИБКА: %s" % exc)
            continue
        if not url:
            lines.append("Страница не найдена")
            continue
        info = site_fetch_lecture_info(url)
        lines.append("URL:    %s" % url)
        lines.append("TITLE:  %s" % info.get("title"))
        lines.append("LECTOR: %s" % info.get("lector"))
        lines.append("CAPTION:")
        lines.append(build_caption(info, num))
    text = "\n".join(lines)
    outp = os.path.join(BASE_DIR, "logs", "site-test.txt")
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print("\nСохранено: %s" % outp)
    return 0


def cmd_run(cfg, dry_run, max_posts_override=None):
    webhook = ((cfg.get("bitrix") or {}).get("webhook_url") or "").strip()
    if not webhook:
        log("Не заполнен bitrix.webhook_url в config.json — смотрите README.md")
        return 2
    tg = cfg.get("telegram") or {}
    if not dry_run and not (tg.get("bot_token") and tg.get("chat_id")):
        log("Для боевого режима нужны telegram.bot_token и telegram.chat_id")
        return 2

    folder_id = b24_find_folder(cfg)
    log("Папка найдена: ID=%s" % folder_id)
    subs = b24_subfolders(cfg, folder_id)
    state_path = os.path.join(BASE_DIR, cfg.get("state_file") or "state.json")
    state = load_state(state_path)
    limit = max_posts_override if max_posts_override is not None else int(tg.get("max_posts_per_run") or 0)
    max_mb = float(tg.get("max_upload_mb") or 49)
    target_mb = float(tg.get("compress_target_mb") or 46)
    posted = 0

    # Чередуем лекции «раундами» (З, Г, К, З, Г, К, …), чтобы посты распределялись равномерно.
    per_sub = [(sub, b24_files(cfg, sub.get("ID"))) for sub in subs]
    max_len = max([len(files) for _, files in per_sub], default=0)
    for i in range(max_len):
        for sub, files in per_sub:
            if i >= len(files):
                continue
            fl = files[i]
            key = "%s|%s|%s" % (sub.get("NAME"), fl.get("NAME"), fl.get("SIZE"))
            if key in state["files"]:
                continue
            if limit and posted >= limit:
                log("Достигнут лимит постов за запуск (%d) — остальное в следующий раз." % limit)
                save_state(state_path, state)
                return 0
            name = sub.get("NAME") or ""
            fname = fl.get("NAME") or ""
            log("Новый клип: %s / %s" % (name, fname))
            page_url = site_find_lecture_page(cfg, name)
            if not page_url:
                log("  !! не нашёл страницу лекции для «%s» — добавьте в site.manual_map" % name)
                continue
            info = site_fetch_lecture_info(page_url)
            qnum = _question_num(fl)
            qnum = qnum if qnum != 9999 else None
            caption = build_caption(info, qnum)
            if dry_run:
                try:
                    sz_mb = int(fl.get("SIZE") or 0) / 1048576.0
                except Exception:
                    sz_mb = 0.0
                extra = ""
                if sz_mb > max_mb:
                    extra = " (сейчас %.0f МБ — будет сжато до ~%d МБ)" % (sz_mb, target_mb)
                log("  [dry-run] было бы опубликовано%s:\n%s" % (extra, caption))
                posted += 1
                continue
            tmpdir = os.path.join(BASE_DIR, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            local = os.path.join(tmpdir, fname)
            log("  скачиваю…")
            try:
                b24_download(cfg, fl.get("ID"), local)
            except Exception as exc:
                log("  !! скачать не удалось: %s — остановка до следующего запуска" % exc)
                return 1
            send_path = local
            if os.path.getsize(local) > max_mb * 1024 * 1024:
                try:
                    send_path = compress_for_upload(cfg, local, max_mb, target_mb)
                except Exception as exc:
                    log("  !! сжатие не удалось (%s) — клип пропущен до следующего раза" % exc)
                    try:
                        os.remove(local)
                    except OSError:
                        pass
                    continue
            log("  отправляю в Telegram…")
            try:
                res = tg_send_video(cfg, send_path, caption, tg.get("button_text"), page_url)
            except Exception as exc:
                log("  !! отправка не удалась: %s" % exc)
                log("     Останов, чтобы не повторять по всем клипам. Проверьте права бота в канале.")
                for pth in {local, send_path}:
                    if pth:
                        try:
                            os.remove(pth)
                        except OSError:
                            pass
                return 1
            state["files"][key] = {
                "status": "published",
                "message_id": (res or {}).get("message_id") if isinstance(res, dict) else None,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "url": page_url,
            }
            save_state(state_path, state)
            posted += 1
            for pth in {local, send_path}:
                if pth:
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
            time.sleep(int(tg.get("post_delay_seconds") or 0))
    save_state(state_path, state)
    log("Готово. Новых публикаций: %d" % posted)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Автопостинг «Вопросы с лекций» Б24 -> Telegram")
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--check", action="store_true", help="проверить доступы (Б24 + TG)")
    ap.add_argument("--test-site", action="store_true", help="тест сопоставления с сайтом")
    ap.add_argument("--dry-run", action="store_true", help="прогон без публикации")
    ap.add_argument("--max-posts", type=int, default=None, help="лимит постов за запуск (0 = без лимита)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.check:
        return cmd_check(cfg)
    if args.test_site:
        return cmd_test_site(cfg)
    return cmd_run(cfg, dry_run=args.dry_run, max_posts_override=args.max_posts)


if __name__ == "__main__":
    sys.exit(main())
