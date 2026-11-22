import asyncio
import html
import json
import logging
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File, Image, Video
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .backend_http import open_response, origin

logger = logging.getLogger("astrbot_plugin_kikoeru_dl")

_PLUGIN_NAME = "astrbot_plugin_kikoeru_dl"
_KIKOERU_OUTPUT_FORMATS = ("mp4", "mp3", "wav")
_KIKOERU_NO_EFFECTS = (
    r"无(?:se|音效|效果音?|効果音?)|無(?:se|音效|效果音?|効果音?)|"
    r"(?:效果音|効果音)(?:なし|無し|なし版)|se(?:なし|無し|无|無|カット)|"
    r"se[ _-]*cut|no[ _-]*(?:se|sfx)|without[ _-]*(?:se|sfx)"
)
_KIKOERU_WITH_EFFECTS = (
    r"(?:有se|有音效|有效果音?|有効果音?|seあり|se有|(?:效果音|効果音)(?:あり|入り)|"
    r"with[ _-]*(?:se|sfx))"
)

_plugin_context: Optional[Context] = None
_plugin_config: Any = None

# Keep candidate numbers scoped to each conversation.
_recent_search_results: dict[str, list[dict]] = {}
_kikoeru_recent_batches: dict[str, dict] = {}
_KIKOERU_QUICK_WINDOW_SECONDS = 600
_KIKOERU_QUICK_RJ = r"rj\s*\d{5,}"
_KIKOERU_QUICK_NEXT = r"换一批|再来一批|换一页|随机(?:再来|换)?一批"
_KIKOERU_QUICK_TRACK = (
    r"第\s*(?P<work>\d{1,4})\s*部\s*第\s*(?P<track>\d{1,4})\s*号"
    r"(?:\s*[,，]?\s*(?:用|发|下载成)?\s*(?:(?P<effects>无音效|有音效)(?:版)?\s*)?"
    r"(?P<format>mp4|mp3|wav)?\s*(?:发我|给我)?)?"
)

# Cache the active work and its grouped tracks for numbered downloads.
_recent_kikoeru_tracks: dict[str, dict] = {}
_kikoeru_work_detail_cache: dict[int, dict] = {}


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


def _dl_dir() -> Path:
    d = StarTools.get_data_dir(_PLUGIN_NAME) / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg(key: str, default: Any) -> Any:
    cfg = _plugin_config
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default) or default


def _event_from_context(context: Any) -> Optional[Any]:
    try:
        agent_ctx = getattr(context, "context", None)
        event = getattr(agent_ctx, "event", None) if agent_ctx else None
        if event is None and agent_ctx is not None:
            event = getattr(getattr(agent_ctx, "context", None), "event", None)
        if event is None and agent_ctx is not None:
            extra = getattr(agent_ctx, "extra", None) or {}
            if isinstance(extra, dict):
                event = extra.get("event")
        return event
    except Exception:
        return None


def _session_umo(event: Any) -> str:
    if event is None:
        return ""
    try:
        umo = getattr(event, "unified_msg_origin", "") or ""
        if ":" in umo:
            return umo
    except Exception:
        pass
    try:
        sid = event.get_session_id()
        if sid:
            return str(sid)
    except Exception:
        pass
    return ""


def _remember_search_results(session: str, results: list) -> None:
    if not session or not results:
        return
    _recent_search_results[session] = [dict(r) for r in results]
    while len(_recent_search_results) > 8:
        _recent_search_results.pop(next(iter(_recent_search_results)))


def _resolve_search_result(session: str, index: Any) -> Optional[dict]:
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    stored = _recent_search_results.get(session or "")
    if not stored or idx < 1 or idx > len(stored):
        return None
    return stored[idx - 1]


async def _send_track_media(session_umo: str, path: Path) -> bool:
    if not session_umo or not path.is_file():
        return False
    ctx = _plugin_context
    if not ctx:
        return False
    try:
        result = MessageEventResult()
        if path.suffix == ".mp4":
            result.chain.append(Video.fromFileSystem(str(path)))
        else:
            result.chain.append(File(name=path.name, file=str(path)))
        return await ctx.send_message(session_umo, result)
    except Exception as e:
        logger.warning("Kikoeru track upload failed: %s", e)
        return False



