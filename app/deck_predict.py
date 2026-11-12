# -*- coding: utf-8 -*-
"""对手卡组预测：拉取 playgwent 官方热门卡组库，按对手已出牌匹配最符合的卡组。

数据源（playgwent 官方页面 API，卡牌模板 ID 与游戏内存读取同一空间）：
  列表 GET /zh-cn/decks/api/guides/offset/{off}/limit/{n}
       -> {guides: [{id, leaderId, faction:{short}, votes, published, hidden, invalid, ...}]}
  详情 GET /zh-cn/decks/api/guides/{id}
       -> {deck: {leader:{id, localizedName}, stratagem, provisionsCost,
                  cards: [{id, localizedName, provisionsCost, repeatCount}]}}
本地缓存 decks_cache.json，超过 CACHE_TTL 后需刷新。
"""
import http.client
import json
import threading
import time

BASE_HOST = "www.playgwent.com"
BASE_PATH = "/zh-cn/decks/api"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
      "Accept-Language": "zh-CN,zh;q=0.9"}
CACHE_TTL = 12 * 3600     # 缓存有效期 12 小时
LIST_PAGE = 500           # 列表页大小
FETCH_N = 120             # 抓取详情的热门卡组数量（服务端对单IP限速，多抓无益）
WORKERS = 12              # 详情并发数（keep-alive 连接复用）

FACTION_ZH = {
    "mon": "怪物", "nil": "尼弗迦德", "nor": "北方领域", "sco": "松鼠党",
    "ske": "史凯利格", "sy": "辛迪加", "syn": "辛迪加", "neu": "中立",
}
SCHEMA = 4                # 缓存结构版本（结构变化时递增，旧缓存自动失效）

# 每线程复用一条 HTTPS 长连接（urllib 每次请求都要完整 TLS 握手，是下载慢的主因）
_tls = threading.local()


def _conn():
    c = getattr(_tls, "conn", None)
    if c is None:
        c = _tls.conn = http.client.HTTPSConnection(BASE_HOST, timeout=25)
    return c


def _drop_conn():
    try:
        if getattr(_tls, "conn", None):
            _tls.conn.close()
    except Exception:  # noqa: BLE001
        pass
    _tls.conn = None


