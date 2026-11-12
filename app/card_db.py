# -*- coding: utf-8 -*-
"""卡牌数据库：从游戏本地 data_definitions（ZIP 包）解析全部卡牌的中文名/人口数/战力。

数据完全来自用户本机游戏文件，保证与当前游戏版本一致。
"""
import csv
import difflib
import io
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

FACTION_BITS = {
    1: "中立",
    2: "怪兽",
    4: "尼弗迦德",
    8: "北方领域",
    16: "松鼠党",
    32: "史凯利格",
    64: "辛迪加",
}

# 常见安装位置，用于自动探测游戏目录
COMMON_GAME_PATHS = [
    r"D:\Program Files (x86)\GOG Galaxy\Games\Gwent",
    r"C:\Program Files (x86)\GOG Galaxy\Games\Gwent",
    r"G:\steam\steamapps\common\GWENT The Witcher Card Game",
    r"C:\Program Files (x86)\Steam\steamapps\common\GWENT The Witcher Card Game",
    r"D:\Steam\steamapps\common\GWENT The Witcher Card Game",
    r"E:\Steam\steamapps\common\GWENT The Witcher Card Game",
]


def find_game_dir():
    """自动探测游戏目录（含 data_definitions 的即为有效目录）。"""
    candidates = list(COMMON_GAME_PATHS)
    # 从注册表/常见盘符补充搜索 Steam 库
    try:
        import winreg
        for hive, key in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
        ]:
            try:
                with winreg.OpenKey(hive, key) as k:
                    steam = winreg.QueryValueEx(k, "InstallPath")[0]
                    candidates.append(os.path.join(steam, "steamapps", "common", "GWENT The Witcher Card Game"))
                    vdfs = os.path.join(steam, "steamapps", "libraryfolders.vdf")
                    if os.path.exists(vdfs):
                        txt = open(vdfs, encoding="utf-8", errors="ignore").read()
                        for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                            p = m.group(1).replace("\\\\", "\\")
                            candidates.append(os.path.join(p, "steamapps", "common", "GWENT The Witcher Card Game"))
            except OSError:
                pass
    except ImportError:
        pass
    for c in candidates:
        if os.path.exists(os.path.join(c, "Gwent.exe")):
            return c
    return None