def _format_duration(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    try:
        total = int(value)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"



_kikoeru_prefs_cache: dict = {}


def _kikoeru_prefs() -> dict:
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata or not _cfg("kikoeru_use_local_preferences", False):
        return {}
    path = Path(appdata) / "com.meteor" / "KikoFlu" / "shared_preferences.json"
    try:
        stamp = path.stat().st_mtime if path.exists() else 0
    except Exception:
        stamp = 0
    if _kikoeru_prefs_cache.get("stamp") == stamp:
        return _kikoeru_prefs_cache.get("data") or {}
    data: dict = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        pass
    _kikoeru_prefs_cache["stamp"] = stamp
    _kikoeru_prefs_cache["data"] = data
    return data


def _kikoeru_pref(key: str, default: str = "") -> str:
    prefs = _kikoeru_prefs()
    val = prefs.get(f"flutter.{key}")
    if val is None:
        val = prefs.get(key)
    if val is None:
        return default
    return str(val)


def _kikoeru_local_backend() -> str:
    host = _kikoeru_pref("server_host").strip().rstrip("/")
    if host and "://" not in host:
        scheme = "http" if host.startswith(("localhost", "127.", "192.168.")) else "https"
        host = f"{scheme}://{host}"
    return host


def _kikoeru_base() -> str:
    configured = (_cfg("kikoeru_url", "") or "").strip().rstrip("/")
    base = configured or _kikoeru_local_backend()
    if not base:
        raise ValueError("请在插件配置填写 KikoFlu 后端地址。")
    origin(base)
    if urllib.parse.urlsplit(base).query or urllib.parse.urlsplit(base).fragment:
        raise ValueError("KikoFlu 后端地址不能包含查询参数或片段。")
    return base


def _kikoeru_same_local_backend(base: str) -> bool:
    local = _kikoeru_local_backend()
    if not local:
        return False
    try:
        return origin(local) == origin(base)
    except ValueError:
        return False


def _kikoeru_headers(base: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": _UA}
    if origin(base) in _KNOWN_MEDIA_ORIGINS:
        headers.update({"Referer": "https://www.asmr.one/", "Origin": "https://www.asmr.one"})
    token = (_cfg("kikoeru_token", "") or "").strip()
    if not token and _kikoeru_same_local_backend(base):
        token = _kikoeru_pref("auth_token").strip()
        if cookie := _kikoeru_pref("server_cookie").strip():
            headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


_KNOWN_MEDIA_ORIGINS = {
    ("https", "api.asmr-200.com", 443): {("https", "raw.kiko-play-niptan.one", 443)},
    ("https", "api.asmr.one", 443): {("https", "raw.kiko-play-niptan.one", 443)},
}


def _kikoeru_media_origins(base: str) -> set[tuple[str, str, int]]:
    result = set(_KNOWN_MEDIA_ORIGINS.get(origin(base), ()))
    for url in _cfg("kikoeru_media_origins", []) or []:
        parts = urllib.parse.urlsplit(url.strip())
        target = origin(parts.geturl())
        if target[0] != "https" or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("媒体地址白名单只接受 HTTPS 源，例如 https://cdn.example.com。")
        result.add(target)
    return result


def _kikoeru_request(method: str, url: str, json_body: dict | None = None) -> Any:
    base = _kikoeru_base()
    proxy = (_cfg("kikoeru_proxy", "") or "").strip()
    return open_response(
        base, url, _kikoeru_headers(base), _kikoeru_media_origins(base), proxy, method, json_body
    )


def _kikoeru_mark_resolve(works: str, session: str) -> tuple:
    """把作品标识解析成 work_id 列表，返回 (ids, failed_parts)。
    纯数字/第N条 只认当前会话刚发候选里的编号，解析不到不会兜底成作品 ID。"""
    ids: list = []
    failed: list = []
    seen: set = set()
    for part in re.split(r"[,，、\s]+", (works or "").strip()):
        if not part:
            continue
        wid = None
        m = re.fullmatch(r"第?(\d+)条?", part)
        if m:
            r = _resolve_search_result(session, int(m.group(1)))
            if r:
                wid = r.get("kikoeru_work_id")
            else:
                failed.append(part)
                continue
        if wid is None:
            m = re.search(r"/api/work/(\d+)", part)
            if m:
                wid = int(m.group(1))
        if wid is None:
            rj = _kikoeru_normalize_rj(part)
            if rj:
                try:
                    d = _kikoeru_search_direct(part)
                except Exception:
                    d = None
                if d:
                    wid = d.get("kikoeru_work_id")
                else:
                    failed.append(part)
                    continue
        if wid is None:
            failed.append(part)
            continue
        if wid not in seen:
            seen.add(wid)
            ids.append(wid)
    return ids, failed


def _kikoeru_mark_works(work_ids: list, status: str) -> list:
    base = _kikoeru_base()
    results: list = []
    for wid in work_ids:
        try:
            if status == "remove":
                resp = _kikoeru_request(
                    "DELETE", f"{base}/api/review?work_id={wid}"
                )
            else:
                resp = _kikoeru_request(
                    "PUT", f"{base}/api/review",
                    {"work_id": wid, "progress": status},
                )
            body = resp.read().decode("utf-8", "ignore")
            results.append({"work_id": wid, "ok": True, "detail": body[:120]})
        except Exception as exc:
            detail = str(exc)[:120]
            if "500" in detail:
                detail = "后端 500（作品可能不存在或服务异常）"
            results.append({"work_id": wid, "ok": False, "detail": detail})
    return results


def _kikoeru_open(url: str) -> Any:
    return _kikoeru_request("GET", url)


def _kikoeru_work_id_from_url(url: Any) -> Optional[int]:
    m = re.search(r"/api/work/(\d+)", url or "")
    return int(m.group(1)) if m else None


def _kikoeru_download_dir() -> Path:
    cfg = (_cfg("kikoeru_download_dir", "") or "").strip()
    target = Path(cfg).expanduser() if cfg else StarTools.get_data_dir(_PLUGIN_NAME) / "library"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _kikoeru_sanitize_segment(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return cleaned[:120] or "untitled"


def _kikoeru_split_segments(title: str) -> list:
    return [
        _kikoeru_sanitize_segment(p)
        for p in re.split(r"[\\/]+", title or "")
        if p.strip()
    ]


def _kikoeru_collect_files(nodes: Any, parent: list, out: list) -> None:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        parts = _kikoeru_split_segments(node.get("title") or node.get("name") or "")
        if parts[: len(parent)] == parent:
            parts = parts[len(parent):]
        node_path = parent + parts
        children = node.get("children")
        if isinstance(children, list):
            _kikoeru_collect_files(children, node_path, out)
        elif node.get("hash"):
            out.append((str(node["hash"]), node_path, node))



def _kikoeru_media_url(base: str, node: dict) -> str:
    if media := node.get("mediaStreamUrl"):
        url = urllib.parse.urljoin(base + "/", str(media).strip())
        try:
            target = origin(url)
        except ValueError:
            target = None
        if target == origin(base) or target in _kikoeru_media_origins(base):
            return url
    file_hash = str(node.get("hash") or "")
    if not file_hash:
        raise ValueError("后端没有提供可用的媒体地址。")
    return f"{base}/api/media/stream/{urllib.parse.quote(file_hash, safe='')}"


def _download_kikoeru_work(work_id: int) -> tuple:
    base = _kikoeru_base()
    resp = _kikoeru_open(f"{base}/api/tracks/{work_id}")
    tracks = json.loads(resp.read().decode("utf-8", "ignore"))
    files: list = []
    _kikoeru_collect_files(tracks, [], files)
    if not files:
        raise RuntimeError(f"作品 {work_id} 没有可下载的文件")
    target = _kikoeru_download_dir() / str(work_id)
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    total = 0
    for idx, (file_hash, parts, node) in enumerate(files, 1):
        path_parts = parts or ["track", str(idx)]
        dest = target.joinpath(*path_parts)
        if dest.exists() and dest.stat().st_size > 0:
            total += dest.stat().st_size
            count += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with _kikoeru_open(
                _kikoeru_media_url(base, node)
            ) as src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f, 1024 * 256)
        except Exception as exc:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"下载 {dest.name} 失败：{exc}") from exc
        total += dest.stat().st_size
        count += 1
    return target, count, total


def _kikoeru_work_lang_flags(work: dict) -> tuple:
    if not isinstance(work, dict):
        work = {}
    has_sub = bool(work.get("has_subtitle"))
    has_chi = False
    for ed in work.get("language_editions") or []:
        if not isinstance(ed, dict):
            continue
        lang = str(ed.get("lang") or "").upper()
        if lang in (
            "CHI", "CHI_HANS", "CHI_HANT", "ZH", "ZH_CN", "ZH_TW",
            "ZH_HANS", "ZH_HANT", "中文", "简体", "繁体", "中字",
        ):
            has_chi = True
            break
    if re.search(r"(简体|繁体|中文|中字)", str(work.get("title") or "")):
        has_chi = True
    return has_sub, has_chi


def _kikoeru_tag_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _kikoeru_excluded_tags() -> set[str]:
    return {
        key
        for name in _cfg("kikoeru_excluded_tags", []) or []
        if isinstance(name, str) and (key := _kikoeru_tag_key(name))
    }


def _kikoeru_work_tags(work: dict) -> tuple[list[str], set[str]]:
    """Read display names and aliases from the backend's tag records.

    Args:
        work: KikoFlu work metadata.

    Returns:
        Tag names in backend order and normalized names for matching.
    """
    tags = []
    aliases = set()
    for tag in work.get("tags") or []:
        if isinstance(tag, str):
            names = [tag]
        elif isinstance(tag, dict):
            translations = tag.get("i18n") or {}
            names = [tag.get("name")]
            if isinstance(translations, dict):
                names.extend(
                    value.get("name") for value in translations.values() if isinstance(value, dict)
                )
        else:
            continue
        names = [name.strip() for name in names if isinstance(name, str) and name.strip()]
        if names:
            tags.append(names[0])
            aliases.update(_kikoeru_tag_key(name) for name in names)
    return tags, aliases


def _kikoeru_tag_rejection(work_id: int, excluded: set[str]) -> str | None:
    if not excluded:
        return None
    try:
        work = _kikoeru_fetch_work_detail(work_id)
        if not isinstance(work.get("tags"), list):
            return "暂时无法核对作品标签，未发送或下载。"
        _, aliases = _kikoeru_work_tags(work)
    except Exception:
        logger.warning("Could not verify tags for Kikoeru work %s", work_id, exc_info=True)
        return "暂时无法核对作品标签，未发送或下载。"
    if excluded.intersection(aliases):
        return "这部作品命中了排除标签，未发送或下载。"
    return None


def _kikoeru_result_from_work(work: dict, direct: bool = False) -> dict:
    base = _kikoeru_base()
    wid = work.get("id")
    title = (work.get("title") or "").strip()
    if not wid or not title:
        raise RuntimeError("KikoFlu 搜索接口返回了不完整的作品数据")
    circle = work.get("circle") or work.get("name") or ""
    if isinstance(circle, dict):
        circle = circle.get("name") or ""
    else:
        circle = str(circle)
    display = f"[{circle}] {title}" if circle else title
    duration = ""
    try:
        seconds = int(work.get("duration") or 0)
        if seconds > 0:
            duration = _format_duration(seconds)
    except (TypeError, ValueError):
        pass
    has_sub, has_chi = _kikoeru_work_lang_flags(work)
    tags, aliases = _kikoeru_work_tags(work)
    return {
        "title": display,
        "work_title": title,
        "circle": str(circle),
        "url": f"{base}/api/work/{wid}",
        "duration": duration,
        "release": str(work.get("release") or ""),
        "tags": tags,
        "tag_aliases": sorted(aliases),
        "kikoeru_work_id": wid,
        "kikoeru_direct": bool(direct),
        "kikoeru_has_subtitle": has_sub,
        "kikoeru_lang": "中字" if has_chi else ("日字" if has_sub else "无字幕"),
    }


def _kikoeru_normalize_rj(keyword: str) -> Optional[str]:
    kw = re.sub(r"\s+", "", (keyword or "").strip()).lower()
    m = re.fullmatch(r"rj(\d{5,})", kw)
    if m:
        return f"rj{m.group(1)}"
    return None


def _kikoeru_search_exact(works: list, keyword: str) -> Optional[dict]:
    kw = re.sub(r"\s+", "", (keyword or "").strip()).lower()
    if not kw:
        return None
    rj = re.fullmatch(r"rj(\d{5,})", kw)
    for w in works or []:
        if not isinstance(w, dict):
            continue
        if rj:
            source = re.sub(
                r"\s+", "", str(w.get("source_id") or w.get("original_workno") or "")
            ).lower()
            if f"rj{rj.group(1)}" == source:
                return w
        title = re.sub(r"\s+", "", str(w.get("title") or "")).lower()
        if title == kw:
            return w
    return None


def _kikoeru_search_raw_works(keyword: str, page: int = 1, pageSize: int = 40) -> list:
    base = _kikoeru_base()
    q = urllib.parse.quote(keyword)
    params = urllib.parse.urlencode(
        {
            "page": page,
            "pageSize": pageSize,
            "order": "create_date",
            "sort": "desc",
            "subtitle": 1,
            "includeTranslationWorks": "true",
        }
    )
    url = f"{base}/api/search/{q}?{params}"
    try:
        resp = _kikoeru_open(url)
    except Exception:
        resp = _kikoeru_open(f"{base}/api/search/{q}?page={page}")
    data = json.loads(resp.read().decode("utf-8", "ignore"))
    works = data.get("works") if isinstance(data, dict) else data
    return works if isinstance(works, list) else []


def _kikoeru_filter_search_results(works: list) -> list:
    pool: list = []
    seen: set = set()
    for w in works or []:
        if not isinstance(w, dict):
            continue
        wid = w.get("id")
        if not wid or not (w.get("title") or "").strip():
            continue
        if wid in seen:
            continue
        seen.add(wid)
        _, has_chi = _kikoeru_work_lang_flags(w)
        if not has_chi:
            continue
        pool.append(w)
    return pool


def _kikoeru_search_page_results(keyword: str, page: int = 1, pageSize: int = 40) -> list:
    works = _kikoeru_search_raw_works(keyword, page, pageSize)
    pool = _kikoeru_filter_search_results(works)
    return [_kikoeru_result_from_work(w) for w in pool]


def _kikoeru_search_direct(keyword: str) -> Optional[dict]:
    query = _kikoeru_normalize_rj(keyword) or keyword
    works = _kikoeru_search_raw_works(query, page=1, pageSize=40)
    exact = _kikoeru_search_exact(works, query)
    if not exact:
        return None
    return _kikoeru_result_from_work(exact, direct=True)


# ---------- KikoFlu 顺序·随机抓取池 ----------
# 池用于“换一批 / 随机一批”的续抓：同一关键词或列表首次调用仍按正常顺序发第一批，
# 只有用户明确说“换一批/换一页/再来一批/继续”或“随机一批/换个口味”时才从池里继续抽，
# 并自动跳过已发过的条目；顺序与随机共用同一池，互相切换也不会重复。
# RJ 号或明确完整作品名直读音轨列表，不进入抓取池。
_KIKOERU_POOL_LOCK = threading.RLock()
_KIKOERU_POOLS: dict = {}
_KIKOERU_POOL_MAX = 3
_KIKOERU_POOL_PAGE_MAX = 3
_KIKOERU_SEEN_IDS_MAX = 3000
_KIKOERU_SEEN_IDS: set = set()


def _kikoeru_pool_key(kind: str, sub: str) -> str:
    if kind == "search":
        return f"kikoeru|search|{sub}"
    if kind == "my":
        return f"kikoeru|my|{sub}"
    return f"kikoeru|{kind}|{sub}"


def _kikoeru_pool_state_path() -> Path:
    return StarTools.get_data_dir(_PLUGIN_NAME) / "state.json"


def _kikoeru_pool_load_state() -> None:
    with _KIKOERU_POOL_LOCK:
        try:
            p = _kikoeru_pool_state_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            pools = data.get("pools") if isinstance(data, dict) else None
            if isinstance(pools, dict):
                for key, value in pools.items():
                    if isinstance(value, dict):
                        _KIKOERU_POOLS[key] = value
            seen = data.get("seen")
            if isinstance(seen, list):
                _KIKOERU_SEEN_IDS.update(str(v) for v in seen[: _KIKOERU_SEEN_IDS_MAX])
        except Exception as exc:
            logger.warning("kikoeru_dl: pool state load failed: %s", exc)


def _kikoeru_pool_save_state() -> None:
    with _KIKOERU_POOL_LOCK:
        try:
            p = _kikoeru_pool_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {"pools": _KIKOERU_POOLS, "seen": sorted(_KIKOERU_SEEN_IDS)[-_KIKOERU_SEEN_IDS_MAX:]}
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("kikoeru_dl: pool state save failed: %s", exc)


def _kikoeru_pool_item_id(item: dict) -> str:
    wid = item.get("kikoeru_work_id")
    if wid is not None:
        return f"work:{wid}"
    return str(item.get("url") or "")


def _kikoeru_pool_mark_seen(item: dict) -> None:
    iid = _kikoeru_pool_item_id(item)
    if not iid:
        return
    with _KIKOERU_POOL_LOCK:
        _KIKOERU_SEEN_IDS.add(iid)
        while len(_KIKOERU_SEEN_IDS) > _KIKOERU_SEEN_IDS_MAX:
            _KIKOERU_SEEN_IDS.pop()


def _kikoeru_pool_new_candidates(kind: str, sub: str) -> list:
    if kind == "search":
        results = _kikoeru_search_page_results(sub, page=1, pageSize=40)
        if not results:
            raise RuntimeError(
                "KikoFlu 音声库没有带中文字幕的匹配结果，请确认 KikoFlu 已登录且后端可达。"
            )
        return results
    if kind == "my":
        results = _kikoeru_my_reviews_page((sub or "marked").strip(), page=1, page_size=20)
        if not results:
            raise RuntimeError("KikoFlu 我的标记列表为空，请先在 KikoFlu 中标记作品。")
        return results
    return []


def _kikoeru_pool_extend(pool: dict) -> tuple:
    kind, sub = pool["kind"], pool["sub"]
    page = pool["page"]
    if kind == "search":
        return _kikoeru_search_page_results(sub, page=page, pageSize=40), True
    if kind == "my":
        return _kikoeru_my_reviews_page((sub or "marked").strip(), page=page, page_size=20), True
    return [], False


def _kikoeru_pool_take_batch(kind: str, sub: str, limit: int = 5, mode: str = "sequential") -> tuple:
    key = _kikoeru_pool_key(kind, sub)
    excluded = _kikoeru_excluded_tags()
    if excluded:
        key += "|exclude:" + json.dumps(sorted(excluded), ensure_ascii=False)
    limit = max(1, int(limit or 0) or 5)
    with _KIKOERU_POOL_LOCK:
        pool = _KIKOERU_POOLS.get(key)
        is_new = False
        if pool is None:
            pool = {
                "kind": kind,
                "sub": sub,
                "all": [],
                "cursor": 0,
                "random_ready": False,
                "random_order": [],
                "random_cursor": 0,
                "page": 2 if kind == "search" else 1,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            pool["all"] = _kikoeru_pool_new_candidates(kind, sub)
            _KIKOERU_POOLS[key] = pool
            while len(_KIKOERU_POOLS) > _KIKOERU_POOL_MAX:
                _KIKOERU_POOLS.pop(next(iter(_KIKOERU_POOLS)))
            _kikoeru_pool_save_state()
            is_new = True
        else:
            _KIKOERU_POOLS.pop(key)
            _KIKOERU_POOLS[key] = pool

        chosen: set = set()

        def _take(stream: list, cursor: int, n: int) -> tuple:
            out = []
            i = cursor
            while len(out) < n and i < len(stream):
                it = stream[i]
                i += 1
                iid = _kikoeru_pool_item_id(it)
                if iid in _KIKOERU_SEEN_IDS or iid in chosen:
                    continue
                if excluded and excluded.intersection(it.get("tag_aliases") or ()):
                    continue
                chosen.add(iid)
                out.append(it)
            return out, i

        mode = (mode or "").strip().lower()
        if is_new or mode != "random":
            out, pool["cursor"] = _take(pool["all"], pool["cursor"], limit)
            mode = "sequential"
        else:
            if not pool["random_ready"]:
                pool["random_order"] = random.sample(pool["all"], len(pool["all"]))
                pool["random_cursor"] = 0
                pool["random_ready"] = True
            out, pool["random_cursor"] = _take(pool["random_order"], pool["random_cursor"], limit)

        rounds = 0
        extended = False
        while len(out) < limit and rounds < _KIKOERU_POOL_PAGE_MAX:
            items, _has_more = _kikoeru_pool_extend(pool)
            if not items:
                break
            pool["page"] += 1
            pool["all"].extend(items)
            if pool["random_ready"]:
                pool["random_order"].extend(random.sample(items, len(items)))
            if mode == "random":
                out2, pool["random_cursor"] = _take(pool["random_order"], pool["random_cursor"], limit - len(out))
            else:
                out2, pool["cursor"] = _take(pool["all"], pool["cursor"], limit - len(out))
            out.extend(out2)
            rounds += 1
            extended = True

        exhausted = len(out) < limit
        pool["updated_at"] = time.time()
        if is_new or extended or exhausted:
            _kikoeru_pool_save_state()
        if exhausted and not is_new and (not excluded or not out):
            raise RuntimeError("这一轮已经翻完了，没有更多新内容。换个关键词/来源，或让我再换一批别的作品。")
        return out, {"is_new": is_new, "mode": mode, "extended": extended, "exhausted": exhausted}


def _search_kikoeru(keyword: str, limit: int = 5) -> list:
    limit = max(1, int(limit or 0) or 5)
    direct = _kikoeru_search_direct(keyword)
    if direct:
        return [direct]
    out, _meta = _kikoeru_pool_take_batch("search", keyword, limit, "sequential")
    return out




def _kikoeru_my_reviews_page(filter_name: str = "marked", page: int = 1, page_size: int = 20) -> list:
    base = _kikoeru_base()
    query = {
        "page": page,
        "pageSize": page_size,
        "order": "updated_at",
        "sort": "desc",
    }
    if (filter_name or "").strip().lower() != "all":
        query["filter"] = filter_name
    params = urllib.parse.urlencode(query)
    resp = _kikoeru_open(f"{base}/api/review?{params}")
    data = json.loads(resp.read().decode("utf-8", "ignore"))
    raw = data.get("works") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raw = data.get("reviews") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raw = data.get("data") if isinstance(data, dict) else data
    works = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        w = item.get("work") if isinstance(item.get("work"), dict) else item
        if w and w.get("id") and (w.get("title") or "").strip():
            works.append(w)
    return [_kikoeru_result_from_work(w) for w in works]


def _kikoeru_my_reviews(limit: int = 5, filter_name: str = "marked") -> list:
    limit = max(1, int(limit or 0) or 5)
    results = _kikoeru_my_reviews_page((filter_name or "marked").strip(), page=1, page_size=20)
    if not results:
        raise RuntimeError("KikoFlu 我的标记列表为空，请先在 KikoFlu 中标记作品。")
    return results[:limit]


def _kikoeru_favourites(limit: int = 5) -> list:
    base = _kikoeru_base()
    params = urllib.parse.urlencode({"page": 1, "pageSize": 20})
    resp = _kikoeru_open(f"{base}/api/favourites?{params}")
    data = json.loads(resp.read().decode("utf-8", "ignore"))
    raw = data.get("works") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raw = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise RuntimeError("KikoFlu 收藏接口返回了无法识别的数据结构")
    works = [
        w
        for w in raw
        if isinstance(w, dict) and w.get("id") and (w.get("title") or "").strip()
    ]
    if not works:
        raise RuntimeError("KikoFlu 收藏列表为空。")
    return [_kikoeru_result_from_work(w) for w in works[:limit]]


def _kikoeru_fetch_work_detail(work_id: int) -> dict:
    cached = _kikoeru_work_detail_cache.get(work_id)
    if isinstance(cached, dict):
        return cached
    base = _kikoeru_base()
    resp = _kikoeru_open(f"{base}/api/work/{work_id}")
    data = json.loads(resp.read().decode("utf-8", "ignore"))
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError(f"KikoFlu 无法读取作品 {work_id} 的详情")
    _kikoeru_work_detail_cache[work_id] = data
    if len(_kikoeru_work_detail_cache) > 100:
        _kikoeru_work_detail_cache.pop(next(iter(_kikoeru_work_detail_cache)), None)
    return data


def _kikoeru_se_label(path_text: str, title: str) -> str:
    combined = unicodedata.normalize("NFKC", f"{path_text}/{title}").casefold()
    return "无SE" if re.search(_KIKOERU_NO_EFFECTS, combined) else "有SE"


def _kikoeru_group_tracks(rows: list[dict]) -> list[dict]:
    """Collapse variants while keeping distinct chapters with the same name.

    Args:
        rows: Raw downloadable MP3 and WAV files in backend order.

    Returns:
        Numberable tracks with a display title and their source variants.
    """
    variant_marker = rf"(?:{_KIKOERU_NO_EFFECTS}|{_KIKOERU_WITH_EFFECTS}|mp3|wav)"
    version = r"(?:[\s_-]*(?:版|版本|音源|ver\.?|version))?"
    folder_variant = re.compile(
        rf"(?:\d+[\s:_\-.]+)?{variant_marker}{version}(?:\s*\([^)]*\))?",
        re.IGNORECASE,
    )
    title_variant = re.compile(
        rf"\s*[\(\[【]{variant_marker}{version}[\)\]】]"
        rf"|(?:[\s_\-]+{variant_marker}{version})+$"
        rf"|^(?:{variant_marker}{version}[\s_\-]+)",
        re.IGNORECASE,
    )
    tracks: dict[tuple, dict] = {}
    for row in rows:
        stem = row["title"].rsplit(".", 1)[0]
        title = unicodedata.normalize("NFKC", stem)
        clean_title = title_variant.sub("", title).strip(" _-") or stem
        parent = []
        for part in row["folder"].split("/"):
            part = unicodedata.normalize("NFKC", part).strip()
            if part and not folder_variant.fullmatch(part):
                # Folders such as "Chapter 1 (MP3)" remain meaningful.
                normalized = title_variant.sub("", part).strip(" _-")
                if normalized:
                    parent.append(" ".join(normalized.casefold().split()))
        key = (tuple(parent), " ".join(clean_title.casefold().split()))
        track = tracks.setdefault(key, {"title": clean_title, "variants": []})
        track["variants"].append(row)
    return list(tracks.values())


def _kikoeru_kind_label(path_text: str) -> str:
    if "特典" in path_text or "おまけ" in path_text:
        return "特典"
    if "闹钟" in path_text or "闹铃" in path_text or "アラーム" in path_text or "alarm" in path_text.lower():
        return "闹铃"
    return "正片"


def _kikoeru_load_track_rows(work_id: int) -> tuple:
    base = _kikoeru_base()
    resp = _kikoeru_open(f"{base}/api/tracks/{work_id}")
    tracks = json.loads(resp.read().decode("utf-8", "ignore"))
    files: list = []
    _kikoeru_collect_files(tracks, [], files)
    text_map: dict = {}
    audio_rows: list = []
    for _, path, node in files:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "")
        title = str(node.get("title") or (path[-1] if path else "") or "")
        lower = title.lower()
        if node_type == "text" and lower.endswith(".vtt"):
            text_map[lower[:-4].strip()] = node
        elif node_type in ("audio", "media") and lower.endswith((".mp3", ".wav")):
            audio_rows.append((path, node))
    if not audio_rows:
        raise RuntimeError(f"作品 {work_id} 没有可发送的 mp3/wav 音轨")
    work = _kikoeru_fetch_work_detail(work_id)
    has_sub, has_chi = _kikoeru_work_lang_flags(work)
    rows: list = []
    for path, node in audio_rows:
        title = str(node.get("title") or (path[-1] if path else "") or "")
        lower = title.lower()
        stem = lower.strip()
        text_node = text_map.get(stem)
        fmt = lower.rsplit(".", 1)[-1]
        folder = "/".join(path[:-1])
        kind = _kikoeru_kind_label(folder)
        se = _kikoeru_se_label(folder, title)
        if text_node:
            sub = "中字" if has_chi else "日字"
        else:
            sub = "无字幕"
        duration = ""
        try:
            d = float(node.get("duration") or 0)
            if d > 0:
                duration = _format_duration(int(d))
        except (TypeError, ValueError):
            pass
        size = ""
        try:
            b = int(node.get("size") or 0)
            if b > 0:
                size = f"{b / (1024 * 1024):.1f}MB"
        except (TypeError, ValueError):
            pass
        rows.append(
            {
                "work_id": work_id,
                "title": title,
                "format": fmt,
                "se": se,
                "kind": kind,
                "subtitle": sub,
                "duration": duration,
                "size": size,
                "audio": node,
                "text": text_node,
                "folder": folder,
            }
        )
    return work, _kikoeru_group_tracks(rows)


def _parse_kikoeru_track_numbers(text: str) -> list:
    tokens = re.split(r"[,，、;；、\s]+", (text or "").strip())
    nums = []
    for tok in tokens:
        tok = re.sub(r"[第个条号项吧]", "", tok).strip()
        if not tok:
            continue
        if re.fullmatch(r"\d{1,3}", tok):
            nums.append(int(tok))
            continue
        m = re.fullmatch(r"(\d{1,3})\s*[-–~至到]\s*(\d{1,3})", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            nums.extend(range(a, b + 1))
    out = []
    for n in nums:
        if n not in out:
            out.append(n)
    return out


def _remember_kikoeru_tracks(session: str, work_id: int, work_title: str, rows: list) -> None:
    if not session:
        return
    _recent_kikoeru_tracks[session] = {
        "work_id": work_id,
        "work_title": work_title,
        "rows": rows,
    }
    while len(_recent_kikoeru_tracks) > 8:
        _recent_kikoeru_tracks.pop(next(iter(_recent_kikoeru_tracks)))


def _format_kikoeru_tracks(work: dict, rows: list) -> str:
    title = (work.get("title") or "").strip()
    lines = [f"《{title}》｜{len(rows)} 条音轨"]
    if any(variant["se"] == "无SE" for row in rows for variant in row["variants"]):
        lines[0] += "｜含无音效"
    for i, row in enumerate(rows, 1):
        lines.append(f"第{i}号 {row['title']}")
    lines.append("\n回复音轨编号下载，可指定 MP4、MP3 或 WAV，也可回复「整部下载」。")
    return "\n".join(lines)


def _kikoeru_cover_url(work_id: int) -> str:
    return f"{_kikoeru_base()}/api/cover/{work_id}"


def _kikoeru_download_to(url: str, dest: Path) -> None:
    with _kikoeru_open(url) as src, open(dest, "wb") as f:
        shutil.copyfileobj(src, f, 1024 * 256)


def _kikoeru_download_cover(work_id: int, tmp: Path) -> Path:
    with _kikoeru_open(
        _kikoeru_cover_url(work_id)
    ) as src:
        ctype = str(src.headers.get("Content-Type") or "").lower()
        ext = ".png" if "png" in ctype else ".jpg"
        dest = tmp / f"cover{ext}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(src, f, 1024 * 256)
    return dest


def _subtitle_to_srt(data: bytes) -> str:
    text = data.decode("utf-8", "ignore").strip()
    if not text:
        return ""
    if text.startswith("WEBVTT"):
        cues: list = []
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block or block == "WEBVTT" or block.startswith("NOTE"):
                continue
            m = re.search(
                r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*"
                r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})",
                block,
            )
            if not m:
                continue
            lines = [ln for ln in block.splitlines() if "-->" not in ln]
            payload = "\n".join(ln.strip() for ln in lines if ln.strip())
            if not payload:
                continue
            cues.append(
                (
                    _normalize_vtt_time(m.group(1)),
                    _normalize_vtt_time(m.group(2)),
                    payload,
                )
            )
        if not cues:
            return ""
        out: list = []
        for i, (start, end, payload) in enumerate(cues, 1):
            out.append(str(i))
            out.append(f"{start} --> {end}")
            out.append(payload)
            out.append("")
        return "\n".join(out).strip() + "\n"
    lrc_m = re.findall(
        r"\[(\d{1,2}):(\d{2})[.:](\d{1,3})\](.*)",
        text,
        re.M,
    )
    if lrc_m:
        cues = []
        for hh, mm, frac_raw, payload in lrc_m:
            frac_ms = int(frac_raw.ljust(3, "0")[:3])
            total_ms = (int(hh) * 3600 + int(mm) * 60) * 1000 + frac_ms
            total_s = total_ms // 1000
            ms = total_ms % 1000
            cues.append(
                (
                    f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d},{ms:03d}",
                    payload.strip(),
                )
            )
        cues.sort(key=lambda x: x[0])
        out = []
        for i, (start, payload) in enumerate(cues, 1):
            out.append(str(i))
            out.append(start)
            out.append(payload)
            out.append("")
        return "\n".join(out).strip() + "\n"
    if "-->" in text:
        return text + "\n"
    return ""


