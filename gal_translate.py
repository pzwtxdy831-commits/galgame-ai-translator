#!/usr/bin/env python3
"""
Galgame AI Translator (prototype)

Kirikiri / KAG visual novel script translation pipeline:
    extract  -> pull dialogue lines out of .ks scripts (XP3 archives or loose files)
    translate-> batch-translate with an LLM (Gemini API or any OpenAI-compatible server)
    check    -> flag suspicious lines (leftover kana, broken tags, too long)
    review   -> export / import a bilingual TSV for proofreading in Excel
    pack     -> build a patch XP3 that loads after the original game data

Pure Python standard library, no third-party dependencies.
"""

import argparse
import csv
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from typing import Dict, List, Optional, Tuple

__version__ = "0.1.0"

ENTRIES_FILE = "entries.json"
GLOSSARY_FILE = "glossary.json"
META_FILE = "project.json"
SCRIPTS_DIR = "scripts"

# ==============================================================================
# XP3 archive I/O (adapted from Galgame Voice Non-Stop, MIT)
# ==============================================================================

XP3_MAGIC = b'XP3\r\n \n\x1a\x8bg\x01'


def is_xp3(path: str) -> bool:
    try:
        with open(path, 'rb') as f:
            return f.read(11) == XP3_MAGIC
    except OSError:
        return False


def xp3_read_index(path: str) -> List[Tuple[str, List[Tuple[int, int, int, int]]]]:
    """Return [(name, segments)] where segments are (flag, offset, orig_size, arch_size)."""
    with open(path, 'rb') as f:
        if f.read(11) != XP3_MAGIC:
            raise ValueError(f"not an XP3 archive: {path}")
        idx_off = struct.unpack('<Q', f.read(8))[0]
        f.seek(idx_off)
        flag = f.read(1)[0]
        while flag == 0x80:  # XP3 2.x cushion header
            f.seek(idx_off + 1 + 8)
            idx_off = struct.unpack('<Q', f.read(8))[0]
            f.seek(idx_off)
            flag = f.read(1)[0]
        if flag & 7:
            c_len, _u_len = struct.unpack('<2Q', f.read(16))
            index = zlib.decompress(f.read(c_len))
        else:
            u_len, = struct.unpack('<Q', f.read(8))
            index = f.read(u_len)

    entries = []
    pos = 0
    while pos + 12 <= len(index):
        tag = index[pos:pos + 4]
        clen = struct.unpack('<Q', index[pos + 4:pos + 12])[0]
        chunk = index[pos + 12:pos + 12 + clen]
        pos += 12 + clen
        if tag != b'File':
            continue
        name, segments = None, []
        cpos = 0
        while cpos + 12 <= len(chunk):
            stag = chunk[cpos:cpos + 4]
            slen = struct.unpack('<Q', chunk[cpos + 4:cpos + 12])[0]
            scon = chunk[cpos + 12:cpos + 12 + slen]
            cpos += 12 + slen
            if stag == b'info' and len(scon) >= 22:
                name_len = struct.unpack('<H', scon[20:22])[0]
                name = scon[22:22 + name_len * 2].decode('utf-16le', errors='replace')
            elif stag == b'segm':
                for i in range(0, len(scon) - 27, 28):
                    segments.append(struct.unpack('<IQQQ', scon[i:i + 28]))
        if name is not None and segments:
            entries.append((name, segments))
    return entries


def xp3_read_file(f, segments) -> bytes:
    parts = []
    for seg_flag, offset, _orig, arch in segments:
        f.seek(offset)
        raw = f.read(arch)
        if seg_flag & 7:
            raw = zlib.decompress(raw)
        parts.append(raw)
    return b''.join(parts)