class CardDB:
    def __init__(self, cards):
        # cards: list of dict
        self.cards = cards
        self.by_id = {c["id"]: c for c in cards}
        self._exact = {}
        for c in cards:
            self._exact.setdefault(c["name_zh"], c)
        self._names_sorted = sorted(self._exact.keys(), key=len, reverse=True)

    # ---------- 构建与加载 ----------
    @staticmethod
    def from_game_dir(game_dir):
        path = os.path.join(game_dir, "Gwent_Data", "StreamingAssets", "data_definitions")
        if not os.path.exists(path):
            raise FileNotFoundError("未找到 data_definitions: %s" % path)
        with zipfile.ZipFile(path) as z:
            xml_text = z.read("Templates.xml").decode("utf-8")
            loc_text = z.read("Localization/zh-cn.csv").decode("utf-8-sig")

        names = {}
        for row in csv.reader(io.StringIO(loc_text), delimiter=";"):
            if len(row) >= 2 and row[0].endswith("_name"):
                cid = row[0][: -len("_name")]
                if cid.isdigit() and row[1] and row[1] not in ("n/a", "Ignore."):
                    names[cid] = row[1]

        cards = []
        root = ET.fromstring(xml_text)
        for tpl in root.iter("Template"):
            if tpl.get("Type") != "CardTemplate":
                continue
            cid = tpl.get("Id", "")
            name_zh = names.get(cid)
            if not name_zh:
                continue
            def _int(tag, default=-1):
                el = tpl.find(tag)
                try:
                    return int(el.text)
                except (TypeError, ValueError, AttributeError):
                    return default
            prov = _int("Provision", 0)
            power = _int("Power", -1)
            fac_bits = _int("FactionId", 1)
            if fac_bits < 0:
                fac_bits = 1
            factions = [n for b, n in FACTION_BITS.items() if fac_bits & b]
            cards.append({
                "id": cid,
                "name_zh": name_zh,
                "name_en": tpl.get("DebugName", ""),
                "provision": prov,
                "power": power,
                "faction": "/".join(factions) or "中立",
                "availability": int(tpl.get("Availability", "0")),
            })
        return CardDB(cards)

    @staticmethod
    def from_json(path):
        with open(path, encoding="utf-8") as f:
            return CardDB(json.load(f))

    def save_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.cards, f, ensure_ascii=False, separators=(",", ":"))

    # ---------- 查询 ----------
    def exact(self, text):
        return self._exact.get(text.strip())

    def best_match(self, text, threshold=0.80):
        """OCR 文本 → 最匹配的卡牌。短名（<=2字）仅接受精确匹配，避免误配。"""
        t = re.sub(r"[\s:：·，,。.!！?？'’\-－]+", "", text.strip())
        if not t:
            return None
        hit = self._exact.get(t)
        if hit:
            return hit
        if len(t) <= 2:
            return None
        best, best_ratio = None, 0.0
        for name in self._names_sorted:
            if abs(len(name) - len(t)) > max(2, len(t) // 3):
                continue
            r = difflib.SequenceMatcher(None, t, name).ratio()
            if r > best_ratio:
                best, best_ratio = name, r
        if best and best_ratio >= threshold:
            return self._exact[best]
        return None

    def search(self, keyword, limit=30):
        kw = keyword.strip()
        if not kw:
            return []
        out = [c for c in self.cards if kw in c["name_zh"]]
        if not out:
            for c in self.cards:
                if kw.lower() in c["name_en"].lower():
                    out.append(c)
        return out[:limit]

    def search_fuzzy(self, keyword, limit=30):
        """模糊搜索：子串匹配（中文→英文），无结果时 difflib 相似度兜底
        （先中文名后英文名，均忽略空格与标点差异）。"""
        out = self.search(keyword, limit)
        if out:
            return out
        kw = re.sub(r"[\s:：·，,。.!！?？'’\-－]+", "", keyword.strip())
        if not kw:
            return []
        close = difflib.get_close_matches(kw, self._names_sorted,
                                          n=limit, cutoff=0.4)
        out = [self._exact[n] for n in close if n in self._exact]
        if not out:
            # 英文名模糊兜底（用户输入英文/拼音式卡名时）
            if not hasattr(self, "_en_index"):
                idx = {}
                for c in self.cards:
                    en = re.sub(r"[^A-Za-z0-9]+", "", c["name_en"].lower())
                    if en:
                        idx.setdefault(en, c)
                self._en_index = idx
            en = re.sub(r"[^A-Za-z0-9]+", "", kw.lower())
            if len(en) >= 2:
                close = difflib.get_close_matches(
                    en, list(self._en_index.keys()), n=limit, cutoff=0.6)
                out = [self._en_index[k] for k in close if k in self._en_index]
        return out[:limit]


def build_or_load(game_dir, cache_path):
    """优先用缓存；游戏目录存在则尝试重建（版本更新后自动刷新）。"""
    err = None
    if game_dir:
        try:
            db = CardDB.from_game_dir(game_dir)
            db.save_json(cache_path)
            return db, None
        except Exception as e:  # noqa: BLE001
            err = e
    if os.path.exists(cache_path):
        return CardDB.from_json(cache_path), err
    raise RuntimeError("无法构建卡牌数据库: %s" % (err or "缺少游戏目录与缓存"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    gdir = sys.argv[1] if len(sys.argv) > 1 else find_game_dir()
    db = CardDB.from_game_dir(gdir)
    collectible = [c for c in db.cards if c["availability"] > 0]
    print("卡牌总数(含衍生):", len(db.cards), " 可收集:", len(collectible))
    for c in db.cards[:5]:
        print(c)
    print("匹配测试:")
    for t in ["利维亚的杰洛特", "利维亚杰洛特", "里维亚的杰洛特", "霜", "维伦特瑞坦梅斯"]:
        m = db.best_match(t)
        print(" ", t, "->", m["name_zh"] if m else None,
              "人口", m["provision"] if m else "-")