def _daemon_map(fn, items, workers=8, raise_exc=True):
    """daemon 线程版并发 map（保持结果顺序）。

    不用 concurrent.futures：其 worker 是非 daemon 线程，解释器退出时会
    被 join —— 网络请求卡住时主线程跟着卡死（表现为关不掉的“未响应”）。
    fn 抛异常时结果位存异常对象；raise_exc=True 时 join 后重抛第一个。
    """
    items = list(items)
    results = [None] * len(items)
    lock = threading.Lock()
    nxt = [0]

    def worker():
        while True:
            with lock:
                i = nxt[0]
                nxt[0] += 1
            if i >= len(items):
                return
            try:
                results[i] = fn(items[i])
            except Exception as e:  # noqa: BLE001
                results[i] = e

    ts = [threading.Thread(target=worker, daemon=True)
          for _ in range(max(1, min(workers, len(items))))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if raise_exc:
        for r in results:
            if isinstance(r, BaseException):
                raise r
    return results


def _get(path, retries=3):
    """请求 BASE_PATH 下的 JSON 接口，keep-alive + 3 次递增退避重试。"""
    last = None
    for i in range(retries):
        try:
            c = _conn()
            c.request("GET", BASE_PATH + path, headers=UA)
            r = c.getresponse()
            data = r.read()
            if r.status != 200:
                raise OSError("HTTP %d" % r.status)
            return json.loads(data.decode("utf-8", "replace"))
        except Exception as e:   # noqa: BLE001
            last = e
            _drop_conn()         # 长连接失效时重建
            # 限流/反爬时延长退避，避免连续请求被 403
            wait = 2.5 * (i + 1) if (isinstance(e, OSError)
                                     and str(e) in ("HTTP 403", "HTTP 429")) \
                else 0.5 * (i + 1)
            time.sleep(wait)
    raise last


def fetch_library(progress=None, max_decks=FETCH_N, cache_path=None,
                  on_partial=None):
    """抓取热门卡组库（阻塞，需在后台线程调用）。

    progress(done, total) 汇报进度；cache_path 给定时每 20 套增量落盘一次，
    中断/失败也能保留已抓到的部分；on_partial(decks) 在每个批次后回调
    当前进度数据，供界面提前启用预测（边下边用）。
    """
    def _snapshot(dks):
        if cache_path and dks:
            try:
                save_cache(cache_path, dks)
            except OSError:
                pass

    # 列表页并发抓取
    pages = _daemon_map(
        lambda off: _get("/guides/offset/%d/limit/%d" % (off, LIST_PAGE)),
        (0, LIST_PAGE), workers=2)
    guides = []
    for data in pages:
        guides.extend(data.get("guides") or [])
    pool = [g for g in guides
            if g.get("published") and not g.get("hidden") and not g.get("invalid")]
    pool.sort(key=lambda g: (g.get("votes", 0), g.get("createdAt", "")), reverse=True)
    pool = pool[:max_decks]

    decks = []
    total = len(pool)
    lock = threading.Lock()
    done = [0]

    def work(g):
        try:
            d = _get("/guides/%d" % g["id"])
            deck = d.get("deck") or {}
            leader = deck.get("leader") or {}
            cards, provs, golds, imgs = {}, {}, [], {}
            for c in deck.get("cards") or []:
                cid = str(c.get("id"))
                if not cid:
                    continue
                # repeatCount 是“额外复制数”：金卡 0 张额外(1张)，铜卡 1 张额外(2张)
                n = int(c.get("repeatCount", 0)) + 1
                cards[cid] = cards.get(cid, 0) + n
                provs[cid] = int(c.get("provisionsCost", 0))
                if c.get("cardGroup") == "gold":
                    golds.append(cid)
                pi = c.get("previewImg")
                if isinstance(pi, dict) and (pi.get("big") or pi.get("small")):
                    # 优先用 big（更高清，适合整行平铺；small 仅 188×270 拉糊）
                    imgs[cid] = pi.get("big") or pi.get("small")
            if not cards or not leader.get("id"):
                return None
            f = (g.get("faction") or {}).get("short") or ""
            return {"id": g["id"],
                    "leader": str(leader.get("id")),
                    "leader_name": leader.get("localizedName") or "",
                    "faction": f,
                    "cards": cards, "provs": provs, "gold": golds,
                    "img": imgs,
                    "prov_total": int(deck.get("provisionsCost", 0)),
                    "votes": g.get("votes", 0),
                    "date": (g.get("createdAt") or "")[:10]}
        except Exception:  # noqa: BLE001
            return None

    for r in _daemon_map(work, pool, workers=WORKERS):
        with lock:
            done[0] += 1
            if done[0] % 20 == 0 or done[0] == total:
                if progress:
                    progress(done[0], total)
                _snapshot(decks)
                if on_partial:
                    try:
                        on_partial(list(decks))
                    except Exception:  # noqa: BLE001
                        pass
        if r:
            decks.append(r)
    # 最终结果补一次落盘/回调（循环内最后一次 snapshot 时末尾结果还没 append）
    if progress:
        progress(done[0], total)
    _snapshot(decks)
    if on_partial:
        try:
            on_partial(list(decks))
        except Exception:  # noqa: BLE001
            pass
    return decks


def fetch_hot_ids(limit=FETCH_N):
    """仅抓列表页，返回当前热门卡组 id 有序列表（轻量，用于更新检测）。

    与 fetch_library 的筛选规则一致：只统计 published 且未隐藏/无效的卡组，
    按热度排序取前 limit 个。单次 2 个请求，适合定期轮询。
    """
    guides = []
    pages = _daemon_map(
        lambda off: _get("/guides/offset/%d/limit/%d" % (off, LIST_PAGE)),
        (0, LIST_PAGE), workers=2)
    for data in pages:
        guides.extend(data.get("guides") or [])
    pool = [g for g in guides
            if g.get("published") and not g.get("hidden") and not g.get("invalid")]
    pool.sort(key=lambda g: (g.get("votes", 0), g.get("createdAt", "")), reverse=True)
    return [str(g["id"]) for g in pool[:limit] if g.get("id")]


def save_cache(path, decks):
    json.dump({"schema": SCHEMA, "fetched_at": time.time(), "decks": decks},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)


def load_cache(path):
    """返回 (decks, fetched_at)；无缓存或版本不符时 ([], 0)。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
        if d.get("schema") != SCHEMA:
            return [], 0
        return d.get("decks") or [], d.get("fetched_at", 0)
    except Exception:  # noqa: BLE001
        return [], 0


def load_builtin(path):
    """加载随 exe 内置的离线卡组库（decks_builtin.json）。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("schema") != SCHEMA:
            return [], 0
        return d.get("decks") or [], d.get("fetched_at", 0)
    except Exception:  # noqa: BLE001
        return [], 0


def merge_libraries(primary, secondary):
    """合并两个卡组库：primary（服务端最新）优先，按卡组 id 去重补充。

    注意不能按 leader 去重：热门卡组集中于少数领袖，按 leader 去重会把
    几百套合并到几十套，冷门卡表全部丢失；同领袖不同卡表是不同卡组。
    """
    seen = set()
    out = []
    for d in list(primary) + list(secondary):
        did = str(d.get("id") or "")
        if not did:
            # 无 id 时退化为 leader+卡表指纹去重
            did = "%s|%s" % (d.get("leader"),
                             ",".join("%s:%s" % kv
                                      for kv in sorted((d.get("cards") or {}).items())))
        if did in seen:
            continue
        seen.add(did)
        out.append(d)
    return out


def predict(decks, played, leader=None, top=3):
    """按对手已出牌匹配最优卡组。

    played: {卡牌模板ID(str): 张数}；leader: 对手领袖模板ID(str|None)。
    返回 [{leader, leader_name, faction, matched, possible, conf, missing}]
    missing: [(卡ID, 库内张数, 人口)] 按人口降序，对手还没打出来的牌。
    """
    if not played:
        return []
    if leader:
        lead_pool = [d for d in decks if d["leader"] == str(leader)]
        if len(lead_pool) >= 3:      # 领袖能锁定足够多卡组时才按领袖过滤
            decks = lead_pool
    possible = sum(played.values())
    out = []
    for d in decks:
        cs = d["cards"]
        matched_copies = matched_distinct = 0
        for tid, n in played.items():
            have = cs.get(tid, 0)
            if have:
                matched_distinct += 1
                matched_copies += min(n, have)
        score = matched_distinct + 0.25 * (matched_copies - matched_distinct)
        if leader and d["leader"] == str(leader):
            score += 1.5             # 领袖一致加分
        missing = [(tid, n, d["provs"].get(tid, 0)) for tid, n in cs.items()
                   if played.get(tid, 0) < n]
        missing.sort(key=lambda x: (-x[2], -x[1]))
        out.append({"score": score, "matched": matched_copies,
                    "possible": possible,
                    "conf": min(1.0, matched_copies / max(1, possible)),
                    "leader": d["leader"], "leader_name": d["leader_name"],
                    "faction": d["faction"], "missing": missing[:6],
                    "cards": dict(cs), "provs": dict(d["provs"]),
                    "gold": set(d.get("gold") or []),
                    "img": dict(d.get("img") or {})})
    out.sort(key=lambda x: -x["score"])
    return out[:top]