def xp3_write(files: List[Tuple[str, bytes]], out_path: str) -> None:
    tmp_path = out_path + ".tmp"
    index = bytearray()
    with open(tmp_path, 'wb') as out:
        out.write(XP3_MAGIC)
        out.write(b'\x00' * 8)
        for name, data in files:
            offset = out.tell()
            compressed = zlib.compress(data, 9)
            seg_flag, body = (1, compressed) if len(compressed) < len(data) else (0, data)
            out.write(body)
            name_u16 = name.encode('utf-16le')
            info = struct.pack('<IQQH', 0, len(data), len(body), len(name_u16) // 2) + name_u16
            segm = struct.pack('<IQQQ', seg_flag, offset, len(data), len(body))
            adlr = struct.pack('<I', zlib.adler32(data) & 0xffffffff)
            chunk = (b'info' + struct.pack('<Q', len(info)) + info
                     + b'segm' + struct.pack('<Q', len(segm)) + segm
                     + b'adlr' + struct.pack('<Q', 4) + adlr)
            index.extend(b'File' + struct.pack('<Q', len(chunk)) + chunk)
        idx_offset = out.tell()
        comp = zlib.compress(bytes(index), 9)
        out.write(b'\x01' + struct.pack('<Q', len(comp)) + struct.pack('<Q', len(index)) + comp)
        out.seek(11)
        out.write(struct.pack('<Q', idx_offset))
    os.replace(tmp_path, out_path)


def archive_load_order(game_dir: str) -> List[str]:
    """data.xp3 first, then other archives, then patch.xp3, patch2.xp3, ... (later wins)."""
    names = [n for n in os.listdir(game_dir) if n.lower().endswith('.xp3')
             and is_xp3(os.path.join(game_dir, n))]

    def key(n: str):
        low = n.lower()
        if low == 'data.xp3':
            return (0, 0, low)
        m = re.fullmatch(r'patch(\d*)\.xp3', low)
        if m:
            return (2, int(m.group(1) or 1), low)
        return (1, 0, low)

    return [os.path.join(game_dir, n) for n in sorted(names, key=key)]


# ==============================================================================
# KAG script parsing
# ==============================================================================

RE_TAG = re.compile(r'\[[^\[\]]*\]')
RE_EDGES = re.compile(r'^(\s*(?:\[[^\[\]]*\]\s*)*)(.*?)((?:\s*\[[^\[\]]*\])*\s*)$', re.S)
RE_JA = re.compile(r'[぀-ヿ一-鿿]')
RE_KANA = re.compile(r'[぀-ゟ゠-ヺヽ-ヿ]')
RE_PLACEHOLDER = re.compile(r'\{(\d+)\}')
RE_SCRIPT_START = re.compile(r'^\s*(?:@iscript\b|\[iscript\])', re.I)
RE_SCRIPT_END = re.compile(r'^\s*(?:@endscript\b|\[endscript\])', re.I)
RE_BRACKET_NAME = re.compile(r'^\s*【([^】]{1,20})】\s*$')


def decode_script(raw: bytes) -> Tuple[Optional[str], str]:
    if raw.startswith(b'\xfe\xfe'):
        return None, 'obfuscated'  # TJS2 simple-crypt / compressed text, not supported
    if raw.startswith(b'\xff\xfe'):
        return raw[2:].decode('utf-16le'), 'utf-16le'
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw[3:].decode('utf-8'), 'utf-8-sig'
    for enc in ('utf-8', 'cp932', 'gbk'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return None, 'unknown'


def split_text_line(line: str) -> Optional[dict]:
    """Split one KAG text line into prefix tags, translatable text and suffix tags.

    Inline tags in the middle (ruby, font changes...) become {1}, {2}... placeholders.
    Returns None for lines with nothing to translate.
    """
    stripped = line.strip()
    if not stripped or stripped[0] in '@;*#':
        return None
    m = RE_EDGES.match(line)
    prefix, body, suffix = m.group(1), m.group(2), m.group(3)
    if not RE_JA.search(RE_TAG.sub('', body)):
        return None
    tags: List[str] = []

    def to_placeholder(mt):
        tags.append(mt.group(0))
        return '{%d}' % len(tags)

    text = RE_TAG.sub(to_placeholder, body)
    return {"prefix": prefix, "text": text, "suffix": suffix, "tags": tags}


def speaker_of(line: str) -> Optional[str]:
    stripped = line.strip()
    if stripped.startswith('#') and len(stripped) > 1:
        return stripped[1:].split('/')[0].strip() or None
    m = RE_BRACKET_NAME.match(stripped)
    return m.group(1) if m else None


def parse_script(name: str, text: str) -> Tuple[List[dict], List[str]]:
    """Return (entries, speaker names) for one script."""
    entries, speakers = [], []
    in_script = False
    speaker = None
    for lineno, line in enumerate(text.splitlines(), 1):
        if in_script:
            in_script = not RE_SCRIPT_END.match(line)
            continue
        if RE_SCRIPT_START.match(line):
            in_script = True
            continue
        sp = speaker_of(line)
        if sp is not None:
            speaker = sp
            if sp not in speakers:
                speakers.append(sp)
            continue  # name lines are translated through the glossary at pack time
        part = split_text_line(line)
        if part is None:
            if not line.strip() or line.strip().startswith('*'):
                speaker = None  # new block / label resets the speaker
            continue
        entries.append({
            "id": f"{name}:{lineno}",
            "file": name,
            "line": lineno,
            "speaker": speaker,
            **part,
            "trans": None,
            "flags": [],
        })
        speaker = None
    return entries, speakers


# ==============================================================================
# Project files
# ==============================================================================

def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json(path: str, data) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def collect_scripts(src: str) -> Dict[str, bytes]:
    """Collect .ks scripts from a game folder, a single XP3 or a folder of loose scripts."""
    scripts: Dict[str, bytes] = {}
    if os.path.isfile(src):
        archives = [src] if is_xp3(src) else []
        if not archives and src.lower().endswith('.ks'):
            with open(src, 'rb') as f:
                scripts[os.path.basename(src)] = f.read()
            return scripts
    else:
        archives = archive_load_order(src)

    for arc in archives:
        try:
            index = xp3_read_index(arc)
            with open(arc, 'rb') as f:
                for name, segs in index:
                    if name.lower().endswith('.ks'):
                        scripts[name] = xp3_read_file(f, segs)
        except (ValueError, zlib.error, struct.error) as e:
            print(f"  ! skipped {os.path.basename(arc)} (encrypted or unsupported: {e})")

    if not archives and os.path.isdir(src):
        for root, _dirs, files in os.walk(src):
            for fn in files:
                if fn.lower().endswith('.ks'):
                    path = os.path.join(root, fn)
                    with open(path, 'rb') as f:
                        scripts[os.path.relpath(path, src).replace('\\', '/')] = f.read()
    return scripts


def script_store_path(project: str, name: str) -> str:
    safe = name.replace('\\', '/').strip('/').replace('/', '__')
    return os.path.join(project, SCRIPTS_DIR, safe + '.txt')


def cmd_extract(args) -> int:
    src = os.path.normpath(os.path.abspath(args.source))
    project = os.path.normpath(os.path.abspath(args.project))
    scripts = collect_scripts(src)
    if not scripts:
        print("No .ks scripts found. If the game uses encrypted XP3, unpack it with GARbro first "
              "and point this tool at the extracted folder.")
        return 1

    os.makedirs(os.path.join(project, SCRIPTS_DIR), exist_ok=True)
    all_entries, all_speakers, meta = [], [], {"version": 1, "source": src, "scripts": {}}
    for name in sorted(scripts):
        text, enc = decode_script(scripts[name])
        if text is None:
            print(f"  ! {name}: {enc} encoding, skipped")
            continue
        entries, speakers = parse_script(name, text)
        if not entries:
            continue
        with open(script_store_path(project, name), 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        meta["scripts"][name] = {"encoding": enc, "lines": len(entries)}
        all_entries.extend(entries)
        all_speakers.extend(s for s in speakers if s not in all_speakers)

    glossary_path = os.path.join(project, GLOSSARY_FILE)
    glossary = load_json(glossary_path, {})
    for sp in all_speakers:
        glossary.setdefault(sp, "")
    save_json(glossary_path, glossary)
    save_json(os.path.join(project, ENTRIES_FILE), all_entries)
    save_json(os.path.join(project, META_FILE), meta)

    chars = sum(len(e["text"]) for e in all_entries)
    print(f"Extracted {len(all_entries)} lines ({chars} chars) from {len(meta['scripts'])} scripts.")
    print(f"Found {len(all_speakers)} speaker names -> fill in {glossary_path} before translating.")
    return 0


# ==============================================================================
# LLM backends
# ==============================================================================

SYSTEM_PROMPT = (
    "You are localizing a Japanese visual novel into Simplified Chinese.\n"
    "Rules:\n"
    "- Translate naturally, keeping each character's tone; this is spoken dialogue and narration.\n"
    "- Keep placeholders such as {1} or {2} exactly as they are, in a sensible position.\n"
    "- Keep Japanese quotation brackets 「」『』 as they are.\n"
    "- Always use the glossary for names and terms.\n"
    "- Input is a JSON array of {\"id\", \"speaker\", \"text\"}. Reply with ONLY a JSON array of "
    "{\"id\", \"text\"} containing every id once.\n"
)

SAKURA_SYSTEM = ("你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
                 "并联系上下文正确使用人称代词，不擅自添加原文中没有的代词。")


def glossary_block(glossary: Dict[str, str]) -> str:
    pairs = [(k, v) for k, v in sorted(glossary.items()) if v]
    if not pairs:
        return "Glossary: (empty)\n"
    return "Glossary (Japanese -> Chinese):\n" + "\n".join(f"{k} -> {v}" for k, v in pairs) + "\n"


def http_post_json(url: str, body: dict, headers: Dict[str, str], retries: int = 4) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    hdrs = {"Content-Type": "application/json; charset=utf-8", **headers}
    delay = 2.0
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='replace')[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                print(f"  HTTP {e.code}, retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"connection failed: {e.reason}") from None
    raise RuntimeError("unreachable")


def parse_json_reply(text: str) -> Optional[list]:
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


class Usage:
    def __init__(self):
        self.prompt = self.cached = self.output = self.requests = 0

    def add(self, prompt: int, cached: int, output: int) -> None:
        self.prompt += prompt or 0
        self.cached += cached or 0
        self.output += output or 0
        self.requests += 1

    def report(self) -> str:
        rate = (self.cached / self.prompt * 100) if self.prompt else 0.0
        return (f"{self.requests} requests, prompt tokens {self.prompt} "
                f"(cached {self.cached}, {rate:.1f}%), output tokens {self.output}")


class Backend:
    """Translate a batch: items = [{"id", "speaker", "text"}] -> {id: translation}."""

    def __init__(self, glossary: Dict[str, str]):
        self.glossary = glossary
        self.usage = Usage()

    def translate(self, items: List[dict]) -> Dict[str, str]:
        raise NotImplementedError

    def _batch_payload(self, items: List[dict]) -> str:
        return json.dumps([{"id": str(i), "speaker": it.get("speaker") or "", "text": it["text"]}
                           for i, it in enumerate(items)], ensure_ascii=False)

    @staticmethod
    def _map_reply(items: List[dict], reply: Optional[list]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if not reply:
            return out
        for obj in reply:
            if isinstance(obj, dict) and str(obj.get("id", "")).isdigit():
                idx = int(obj["id"])
                if 0 <= idx < len(items) and isinstance(obj.get("text"), str):
                    out[items[idx]["id"]] = obj["text"]
        return out


class GeminiBackend(Backend):
    def __init__(self, glossary, model: str, api_key: str):
        super().__init__(glossary)
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self.api_key = api_key
        # Stable prefix (instructions + glossary) first, so Gemini implicit caching can reuse it.
        self.system = SYSTEM_PROMPT + "\n" + glossary_block(glossary)

    def translate(self, items):
        body = {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": [{"role": "user", "parts": [{"text": self._batch_payload(items)}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
        }
        resp = http_post_json(self.url, body, {"x-goog-api-key": self.api_key})
        meta = resp.get("usageMetadata", {})
        self.usage.add(meta.get("promptTokenCount"), meta.get("cachedContentTokenCount"),
                       meta.get("candidatesTokenCount"))
        parts = (resp.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return self._map_reply(items, parse_json_reply("".join(p.get("text", "") for p in parts)))


class OpenAIBackend(Backend):
    """Any OpenAI-compatible /chat/completions server (llama.cpp server, vLLM, OpenRouter...)."""

    def __init__(self, glossary, model: str, base_url: str, api_key: str, sakura: bool):
        super().__init__(glossary)
        self.url = base_url.rstrip('/') + "/chat/completions"
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.sakura = sakura

    def _chat(self, system: str, user: str) -> str:
        body = {"model": self.model, "temperature": 0.1,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        resp = http_post_json(self.url, body, self.headers)
        u = resp.get("usage") or {}
        self.usage.add(u.get("prompt_tokens"), (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                       u.get("completion_tokens"))
        return resp["choices"][0]["message"]["content"]

    def _sakura_prompt(self, text: str) -> str:
        terms = [(k, v) for k, v in sorted(self.glossary.items()) if v and k in text]
        prefix = ""
        if terms:
            prefix = "根据以下术语表（可以为空）：\n" + "\n".join(f"{k}->{v}" for k, v in terms) + "\n"
        return prefix + "将下面的日文文本翻译成中文：" + text

    def translate(self, items):
        if self.sakura:
            # Sakura models keep line counts reliably; fall back to one-by-one on mismatch.
            reply = self._chat(SAKURA_SYSTEM, self._sakura_prompt("\n".join(it["text"] for it in items)))
            lines = [ln for ln in reply.strip().split("\n")]
            if len(lines) == len(items):
                return {it["id"]: ln.strip() for it, ln in zip(items, lines)}
            return {it["id"]: self._chat(SAKURA_SYSTEM, self._sakura_prompt(it["text"])).strip()
                    for it in items}
        system = SYSTEM_PROMPT + "\n" + glossary_block(self.glossary)
        return self._map_reply(items, parse_json_reply(self._chat(system, self._batch_payload(items))))


class DummyBackend(Backend):
    """Offline backend for testing the pipeline: marks text instead of translating it."""

    def translate(self, items):
        self.usage.add(0, 0, 0)
        return {it["id"]: "【译】" + it["text"] for it in items}


def make_backend(args, glossary) -> Backend:
    if args.backend == 'gemini':
        key = args.api_key or os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not key:
            raise SystemExit("Set GEMINI_API_KEY (or pass --api-key) to use the Gemini backend.")
        return GeminiBackend(glossary, args.model or 'gemini-flash-latest', key)
    if args.backend == 'openai':
        return OpenAIBackend(glossary, args.model or 'local', args.base_url,
                             args.api_key or os.environ.get('OPENAI_API_KEY', ''), args.sakura)
    return DummyBackend(glossary)


# ==============================================================================
# Checks
# ==============================================================================

def check_entry(e: dict) -> List[str]:
    t = e.get("trans")
    if t is None:
        return []
    flags = []
    if not t.strip():
        flags.append("empty")
    if RE_KANA.search(RE_PLACEHOLDER.sub('', t)):
        flags.append("kana")
    want = sorted(int(n) for n in RE_PLACEHOLDER.findall(e["text"]))
    if sorted(int(n) for n in RE_PLACEHOLDER.findall(t)) != want:
        flags.append("tags")
    if len(t) > len(e["text"]) * 1.6 + 10:
        flags.append("long")
    return flags


# ==============================================================================
# Commands
# ==============================================================================

def load_project(project: str) -> Tuple[List[dict], Dict[str, str]]:
    entries = load_json(os.path.join(project, ENTRIES_FILE))
    if entries is None:
        raise SystemExit(f"No project at {project}. Run 'extract' first.")
    return entries, load_json(os.path.join(project, GLOSSARY_FILE), {})


def cmd_translate(args) -> int:
    project = os.path.abspath(args.project)
    entries, glossary = load_project(project)
    backend = make_backend(args, glossary)
    todo = [e for e in entries if e["trans"] is None or (args.retranslate_flagged and e["flags"])]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("Nothing to translate.")
        return 0
    print(f"Translating {len(todo)} lines with {args.backend} (batch size {args.batch})...")
    done = 0
    for i in range(0, len(todo), args.batch):
        batch = todo[i:i + args.batch]
        items = [{"id": e["id"], "speaker": glossary.get(e["speaker"] or "", "") or e["speaker"],
                  "text": e["text"]} for e in batch]
        try:
            result = backend.translate(items)
        except RuntimeError as err:
            print(f"  ! batch failed: {err}")
            break
        for e in batch:
            if e["id"] in result:
                e["trans"] = result[e["id"]]
                e["flags"] = check_entry(e)
                done += 1
        save_json(os.path.join(project, ENTRIES_FILE), entries)  # resumable
        print(f"  {min(i + args.batch, len(todo))}/{len(todo)}")
    print(f"Translated {done} lines. Usage: {backend.usage.report()}")
    return 0 if done else 1


def cmd_status(args) -> int:
    entries, glossary = load_project(os.path.abspath(args.project))
    done = [e for e in entries if e["trans"] is not None]
    flagged = [e for e in done if e["flags"]]
    print(f"Lines: {len(entries)}  translated: {len(done)}  flagged: {len(flagged)}")
    missing = [k for k, v in glossary.items() if not v]
    if missing:
        print(f"Glossary entries without translation: {len(missing)} ({', '.join(missing[:8])}...)")
    for e in flagged[:args.show]:
        print(f"  [{','.join(e['flags'])}] {e['id']}: {e['text']}  =>  {e['trans']}")
    return 0


def cmd_export(args) -> int:
    entries, _ = load_project(os.path.abspath(args.project))
    with open(args.out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(["id", "speaker", "source", "translation", "flags"])
        for e in entries:
            w.writerow([e["id"], e["speaker"] or "", e["text"], e["trans"] or "", ",".join(e["flags"])])
    print(f"Wrote {len(entries)} rows to {args.out}")
    return 0


def cmd_import(args) -> int:
    project = os.path.abspath(args.project)
    entries, _ = load_project(project)
    by_id = {e["id"]: e for e in entries}
    changed = 0
    with open(args.tsv, 'r', encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            e = by_id.get(row.get("id", ""))
            new = row.get("translation", "")
            if e is not None and new and new != e["trans"]:
                e["trans"] = new
                e["flags"] = check_entry(e)
                changed += 1
    save_json(os.path.join(project, ENTRIES_FILE), entries)
    print(f"Updated {changed} lines.")
    return 0


def rebuild_line(e: dict) -> str:
    trans = e["trans"]
    tags = e["tags"]
    if "tags" in e["flags"]:
        # Placeholders were lost. Inline tags are mostly decoration (ruby, font, mid-line waits),
        # so dropping them is safer than guessing a position; proofreaders can restore them.
        body = RE_PLACEHOLDER.sub('', trans)
    else:
        body = RE_PLACEHOLDER.sub(lambda m: tags[int(m.group(1)) - 1]
                                  if 0 < int(m.group(1)) <= len(tags) else '', trans)
    return e["prefix"] + body + e["suffix"]


def cmd_pack(args) -> int:
    project = os.path.abspath(args.project)
    entries, glossary = load_project(project)
    meta = load_json(os.path.join(project, META_FILE), {"scripts": {}})
    by_file: Dict[str, List[dict]] = {}
    for e in entries:
        if e["trans"] is not None and not (args.skip_flagged and e["flags"]):
            by_file.setdefault(e["file"], []).append(e)

    files: List[Tuple[str, bytes]] = []
    for name in meta["scripts"]:
        with open(script_store_path(project, name), 'r', encoding='utf-8', newline='') as f:
            lines = f.read().splitlines(keepends=True)
        replaced = {e["line"]: e for e in by_file.get(name, [])}
        out = []
        for lineno, line in enumerate(lines, 1):
            ending = line[len(line.rstrip('\r\n')):]
            if lineno in replaced:
                line = rebuild_line(replaced[lineno]) + ending
            else:
                sp = speaker_of(line)
                if sp and glossary.get(sp):
                    line = line.replace(sp, glossary[sp], 1)
            out.append(line)
        if not replaced:
            continue
        # Kirikiri reads UTF-16LE with BOM on every version, and it can hold Chinese text.
        files.append((name, b'\xff\xfe' + "".join(out).encode('utf-16le')))

    if not files:
        print("No translated lines to pack.")
        return 1
    out_path = os.path.abspath(args.out or os.path.join(project, 'patch_chs.xp3'))
    xp3_write(files, out_path)
    print(f"Packed {len(files)} scripts into {out_path}")
    print("Copy it into the game folder as the next unused patch name (patch.xp3, patch2.xp3, ...).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gal_translate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('extract', help='extract dialogue from a game folder, XP3 or .ks folder')
    s.add_argument('source')
    s.add_argument('project', help='project folder to create')
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser('translate', help='translate untranslated lines with an LLM')
    s.add_argument('project')
    s.add_argument('--backend', choices=['gemini', 'openai', 'dummy'], default='gemini')
    s.add_argument('--model', help='gemini default: gemini-flash-latest')
    s.add_argument('--base-url', default='http://127.0.0.1:8080/v1', help='OpenAI-compatible server')
    s.add_argument('--api-key')
    s.add_argument('--sakura', action='store_true', help='use the SakuraLLM prompt format')
    s.add_argument('--batch', type=int, default=30)
    s.add_argument('--limit', type=int, default=0, help='translate at most N lines (for trying out)')
    s.add_argument('--retranslate-flagged', action='store_true')
    s.set_defaults(func=cmd_translate)

    s = sub.add_parser('status', help='show progress and flagged lines')
    s.add_argument('project')
    s.add_argument('--show', type=int, default=20)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser('export', help='export a bilingual TSV for proofreading')
    s.add_argument('project')
    s.add_argument('out')
    s.set_defaults(func=cmd_export)

    s = sub.add_parser('import', help='import a proofread TSV')
    s.add_argument('project')
    s.add_argument('tsv')
    s.set_defaults(func=cmd_import)

    s = sub.add_parser('pack', help='build a patch XP3 from the translation')
    s.add_argument('project')
    s.add_argument('-o', '--out')
    s.add_argument('--skip-flagged', action='store_true', help='keep original text for flagged lines')
    s.set_defaults(func=cmd_pack)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