def _normalize_vtt_time(ts: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})", ts)
    if m:
        h, mm, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ms = int(m.group(4).ljust(3, "0"))
        return f"{h:02d}:{mm:02d}:{s:02d},{ms:03d}"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})[.,](\d{1,3})", ts)
    if m:
        h, mm, s = 0, int(m.group(1)), int(m.group(2))
        ms = int(m.group(3).ljust(3, "0"))
        return f"{h:02d}:{mm:02d}:{s:02d},{ms:03d}"
    return ts


def _ffmpeg_path() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    raise RuntimeError("未找到 FFmpeg，请安装后加入 AstrBot 运行环境的 PATH。")


def _build_kikoeru_track_mp4(row: dict) -> tuple:
    import subprocess

    work_id = row["work_id"]
    tmp = Path(tempfile.mkdtemp(prefix="kikoeru_", dir=str(_dl_dir())))
    out = tmp / "voice.mp4"
    try:
        cover = _kikoeru_download_cover(work_id, tmp)
        audio = row["audio"]
        audio_path = tmp / f"audio.{row['format']}"
        _kikoeru_download_to(_kikoeru_media_url(_kikoeru_base(), audio), audio_path)
        srt_path = None
        text_node = row.get("text")
        if text_node:
            vtt_path = tmp / "subtitle.vtt"
            _kikoeru_download_to(
                _kikoeru_media_url(_kikoeru_base(), text_node), vtt_path
            )
            srt_text = _subtitle_to_srt(vtt_path.read_bytes())
            if srt_text.strip():
                srt_path = tmp / "subtitle.srt"
                with open(srt_path, "w", encoding="utf-8", newline="\n") as sf:
                    sf.write(srt_text)
        cmd = [
            _ffmpeg_path(),
            "-y",
            "-framerate", "25",
            "-loop", "1",
            "-i", str(cover),
            "-i", str(audio_path),
        ]
        vf = (
            "fps=25,scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        if srt_path:
            cmd += ["-i", str(srt_path)]
            vf += ",subtitles=subtitle.srt"
        cmd += [
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "stillimage",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(out),
        ]
        res = subprocess.run(
            cmd,
            cwd=str(tmp),
            timeout=1800,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode != 0 or not out.exists() or out.stat().st_size <= 0:
            err = (res.stderr or "").strip()[-1200:]
            raise RuntimeError(f"ffmpeg 打包失败：{err}")
        size_mb = out.stat().st_size / (1024 * 1024)
        return out, size_mb, bool(srt_path)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _build_kikoeru_track_audio(row: dict, output_format: str) -> tuple[Path, float, bool]:
    """Download an audio track and embed its timed captions.

    Args:
        row: Selected KikoFlu audio and subtitle metadata.
        output_format: mp3 or wav.

    Returns:
        Output path, size in MiB, and whether subtitles were embedded.

    Raises:
        RuntimeError: The audio could not be converted to the requested format.
    """
    import subprocess

    from .audio_subtitles import embed_timed_subtitles

    tmp = Path(tempfile.mkdtemp(prefix="kikoeru_", dir=str(_dl_dir())))
    basename = _kikoeru_sanitize_segment(Path(row["title"].replace("\\", "/")).stem)[:70]
    output = tmp / f"{row['work_id']}-{basename}.{output_format}"
    source_format = row["format"]
    source_path = output if source_format == output_format else tmp / f"source.{source_format}"
    try:
        base = _kikoeru_base()
        _kikoeru_download_to(_kikoeru_media_url(base, row["audio"]), source_path)
        if source_path != output:
            codec = ["-c:a", "libmp3lame", "-q:a", "2"] if output_format == "mp3" else ["-c:a", "pcm_s16le"]
            result = subprocess.run(
                [_ffmpeg_path(), "-nostdin", "-v", "error", "-i", str(source_path),
                 "-map", "0:a:0", *codec, str(output)],
                capture_output=True,
                timeout=1800,
                check=False,
            )
            if result.returncode or not output.is_file():
                detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
                raise RuntimeError(f"转换为 {output_format.upper()} 失败：{detail}")
        has_sub = False
        if text_node := row.get("text"):
            with _kikoeru_open(_kikoeru_media_url(base, text_node)) as response:
                content = response.read()
            language = "zho" if row.get("subtitle") == "中字" else "jpn"
            has_sub = embed_timed_subtitles(output, content, language)
        return output, output.stat().st_size / (1024 * 1024), has_sub
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


async def _download_and_send_kikoeru_track(
    session: str, hint: str, track: dict, output_format: str = "mp4", sound_effects: str = "auto"
) -> str:
    variants = track["variants"]
    if excluded := _kikoeru_excluded_tags():
        rejection = await asyncio.to_thread(_kikoeru_tag_rejection, variants[0]["work_id"], excluded)
        if rejection:
            return rejection
    if sound_effects == "auto":
        eligible = [item for item in variants if item["se"] == "有SE"] or variants
    else:
        wanted = "无SE" if sound_effects == "without" else "有SE"
        eligible = [item for item in variants if item["se"] == wanted]
        if not eligible:
            version = "无音效" if sound_effects == "without" else "有音效"
            return f"音轨《{hint}》没有{version}版本。"
    preferred_format = "mp3" if output_format == "mp4" else output_format
    row = min(eligible, key=lambda item: (item["format"] != preferred_format, not item.get("text")))
    if not row.get("text"):
        subtitle_source = next((item for item in eligible if item.get("text")), None)
        if subtitle_source:
            row = {**row, "text": subtitle_source["text"], "subtitle": subtitle_source["subtitle"]}

    format_name = output_format.upper()
    try:
        if output_format == "mp4":
            path, size_mb, has_sub = await asyncio.to_thread(_build_kikoeru_track_mp4, row)
        else:
            path, size_mb, has_sub = await asyncio.to_thread(
                _build_kikoeru_track_audio, row, output_format
            )
    except Exception as exc:
        logger.exception("Kikoeru track download failed (%s)", format_name)
        return f"KikoFlu 音轨《{hint}》制作 {format_name} 失败：{exc}"
    try:
        ok = await _send_track_media(session, path)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    sub_note = "（含字幕）" if has_sub else "（未找到可用字幕）"
    logger.info("Kikoeru track upload=%s format=%s size=%.1fMB subtitle=%s", ok, format_name, size_mb, has_sub)
    if not ok:
        return f"音轨《{hint}》已制成 {format_name}（{size_mb:.1f}MB），但发送失败，请检查平台文件大小和发送限制。"
    return f"已发送音轨《{hint}》的 {format_name}（{size_mb:.1f}MB）{sub_note}。"


async def _send_kikoeru_cards(session: str, results: list, *, numbered: bool = True) -> str:
    """Send one card per work in one AstrBot message.

    Args:
        session: Target AstrBot conversation.
        results: Work candidates in display order.
        numbered: Whether to display candidate numbers and save the candidate list.

    Returns:
        Status for the agent, or a text fallback when images cannot be sent.
    """
    from .work_cards import render_work_card

    limit = asyncio.Semaphore(4)
    if excluded := _kikoeru_excluded_tags():
        async def check(item: dict) -> str | None:
            if excluded.intersection(item.get("tag_aliases") or ()):
                return "这部作品命中了排除标签，未发送或下载。"
            async with limit:
                return await asyncio.to_thread(_kikoeru_tag_rejection, item["kikoeru_work_id"], excluded)

        rejected = await asyncio.gather(*(check(item) for item in results))
        results = [item for item, rejection in zip(results, rejected) if rejection is None]
        if not results:
            _recent_kikoeru_tracks.pop(session, None)
            _recent_search_results.pop(session, None)
            return "这一批没有符合排除标签设置的作品。" if numbered else rejected[0]

    def prepare(item: dict, index: int, directory: Path) -> tuple[dict, Path | None]:
        work_id = item["kikoeru_work_id"]
        work = item.get("card_work")
        rows = item.get("card_rows")
        if work is None or rows is None:
            try:
                work, rows = _kikoeru_load_track_rows(work_id)
            except Exception:
                logger.warning("Could not read track list for Kikoeru work %s", work_id, exc_info=True)
        entry = {**item, "card_work": work, "card_rows": rows}
        card_info = item
        if work and work.get("id") and work.get("title"):
            details = _kikoeru_result_from_work(work)
            card_info = {**item, **{key: value for key, value in details.items() if value}}

        cover = None
        try:
            cover = _kikoeru_download_cover(work_id, directory)
        except Exception:
            logger.warning("Could not download cover for Kikoeru work %s", work_id, exc_info=True)

        card = directory / "work.jpg"
        try:
            render_work_card(card_info, rows, cover, index if numbered else None, card)
        except Exception:
            logger.exception("Could not render card for Kikoeru work %s", work_id)
            card = None
        return entry, card

    with tempfile.TemporaryDirectory(prefix="kikoeru_cards_", dir=_dl_dir()) as tmp:
        async def prepare_one(index: int, item: dict) -> tuple[dict, Path | None]:
            directory = Path(tmp) / str(index)
            directory.mkdir()
            async with limit:
                return await asyncio.to_thread(prepare, item, index, directory)

        cards = await asyncio.gather(
            *(prepare_one(index, item) for index, item in enumerate(results, 1))
        )
        entries = [entry for entry, _ in cards]
        _recent_kikoeru_tracks.pop(session, None)
        if len(entries) == 1 and entries[0]["card_rows"]:
            entry = entries[0]
            _remember_kikoeru_tracks(
                session, entry["kikoeru_work_id"],
                (entry.get("card_work") or {}).get("title") or entry["title"], entry["card_rows"],
            )
        delivered = False
        images = [path for _, path in cards if path is not None]
        if _plugin_context and len(images) == len(entries):
            try:
                message = MessageEventResult()
                message.chain.extend(Image.fromFileSystem(str(path)) for path in images)
                delivered = await _plugin_context.send_message(session, message)
            except Exception:
                logger.exception("Kikoeru work cards could not be sent")
        if numbered:
            for entry in entries:
                entry["card_sent"] = delivered
            _remember_search_results(session, entries)

    if delivered:
        if numbered:
            for item in results:
                _kikoeru_pool_mark_seen(item)
            _kikoeru_pool_save_state()
        return f"已发送 {len(results)} 张作品卡片，无需重复列出。"
    if not numbered:
        entry = entries[0]
        if rows := entry["card_rows"]:
            return _format_kikoeru_tracks(entry["card_work"], rows)
        return f"暂时无法读取《{entry['title']}》的音轨，请稍后重试。"
    listing = "\n".join(
        f"第{index}部 {' '.join(item['title'].split())}"
        for index, item in enumerate(results, 1)
    )
    return f"KikoFlu 作品\n{listing}\n\n回复作品编号查看音轨。"


async def _kikoeru_show_batch(
    event: AstrMessageEvent, kind: str, sub: str, count: int, mode: str,
) -> str:
    """Send a page of cards and remember the source for direct follow-ups.

    Args:
        event: Message that requested the page.
        kind: Search or marked-list source.
        sub: Search keyword or marked-list filter.
        count: Requested batch size.
        mode: Sequential or random selection.

    Returns:
        Card send status, fallback list, or error text.
    """
    session = _session_umo(event)
    try:
        results, _ = await asyncio.to_thread(_kikoeru_pool_take_batch, kind, sub, count, mode)
    except Exception as exc:
        if "翻完" in str(exc):
            return str(exc)
        logger.exception("Kikoeru batch lookup failed")
        failure = "搜索失败" if kind == "search" else "读取 KikoFlu 我的标记失败"
        return f"{failure}：{exc}"
    if not results:
        if kind == "search":
            return f"没有找到与“{sub}”相关的结果。"
        return "KikoFlu 我的标记列表为空。"

    previous = _recent_search_results.get(session)
    response = await _send_kikoeru_cards(session, results)
    cards = _recent_search_results.get(session)
    if cards and cards is not previous:
        _kikoeru_recent_batches[session] = {
            "sender": event.get_sender_id(), "kind": kind, "sub": sub, "count": count,
            "cards": cards, "time": time.monotonic(),
        }
        for stale in _kikoeru_recent_batches.keys() - _recent_search_results.keys():
            _kikoeru_recent_batches.pop(stale, None)
    return response


async def _download_kikoeru_work_and_report(work_id: int, hint: str) -> str:
    if excluded := _kikoeru_excluded_tags():
        rejection = await asyncio.to_thread(_kikoeru_tag_rejection, work_id, excluded)
        if rejection:
            return rejection
    try:
        saved_dir, count, total_bytes = await asyncio.to_thread(
            _download_kikoeru_work, work_id
        )
    except Exception as exc:
        logger.exception("kikoeru_dl: work download failed")
        return f"KikoFlu 作品下载失败：{exc}"
    size_mb = total_bytes / (1024 * 1024)
    logger.info(
        "kikoeru_dl: saved=%s files=%d size=%.1fMB", saved_dir, count, size_mb
    )
    return (
        f"已完成：KikoFlu 作品《{hint}》已下载到 {saved_dir}，"
        f"共 {count} 个文件（{size_mb:.1f}MB）。"
    )


@pydantic_dataclass
class KikoeruSearchTool(FunctionTool[AstrAgentContext]):
    name: str = "kikoeru_search"
    description: str = (
        "搜索 KikoFlu 音声（名称、社团、标签、RJ 号）。普通搜索只选中文字幕；RJ 或完整标题直查作品。"
        "继续沿用关键词，随机时用 random。作品卡已发送时无需复述。失败时如实说明，不切换视频源。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "关键词或 RJ 号。",
                },
                "limit": {
                    "type": "integer",
                    "description": "用户指定的数量，默认 5。",
                },
                "pool_mode": {
                    "type": "string",
                    "enum": ["sequential", "random"],
                    "description": "顺序（默认）或随机。",
                },
            },
            "required": ["keyword"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        keyword = (kwargs.get("keyword") or "").strip()
        if not keyword:
            return "没有提供搜索关键词。"
        limit = int(kwargs.get("limit") or 5)
        limit = max(1, limit)
        pool_mode = (kwargs.get("pool_mode") or "sequential").strip().lower()
        event = _event_from_context(context)
        session = _session_umo(event)
        if not session:
            return "无法确定发送目标，请确认这条消息来自受支持的消息平台。"
        try:
            direct = await asyncio.to_thread(_kikoeru_search_direct, keyword)
        except Exception as exc:
            logger.exception("Kikoeru search failed")
            return f"搜索失败：{exc}"
        if direct:
            _recent_search_results.pop(session, None)
            _kikoeru_recent_batches.pop(session, None)
            return await _send_kikoeru_cards(session, [direct], numbered=False)
        if _kikoeru_normalize_rj(keyword):
            return f"没有找到作品 {keyword}。"
        return await _kikoeru_show_batch(event, "search", keyword, limit, pool_mode)


@pydantic_dataclass
class KikoeruListTool(FunctionTool[AstrAgentContext]):
    name: str = "kikoeru_list"
    description: str = (
        "读取 KikoFlu 在线标记或想听列表，发送作品卡。继续沿用筛选，随机时用 random。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "filter_name": {
                    "type": "string",
                    "enum": ["marked", "listening", "listened", "replay", "postponed", "all"],
                    "description": "marked 想听（默认），listening 在听，listened 听过，replay 重听，postponed 搁置，all 全部。",
                },
                "count": {
                    "type": "integer",
                    "description": "用户指定的数量，默认 5。",
                },
                "pool_mode": {
                    "type": "string",
                    "enum": ["sequential", "random"],
                    "description": "顺序（默认）或随机。",
                },
            },
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        filter_name = (kwargs.get("filter_name") or "marked").strip()
        count = int(kwargs.get("count") or 5)
        count = max(1, count)
        pool_mode = (kwargs.get("pool_mode") or "sequential").strip().lower()
        event = _event_from_context(context)
        if not _session_umo(event):
            return "无法确定发送目标，请确认这条消息来自受支持的消息平台。"
        return await _kikoeru_show_batch(event, "my", filter_name, count, pool_mode)


@pydantic_dataclass
class KikoeruMarkTool(FunctionTool[AstrAgentContext]):
    name: str = "kikoeru_mark"
    description: str = (
        "标记、收藏或取消标记 KikoFlu 作品。编号来自本会话最近一批卡片；过期则重新查询。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "works": {
                    "type": "string",
                    "description": "候选编号、RJ 号或作品链接；多部用逗号分隔。",
                },
                "status": {
                    "type": "string",
                    "enum": ["marked", "listening", "listened", "replay", "postponed", "remove"],
                    "description": "marked 想听（默认），listening 在听，listened 听过，replay 重听，postponed 搁置，remove 取消。",
                },
            },
            "required": ["works"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        works = (kwargs.get("works") or "").strip()
        if not works:
            return "没有提供要标记的作品。"
        status = (kwargs.get("status") or "marked").strip().lower()
        if status not in ("marked", "listening", "listened", "replay", "postponed", "remove"):
            return f"不支持的标记状态：{status}。"
        session = _session_umo(_event_from_context(context))
        if not session:
            return "无法确定发送目标，请确认这条消息来自受支持的消息平台。"
        work_ids, failed = _kikoeru_mark_resolve(works, session)
        if not work_ids:
            hint = ""
            if failed:
                hint = f"以下标识无法解析：{'、'.join(failed)}。"
            return (
                hint + "编号必须是最近一次 kikoeru_search / kikoeru_list 刚发候选里的第几条；"
                "如果候选已过期，请先重新搜索/读取列表，或直接提供 RJ 号、作品链接。"
            )
        results = await asyncio.to_thread(_kikoeru_mark_works, work_ids, status)
        labels = {
            "marked": "想听", "listening": "在听", "listened": "听过",
            "replay": "重听", "postponed": "搁置",
        }
        label = "取消标记" if status == "remove" else labels.get(status, status)
        ok = [r for r in results if r["ok"]]
        fail = [r for r in results if not r["ok"]]
        lines = [f"已把 {len(ok)} 个作品标记为「{label}】："]
        for r in ok:
            lines.append(f"- work {r['work_id']} 成功")
        if fail:
            lines.append(f"失败 {len(fail)} 个：")
            for r in fail:
                lines.append(f"- work {r['work_id']}：{r['detail']}")
        if failed:
            lines.append("无法解析的标识：" + "、".join(failed)
                         + "（候选过期请重新搜索后选编号，或提供 RJ 号/作品链接）")
        return "\n".join(lines)


@pydantic_dataclass
class KikoeruDownloadTool(FunctionTool[AstrAgentContext]):
    name: str = "kikoeru_download"
    description: str = (
        "按卡片编号下载 KikoFlu 音轨或整部。第2部第3号用 work_index=2、search_index=3、selection=track。"
        "多选用 tracks。多张卡片未选作品时先确认是哪一部。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "用户提供的 KikoFlu 作品链接。",
                },
                "search_index": {
                    "type": "integer",
                    "description": "按 selection 传作品编号或音轨编号。",
                },
                "work_index": {
                    "type": "integer",
                    "description": "指定的作品卡编号。",
                },
                "selection": {
                    "type": "string",
                    "enum": ["auto", "work", "track", "whole"],
                    "description": "work 选作品，track 下载音轨，whole 整部保存到本地，auto 自动判断。",
                },
                "tracks": {
                    "type": "string",
                    "description": "多音轨编号，如 3,5,8 或 2-6。",
                },
                "output_format": {
                    "type": "string",
                    "enum": list(_KIKOERU_OUTPUT_FORMATS),
                    "description": "仅用户明确指定时传；省略用插件默认格式。",
                },
                "sound_effects": {
                    "type": "string",
                    "enum": ["with", "without"],
                    "description": "with 有音效，without 无音效。",
                },
                "filename_hint": {
                    "type": "string",
                    "description": "可选标题。",
                },
            },
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        target_text = (kwargs.get("target") or "").strip()
        search_index = kwargs.get("search_index")
        hint = (kwargs.get("filename_hint") or "").strip() or target_text
        selection = (kwargs.get("selection") or "auto").strip().lower()
        sound_effects = (kwargs.get("sound_effects") or "auto").strip().lower()
        if sound_effects not in {"auto", "with", "without"}:
            return "请选择有音效或无音效。"
        output_format = (kwargs.get("output_format") or "").strip().lower()
        if output_format and output_format not in _KIKOERU_OUTPUT_FORMATS:
            return "请选择 MP4、MP3 或 WAV。"
        if not output_format:
            configured_format = str(_cfg("kikoeru_default_format", "mp4")).strip().lower()
            output_format = configured_format if configured_format in _KIKOERU_OUTPUT_FORMATS else "mp4"
        session = _session_umo(_event_from_context(context))
        if not session:
            return "无法确定发送目标，请确认这条消息来自受支持的消息平台。"

        def _to_index(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        index_value = _to_index(search_index)
        if index_value is None and target_text:
            m = re.fullmatch(r"第?\s*(\d+)\s*(?:个|条|号|项|吧)?", target_text)
            if m:
                index_value = int(m.group(1))

        tracks_text = (kwargs.get("tracks") or "").strip()
        track_numbers = _parse_kikoeru_track_numbers(tracks_text) if tracks_text else []
        url = target_text if target_text.startswith(("http://", "https://")) else ""
        track_ctx = _recent_kikoeru_tracks.get(session)
        explicit_work = kwargs.get("work_index")
        if explicit_work is not None:
            chosen = _resolve_search_result(session, _to_index(explicit_work))
            if not chosen:
                return "找不到这部作品，请使用最近一批卡片上的作品编号。"
            if selection == "whole":
                return await _download_kikoeru_work_and_report(chosen["kikoeru_work_id"], chosen["title"])
            if track_numbers or selection == "track" or (selection == "auto" and index_value is not None):
                work_id = chosen["kikoeru_work_id"]
                work, rows = chosen.get("card_work"), chosen.get("card_rows")
                if not rows:
                    try:
                        work, rows = await asyncio.to_thread(_kikoeru_load_track_rows, work_id)
                    except Exception as exc:
                        logger.exception("Kikoeru track list could not be read")
                        return f"读取 KikoFlu 音轨列表失败：{exc}"
                _remember_kikoeru_tracks(session, work_id, work.get("title") or chosen["title"], rows)
                track_ctx = _recent_kikoeru_tracks[session]
                selection = "track"
            else:
                index_value = _to_index(explicit_work)

        if track_numbers:
            if not track_ctx:
                return "请先让我读取 KikoFlu 音轨列表，再一次性选多条音轨。"
            bad = [n for n in track_numbers if not (1 <= n <= len(track_ctx["rows"]))]
            if bad:
                return f"音轨编号超出范围：{bad}，该作品共 {len(track_ctx['rows'])} 条音轨。"
            parts = []
            for n in track_numbers:
                row = track_ctx["rows"][n - 1]
                track_hint = f"{track_ctx.get('work_title') or hint}｜{row['title']}"
                parts.append(await _download_and_send_kikoeru_track(session, track_hint, row, output_format, sound_effects))
            return "多音轨下载完成：\n" + "\n".join(f"- {p}" for p in parts)

        auto_resolved = None
        if index_value is not None:
            auto_resolved = _resolve_search_result(session, index_value)

        if selection == "track":
            if (
                not track_ctx
                or index_value is None
                or not (1 <= index_value <= len(track_ctx["rows"]))
            ):
                return "没有找到对应的音轨。若有多张作品卡片，请同时给出作品和音轨编号，例如「第2部第3号」。"
            row = track_ctx["rows"][index_value - 1]
            track_hint = f"{track_ctx.get('work_title') or hint}｜{row['title']}"
            return await _download_and_send_kikoeru_track(session, track_hint, row, output_format, sound_effects)

        if (
            selection == "auto"
            and not auto_resolved
            and not url
            and track_ctx
            and index_value is not None
            and 1 <= index_value <= len(track_ctx["rows"])
        ):
            row = track_ctx["rows"][index_value - 1]
            track_hint = f"{track_ctx.get('work_title') or hint}｜{row['title']}"
            return await _download_and_send_kikoeru_track(session, track_hint, row, output_format, sound_effects)

        if selection in ("auto", "work", "whole") and not url and auto_resolved:
            url = auto_resolved.get("url") or ""
            hint = (auto_resolved.get("title") or "").strip() or hint

        if selection == "whole" and not target_text and index_value is None and track_ctx:
            return await _download_kikoeru_work_and_report(
                track_ctx["work_id"], track_ctx.get("work_title") or hint
            )

        if not url:
            return (
                "无法识别的 KikoFlu 作品链接。可以补上候选编号（如“第3条”）或直接给作品链接"
                "（如 https://api.asmr-200.com/api/work/1653004）。"
            )

        work_id = _kikoeru_work_id_from_url(url)
        if not work_id:
            return "KikoFlu 链接里没有作品编号，请用 search_index 选择候选后再下载。"
        if selection == "whole":
            return await _download_kikoeru_work_and_report(work_id, hint)
        item = {"kikoeru_work_id": work_id, "title": hint or f"KikoFlu {work_id}"}
        if auto_resolved and auto_resolved.get("kikoeru_work_id") == work_id:
            item = auto_resolved
            rows = item.get("card_rows")
            if rows and item.get("card_sent"):
                if excluded := _kikoeru_excluded_tags():
                    rejection = await asyncio.to_thread(_kikoeru_tag_rejection, work_id, excluded)
                    if rejection:
                        return rejection
                title = (item.get("card_work") or {}).get("title") or hint
                _remember_kikoeru_tracks(session, work_id, title, rows)
                return f"已选定《{title}》，请回复卡片上的音轨编号。"
        return await _send_kikoeru_cards(session, [item], numbered=False)




@register(
    "kikoeru_dl",
    "音声下载（KikoFlu）",
    "让AI检索KikoFlu音声库的中文字幕作品，并按音轨发送内嵌字幕的 MP3、WAV 或 MP4",
    "1.6.1",
    "",
)
class KikoeruPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        global _plugin_config, _plugin_context
        _kikoeru_pool_load_state()
        _plugin_config = self.config = config or {}
        _plugin_context = self.context
        self.search_tool = KikoeruSearchTool()
        self.download_tool = KikoeruDownloadTool()
        self.context.add_llm_tools(self.search_tool)
        self.context.add_llm_tools(KikoeruListTool())
        self.context.add_llm_tools(KikoeruMarkTool())
        self.context.add_llm_tools(self.download_tool)

    @filter.regex(re.compile(
        rf"^(?:{_KIKOERU_QUICK_RJ}|{_KIKOERU_QUICK_NEXT}|{_KIKOERU_QUICK_TRACK})$",
        re.IGNORECASE,
    ))
    async def quick_kikoeru(self, event: AstrMessageEvent) -> None:
        if not event.is_at_or_wake_command:
            return
        session = _session_umo(event)
        if not session:
            return
        message = event.get_message_str().strip()
        keyword = _kikoeru_normalize_rj(message)
        track = re.fullmatch(_KIKOERU_QUICK_TRACK, message, re.IGNORECASE)
        if not (keyword or track or re.fullmatch(_KIKOERU_QUICK_NEXT, message)):
            return
        batch = _kikoeru_recent_batches.get(session)
        if not keyword and (
            not batch
            or batch["sender"] != event.get_sender_id()
            or batch["cards"] is not _recent_search_results.get(session)
            or time.monotonic() - batch["time"] > _KIKOERU_QUICK_WINDOW_SECONDS
        ):
            return

        event.stop_event()
        try:
            if keyword or track:
                context = ContextWrapper(AstrAgentContext(context=self.context, event=event))
                if keyword:
                    result = await self.search_tool.call(context, keyword=keyword.upper())
                else:
                    effects = {"有音效": "with", "无音效": "without"}.get(track.group("effects"))
                    result = await self.download_tool.call(
                        context, selection="track", work_index=int(track.group("work")),
                        search_index=int(track.group("track")), output_format=track.group("format"),
                        sound_effects=effects,
                    )
            else:
                mode = "random" if message.startswith("随机") else "sequential"
                result = await _kikoeru_show_batch(event, batch["kind"], batch["sub"], batch["count"], mode)
        except Exception:
            logger.exception("Kikoeru direct request failed")
            result = "KikoFlu 请求处理失败，请稍后重试。"
        if isinstance(result, str) and not result.startswith(("已发送 ", "已发送音轨")):
            await event.send(MessageEventResult().message(result))
