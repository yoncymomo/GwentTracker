# -*- coding: utf-8 -*-
"""内存读取数据源：通过 frida 附加到 Gwent.exe，调用 il2cpp 运行时 API
定位 BattleHandler 单例 → GameController → BattleHistory → Entries，
从出牌历史条目中直接读取对手打出的卡牌模板 ID，取代 OCR 方案。

注意：本模块只做内存【读取】，不写入、不修改游戏代码。
偏移定义在 offsets.json，游戏版本更新后只需更新该文件。
"""
import json
import os
import sys
import threading
import time

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OFFSETS_PATH = os.path.join(BASE_DIR, "offsets.json")
DEBUG_LOG = os.path.join(BASE_DIR, "agent_debug.log")


def _dbg(msg):
    """诊断日志：窗口化 exe 无控制台，消息回调线程的异常会被 frida 吞掉，
    全部落到本地文件便于排查（日志很小：仅事件摘要，按天人工清理）。"""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except OSError:
        pass

DEFAULT_OFFSETS = {
    # il2cpp 运行时结构（metadata v29 / Unity 2021）
    "klass_parent": 0x58,
    "klass_static_fields": 0xB8,
    # BattleHandler (GwentVisuals.BattleHandler : ASingleton<BattleHandler>)
    # 这两个字段在 setup() 里按字段名动态解析并覆盖，以下仅是解析失败的回退值：
    # 新版游戏（Unity 运行时升级，MonoBehaviour 布局 +8）实测 0x30/0x130，
    # 旧版游戏为 0x28/0x128 —— 动态解析两种版本通吃
    "battlehandler_gamecontroller": 0x30,
    "battlehandler_localuser": 0x130,
    # Player (GwentGameplay.Player)
    "player_id": 0x18,
    "player_deckstate": 0x28,
    # GameController (GwentGameplay.GameController)
    "gamecontroller_playermanager": 0x60,
    "gamecontroller_cardmanager": 0x70,
    "gamecontroller_battlehistory": 0xB0,
    # PlayerManager
    "playermanager_players": 0x24,   # PlayerManager.Players（字段名动态解析实测 0x24，旧 0x18 读偏导致对局信息全空）
    # BattleDeck（领袖为内联结构体，首字段 TemplateId）
    "deck_leader_ability": 0x14,
    # CardManager
    "cardmanager_allcards": 0x28,
    # BattleHistory
    "battlehistory_entries": 0x18,
    # List<T> / 数组
    "list_items": 0x10,
    "list_size": 0x18,
    "array_elements": 0x20,
    # CardPlayedHistoryEntry
    "played_player_id": 0x10,
    "played_card_original": 0x18,   # OriginalCard（ClonedCard 为空时兜底）
    "played_card": 0x20,            # ClonedCard（出牌快照）
    # CardMovedHistoryEntry（撕牌/弃牌/移动——手牌→墓地=对手撕牌，需计入对手已出牌）
    # 权威来源：Il2CppDumper 对新版 Assembly-CSharp 的 dump（diag_hist 实测）
    "cardmoved_card": 0x10,
    "cardmoved_from": 0x18,          # CardPosition（+0x0 PlayerId / +0x4 Location）
    "cardmoved_to": 0x24,            # CardPosition（+0x0 PlayerId / +0x4 Location）
    # ELocation 枚举（CardPosition.Location）
    "loc_hand": 8,
    "loc_deck": 16,
    "loc_graveyard": 32,
    # CardSpawnedHistoryEntry（开局发牌/生成事件，仅用于建立真实卡牌实例集合）
    "spawned_card": 0x10,
    "spawned_spawner": 0x18,
    "spawned_pos": 0x20,       # CardPosition（+0x4 为 Location）
    # RoundStartedHistoryEntry
    "round_id": 0x10,
    # Card
    "card_template": 0x48,
    "card_definition": 0x50,   # 内联结构体，首字段 TemplateId
    "card_instance_id": 0x58,
    "card_pos_player": 0x5C,   # CardPosition.PlayerId（所在半场）
    "card_pos_location": 0x60, # CardPosition.Location（ELocation：1近战/2远程/8手牌/16牌组/32墓地/64领袖）
    "card_played_by": 0x1D8,   # EPlayerId PlayedBy（生成牌的归属）
    # CardTemplate (GwentGameplay.CardTemplate)
    "template_id": 0x10,
    "template_tier": 0x48,      # ECardTier：8=Gold（金卡）
    "template_power": 0x54,
    "template_provision": 0x5C,
    # Card → CardData → CardPower.CurrentPower（双方实时点数用）
    "card_data": 0xA0,
    "carddata_power": 0x20,
    "cardpower_current": 0x1C,
    # 数组（Il2CppArray）长度
    "array_length": 0x18,
    # ---- CollectionManager（构筑界面卡组库直读，登录后数据常驻内存）----
    "cm_controller": 0x38,     # CollectionManager.CurrentController（probeDecks 里按字段名动态解析，此为回退值）
    "acd_data": 0x28,          # ACollectionController.Data
    "cd_decks": 0x28,          # CollectionData.m_Decks : Dictionary<ulong, CollectionDeck>
    "cd_activeid": 0x50,       # CollectionData.ActiveDeckId
    "deck_name": 0x48,         # CollectionDeck.Name : System.String
    "deck_faction": 0x50,      # CollectionDeck.Faction : EFactionId（位标志）
    "deck_leader": 0x60,       # CollectionDeck._LeaderCard : CollectionCard
    "deck_strat": 0x68,        # CollectionDeck.StratagemCard
    "deck_ability": 0x70,      # CollectionDeck.AbilityId
    "deck_cards": 0x78,        # CollectionDeck.m_Cards : Dictionary<CollectionCard, int>
    "collcard_card": 0x20,     # CollectionCard.Card : GwentGameplay.Card
    "dict_entries": 0x18,      # Dictionary`2.entries（metadata v29 布局）
    "dict_count": 0x20,        # Dictionary`2.count
    "string_chars": 0x14,      # System.String 首字符（+0x10 为长度）
    # 回合 ID 起始值（GWENT RoundID 从 1 开始，故 1 → 第 1 小局）
    "round_id_base": 1,
    # ---- 对局信息扩展（diag 实测 2026-11：PlayerInfo/PlayerManager/GameInstance） ----
    "gamecontroller_gameinstance": 0x30,   # GameController.GameInstance
    "gameinstance_gamemode": 0x38,         # GameInstance._gameMode（byte，EGameMode）
    "pm_localuserid": 0x29,                # PlayerManager.LocalUserId（byte）
    "pm_currentplayerid": 0x2C,            # PlayerManager.CurrentPlayerId（byte，回合归属）
    "player_info": 0x60,                   # Player.Info → PlayerInfo
    "player_crowns": 0x24,                 # Player.Crowns（int32，已赢小局数）
    "player_haspassed": 0x19,              # Player.HasPassed（bool）
    "player_hasendedturn": 0x1A,           # Player.HasEndedTurn（bool）
    "pinfo_name": 0x10,                    # PlayerInfo.Name（System.String）
    "pinfo_level": 0x30,                   # PlayerInfo.Level（int32）
    "pinfo_mmr": 0x38,                     # PlayerInfo.MMR（int32，职天 > 0）
    "pinfo_rank": 0x3C,                    # PlayerInfo.Rank（int32）
    # ---- 对局结果（diag 实测 2026-11：RoundEndedHistoryEntry / GameEndedHistoryEntry / RoundInfo）----
    # 权威小局比分与胜负判定：不再依赖场上力量扫描（漏攻城排、投降时清零导致判错）
    "roundended_roundinfo": 0x10,     # RoundEndedHistoryEntry.RoundInfo（引用类型指针）
    "roundinfo_id": 0x10,             # RoundInfo.Id（int32）
    "roundinfo_playerscores": 0x18,   # RoundInfo.PlayerScores（Int32[] 指针，[P1,P2]）
    "roundinfo_winnerid": 0x21,       # RoundInfo.WinnerId（byte，EPlayerId）
    "gameended_winnerid": 0x10,       # GameEndedHistoryEntry.WinnerID（byte，EPlayerId）
    "gameended_reason": 0x11,         # GameEndedHistoryEntry.Reason（byte，EEndGameReason）
}

# JS agent：{} 占位由 python 端注入偏移 JSON
AGENT_JS = r"""
var OFF = __OFFSETS__;
var PTR = Process.pointerSize;
var api = null;   // 导出函数延迟解析：游戏启动初期 GameAssembly.dll 尚未加载，此时不抛错，等下一帧
var NAMES = [
    'il2cpp_domain_get', 'il2cpp_domain_get_assemblies', 'il2cpp_assembly_get_image',
    'il2cpp_image_get_name', 'il2cpp_class_from_name', 'il2cpp_class_get_static_field_data',
    'il2cpp_class_get_name', 'il2cpp_class_get_fields', 'il2cpp_field_get_name',
    'il2cpp_field_get_offset'
];

function findExport(name) {
    // 兼容各版本 frida：17+ 只有模块实例方法 / 全局查找
    try {
        var mod = Process.getModuleByName('GameAssembly.dll');
        if (mod.findExportByName) {
            var p0 = mod.findExportByName(name);
            if (p0) return p0;
        }
    } catch (e) { }
    try {
        if (typeof Module.findExportByName === 'function') {
            var p1 = Module.findExportByName('GameAssembly.dll', name);
            if (p1) return p1;
        }
    } catch (e) { }
    try {
        if (typeof Module.getGlobalExportByName === 'function') {
            var p2 = Module.getGlobalExportByName(name);
            if (p2) return p2;
        }
    } catch (e) { }
    return null;
}

// 预先分配 C 字符串常量，供 il2cpp_class_from_name 使用
var STR_NS_VIS, STR_NS_GP, STR_BH, STR_PLAYED, STR_SPAWNED, STR_ROUND, STR_MOVED,
    STR_ROUNDENDED, STR_GAMEENDED;
// 对局结束条目的候选类名（历史条目列表末尾出现 = 本局结束），
// 逐一尝试解析，命中任意一个即可即时识别结算画面
var MO_NAMES = ['MatchOverHistoryEntry', 'MatchEndedHistoryEntry',
                'GameEndedHistoryEntry', 'MatchFinishedHistoryEntry'];
var f_domain_get, f_domain_asms, f_asm_image, f_image_name,
    f_class_from_name, f_static_data, f_class_name,
    f_class_fields, f_field_name, f_field_off;

function initExports() {
    if (api) return true;
    try {
        if (!Process.findModuleByName('GameAssembly.dll')) return false;
    } catch (e) { return false; }
    var ok = true;
    api = {};
    NAMES.forEach(function (n) {
        var p = findExport(n);
        if (p) api[n] = p; else ok = false;
    });
    if (!ok) { api = null; return false; }   // 引擎加载中：下一帧重试
    f_domain_get = new NativeFunction(api.il2cpp_domain_get, 'pointer', []);
    f_domain_asms = new NativeFunction(api.il2cpp_domain_get_assemblies, 'pointer', ['pointer', 'pointer']);
    f_asm_image = new NativeFunction(api.il2cpp_assembly_get_image, 'pointer', ['pointer']);
    f_image_name = new NativeFunction(api.il2cpp_image_get_name, 'pointer', ['pointer']);
    f_class_from_name = new NativeFunction(api.il2cpp_class_from_name, 'pointer', ['pointer', 'pointer', 'pointer']);
    f_static_data = new NativeFunction(api.il2cpp_class_get_static_field_data, 'pointer', ['pointer']);
    f_class_name = new NativeFunction(api.il2cpp_class_get_name, 'pointer', ['pointer']);
    f_class_fields = new NativeFunction(api.il2cpp_class_get_fields, 'pointer', ['pointer', 'pointer']);
    f_field_name = new NativeFunction(api.il2cpp_field_get_name, 'pointer', ['pointer']);
    f_field_off = new NativeFunction(api.il2cpp_field_get_offset, 'int32', ['pointer']);
    STR_NS_VIS = Memory.allocUtf8String('GwentVisuals');
    STR_NS_GP = Memory.allocUtf8String('GwentGameplay');
    STR_BH = Memory.allocUtf8String('BattleHandler');
    STR_PLAYED = Memory.allocUtf8String('CardPlayedHistoryEntry');
    STR_SPAWNED = Memory.allocUtf8String('CardSpawnedHistoryEntry');
    STR_ROUND = Memory.allocUtf8String('RoundStartedHistoryEntry');
    STR_MOVED = Memory.allocUtf8String('CardMovedHistoryEntry');
    STR_ROUNDENDED = Memory.allocUtf8String('RoundEndedHistoryEntry');
    STR_GAMEENDED = Memory.allocUtf8String('GameEndedHistoryEntry');
    return true;
}

var g_image = null, g_staticBH = null, g_klassPlayed = null,
    g_klassSpawned = null, g_klassRound = null, g_klassMoved = null,
    g_klassRoundEnded = null, g_klassGameEnded = null;
var g_klassMO = [];        // 已解析成功的对局结束条目类（可能 0 个）
var g_moSent = false;      // 本局 matchover 只发一次
var g_seenEntry = {};      // 未知条目类名去重（诊断日志用）

function rp(a) { return a.readPointer(); }
function ru8(a) { return a.readU8(); }
function ri32(a) { return a.readS32(); }

// 按字段名查类字段偏移（实例/静态通用；查不到返回 -1）。
// 字段名比偏移稳定：游戏更新或发行渠道差异（GOG/Steam）移动字段时，
// 动态解析能自动适配，避免硬编码偏移静默失效。
function fieldOff(klass, name) {
    var iter = Memory.alloc(PTR);
    iter.writePointer(ptr(0));
    var fk;
    while (!(fk = f_class_fields(klass, iter)).isNull()) {
        var np = f_field_name(fk);
        if (!np.isNull() && np.readCString() === name) return f_field_off(fk);
    }
    return -1;
}

function setup() {
    if (g_staticBH) return true;
    if (!initExports()) return false;   // GameAssembly.dll 未就绪：下帧再试
    var domain = f_domain_get();
    if (domain.isNull()) return false;
    var cnt = Memory.alloc(PTR);
    var arr = f_domain_asms(domain, cnt);
    var n = cnt.readU32();
    var image = null;
    for (var i = 0; i < n; i++) {
        var img = f_asm_image(arr.add(i * PTR).readPointer());
        var np = f_image_name(img);
        if (np.isNull()) continue;
        if (np.readCString() === 'Assembly-CSharp.dll') { image = img; break; }
    }
    if (!image) return false;
    var kBH = f_class_from_name(image, STR_NS_VIS, STR_BH);
    if (kBH.isNull()) return false;
    // m_Instance 声明在泛型基类 ASingleton<BattleHandler>，取其 parent 的静态区
    var kParent = kBH.add(OFF.klass_parent).readPointer();
    if (kParent.isNull()) return false;
    var sb = f_static_data(kParent);
    if (sb.isNull()) return false;   // 类尚未初始化
    // 关键字段偏移动态解析（放在 g_staticBH 赋值前，setup 成功只执行一次）
    var offGC = fieldOff(kBH, '<GameController>k__BackingField');
    var offLU = fieldOff(kBH, '<LocalUser>k__BackingField');
    if (offGC > 0) OFF.battlehandler_gamecontroller = offGC;
    if (offLU > 0) OFF.battlehandler_localuser = offLU;
    send({ t: 'bh_off', gc: offGC, lu: offLU });
    g_staticBH = sb;
    g_klassPlayed = f_class_from_name(image, STR_NS_GP, STR_PLAYED);
    g_klassSpawned = f_class_from_name(image, STR_NS_GP, STR_SPAWNED);
    g_klassRound = f_class_from_name(image, STR_NS_GP, STR_ROUND);
    g_klassMoved = f_class_from_name(image, STR_NS_GP, STR_MOVED);
    g_klassRoundEnded = f_class_from_name(image, STR_NS_GP, STR_ROUNDENDED);
    g_klassGameEnded = f_class_from_name(image, STR_NS_GP, STR_GAMEENDED);
    g_klassMO = [];
    MO_NAMES.forEach(function (n) {
        var k = f_class_from_name(image, STR_NS_GP, Memory.allocUtf8String(n));
        if (!k.isNull()) g_klassMO.push(k);
    });
    g_image = image;
    send({ t: 'ready', platform: detectPlatform() });
    return true;
}

var lastBH = null, lastIdx = 0, lastState = -1, seq = 0;
var nullStreak = 0;   // 对局对象被置空的连续帧数（回主菜单检测，防抖用）

function replay(entries, items, size, local) {
    // 从头重放历史（含回合标记），python 端重建整局记录
    send({ t: 'reset', local: local });
    lastIdx = 0;
    for (var i = 0; i < size; i++) {
        emitEntry(items.add(OFF.array_elements + i * PTR).readPointer(), local);
    }
}

function emitEntry(entry, local) {
    if (entry.isNull()) { lastIdx++; return; }
    var k = entry.readPointer();
    if (k.equals(g_klassPlayed)) {
        var pid = ru8(entry.add(OFF.played_player_id));
        var card = entry.add(OFF.played_card).readPointer();
        if (card.isNull()) card = entry.add(OFF.played_card_original).readPointer();
        if (!card.isNull()) {
            var piid = card.add(OFF.card_instance_id).readU16();
            g_playedByIids[piid] = pid;   // 打出者归属（出牌历史权威：
                                          // 间谍落点在打出者对面半场，不能按半场推断）
            if (!g_deckIids[piid]) { lastIdx++; return; }  // 生成牌（非开局牌组真实卡牌）打出也不记录
            var tpl = card.add(OFF.card_template).readPointer();
            if (!tpl.isNull()) {
                seq++;
                send({
                    t: 'played', seq: seq, pid: pid,
                    tid: ri32(tpl.add(OFF.template_id)),
                    prov: ri32(tpl.add(OFF.template_provision)),
                    tier: ru8(tpl.add(OFF.template_tier)),
                    iid: card.add(OFF.card_instance_id).readU16(),
                    local: local
                });
            }
        }
    } else if (g_klassSpawned && k.equals(g_klassSpawned)) {
        // 生成事件分两类：
        //  Spawner 为空且落点在牌组/手牌/领袖位 = 开局真实卡牌 → 记入本局真实卡牌实例集合；
        //  其余（Spawner 非空的凭空生成、或直接落到场上/其他区域的衍生物）→ 不记录、不计人口。
        var sc = entry.add(OFF.spawned_card).readPointer();
        if (!sc.isNull()) {
            var spn = entry.add(OFF.spawned_spawner).readPointer();
            if (spn.isNull()) {
                var pLoc = ri32(entry.add(OFF.spawned_pos + 4));
                // 仅手牌/牌组算真实卡牌；领袖位(64)不算——领袖不属于卡组，
                // 计入会导致卡组明细出现领袖、领袖技能被当作打出的牌
                if (pLoc === 8 || pLoc === 16) {
                    g_deckIids[sc.add(OFF.card_instance_id).readU16()] = 1;
                }
            }
        }
    } else if (k.equals(g_klassRound)) {
        lastS1 = -1; lastS2 = -1;   // 新小局：强制重发点数（场上清空后为 0）
        send({ t: 'round', id: ri32(entry.add(OFF.round_id)) });
    } else if (k.equals(g_klassMoved)) {
        // CardMovedHistoryEntry：卡牌位置移动
        //  1) 手牌→墓地 = 撕牌/弃牌（含"抽1弃1"效果），计入已出牌；
        //  2) 牌库→手牌 = 抽牌，补记真实卡牌实例（开局后才抽上的牌
        //     不在开局实例集合里，后续打出/弃置都需要它）
        //  其它移动（上场单位换排、战场内换区等）忽略
        var fromLoc = ri32(entry.add(OFF.cardmoved_from + 4));
        var toLoc = ri32(entry.add(OFF.cardmoved_to + 4));
        if (fromLoc === OFF.loc_hand && toLoc === OFF.loc_graveyard) {
            var cardM = entry.add(OFF.cardmoved_card).readPointer();
            var iidM = cardM.isNull() ? -1 : cardM.add(OFF.card_instance_id).readU16();
            if (iidM >= 0) {
                var tplM = cardM.add(OFF.card_template).readPointer();
                if (!tplM.isNull()) {
                    // 归属：From.PlayerId（CardPosition+0）——手牌归属即玩家归属。
                    // 不再要求在开局实例集合：抽上来的新牌同样会被弃置
                    g_deckIids[iidM] = 1;
                    var pidM = ru8(entry.add(OFF.cardmoved_from));
                    seq++;
                    send({
                        t: 'discarded', seq: seq, pid: pidM,
                        tid: ri32(tplM.add(OFF.template_id)),
                        prov: ri32(tplM.add(OFF.template_provision)),
                        tier: ru8(tplM.add(OFF.template_tier)),
                        iid: iidM, local: local
                    });
                }
            }
        } else if (fromLoc === OFF.loc_deck && toLoc === OFF.loc_hand) {
            // 抽牌：牌库→手牌，真实卡牌补记入实例集合（幂等）
            var cardD = entry.add(OFF.cardmoved_card).readPointer();
            if (!cardD.isNull()) {
                g_deckIids[cardD.add(OFF.card_instance_id).readU16()] = 1;
            }
        }
    } else if (g_klassRoundEnded && k.equals(g_klassRoundEnded)) {
        // RoundEndedHistoryEntry：小局结束，携带权威比分（双方该局最终得分）。
        // 场上力量扫描只计近战/远程排、投降时清零，不可靠；这里直接读结算分。
        var ri = entry.add(OFF.roundended_roundinfo).readPointer();
        if (!ri.isNull()) {
            var scores = ri.add(OFF.roundinfo_playerscores).readPointer();
            var s1 = 0, s2 = 0;
            if (!scores.isNull() && ri32(scores.add(OFF.array_length)) >= 2) {
                s1 = ri32(scores.add(OFF.array_elements));
                s2 = ri32(scores.add(OFF.array_elements + 4));
            }
            send({
                t: 'roundend', id: ri32(ri.add(OFF.roundinfo_id)),
                s1: s1, s2: s2,
                winner: ru8(ri.add(OFF.roundinfo_winnerid)),
                local: local
            });
        }
    } else if (g_klassGameEnded && k.equals(g_klassGameEnded)) {
        // GameEndedHistoryEntry：整局结束，权威胜负 + 结束原因（投降/断线等），
        // 比分可能不完整（投降时最后一局未打完），胜负以 WinnerID 为准。
        if (!g_moSent) {
            g_moSent = true;
            send({
                t: 'matchover',
                winner: ru8(entry.add(OFF.gameended_winnerid)),
                reason: ru8(entry.add(OFF.gameended_reason)),
                local: local
            });
        }
    } else if (!k.isNull()) {
        // 对局结束条目：历史末尾出现 = 本局打完进入结算画面，
        // 立即通知 python 端存档并清空悬浮窗（不必等回主菜单）
        var isMO = false;
        for (var mi = 0; mi < g_klassMO.length; mi++) {
            if (k.equals(g_klassMO[mi])) { isMO = true; break; }
        }
        if (isMO) {
            if (!g_moSent) {
                g_moSent = true;
                send({ t: 'matchover' });
            }
        } else if (!g_seenEntry[k.toString()]) {
            // 未知条目类型：记录类名（agent_debug.log），便于后续版本识别
            g_seenEntry[k.toString()] = 1;
            try {
                var np = f_class_name(k);
                send({ t: 'unknown_entry', name: np.isNull() ? '?' : np.readCString() });
            } catch (e2) { }
        }
    }
    lastIdx++;
}

// 双方领袖人口：结构体 BattleDeck.LeaderAbility 只存模板ID，
// 需在 CardManager.AllCards 中找到对应卡牌读取其模板人口。
// 双方领袖人口可能不同，需分别读取（我方进度条才能用正确的上限）。
var g_leaderTid = {}, g_leaderProv = {};
var g_deckIids = {};   // 本局真实卡牌实例（开局进入牌组/手牌的牌），生成牌不在其中
var g_playedByIids = {};   // 卡牌实例 → 打出者（出牌历史记录，间谍归属修正用）

// 场上扫描：条件触发跟随打出的牌（如攻城大师）不会产生任何历史条目，
// 只能靠位置变化发现 —— 出现在近战/远程排且尚未计过的牌实例即视为已打出。
// 同一轮遍历顺便统计双方实时点数：只计当前小局场上两排的力量
// （小局结束牌进墓地，墓地不计入 —— 否则点数会跨小局累加）。
var lastS1 = -1, lastS2 = -1;
var lastMyDeck = "";   // 我方卡组去重（JSON 串对比，变化才重发）
var boardSent = {};    // 已上报过 board 事件的卡牌实例（每局每张牌只报一次：
                       // 此前无去重，场上每张牌每 250ms 重发，消息洪水拖垮通道）

function cardPower(c) {
    // 只读运行时当前力量（含 buff/减伤），【不回退模板基础力量】：
    // 计谋等无 CardPower 组件的牌会被 template_power 误读出 1 点
    // （实测开局对方默认多 1 点的根因——计谋在攻城排且模板字段非 0），
    // 这类牌本就不计入游戏内总分。返回 -1000 哨兵由调用方过滤。
    var cd = rp(c.add(OFF.card_data));
    if (cd.isNull()) return -1000;
    var cp = rp(cd.add(OFF.carddata_power));
    if (cp.isNull()) return -1000;
    var cur = ri32(cp.add(OFF.cardpower_current));
    return cur > -900 ? cur : -1000;
}

function detectPlatform() {
    // 发行渠道判定（v2.2.1 起按可信度排序）：
    // 1) 进程主模块路径：Steam 版一定在 steamapps/common 下，这条最硬；
    // 2) 进程实际加载的平台 DLL：Steam = steam_api64.dll，GOG = Galaxy64.dll。
    // 老逻辑先看到 Galaxy 就判 GOG：Steam 版若顺带加载了 Galaxy SDK（两个
    // 渠道共用部分构建），Steam 版会被误报成 GOG —— 用户实测踩到过。
    // 现在 steam_api 优先，路径命中 steamapps 直接判 Steam。
    var path = '', gog = false, steam = false;
    // 主模块路径要取【游戏自身的 exe】：Process.mainModule 在部分注入场景
    // 返回的是第一个加载的 DLL（实测拿到 nvwgf2umx.dll），所以按扩展名筛。
    try {
        var mm = Process.mainModule;
        if (mm && /\.exe$/i.test(mm.name)) path = (mm.path || '').toLowerCase();
    } catch (e) { }
    if (!path) {
        try {
            var m0 = Process.enumerateModules();
            for (var k = 0; k < m0.length; k++) {
                if (/\.exe$/i.test(m0[k].name)) {
                    path = (m0[k].path || '').toLowerCase();
                    break;
                }
            }
        } catch (e) { }
    }
    try {
        var mods = Process.enumerateModules();
        for (var i = 0; i < mods.length; i++) {
            var nm = mods[i].name.toLowerCase();
            if (nm.indexOf('galaxy64') !== -1) gog = true;
            else if (nm.indexOf('steam_api') !== -1) steam = true;
        }
    } catch (e) { }
    var plat = '';
    if (path.indexOf('steamapps') !== -1) plat = 'Steam';
    else if (steam) plat = 'Steam';
    else if (gog) plat = 'GOG';
    else if (path.indexOf('gog') !== -1 || path.indexOf('galaxy') !== -1) plat = 'GOG';
    send({ t: 'plat_probe', path: path, gog: gog, steam: steam, plat: plat });
    return plat;
}

function scanBoard(gc, local) {
    var cm = rp(gc.add(OFF.gamecontroller_cardmanager));
    if (cm.isNull()) return;
    var all = rp(cm.add(OFF.cardmanager_allcards));
    if (all.isNull()) return;
    var n = ri32(all.add(OFF.array_length));
    if (n <= 0 || n > 8192) return;
    var s1 = 0, s2 = 0, any = false;
    var md = {};   // 我方卡组：开局真实卡牌（位置不限：手牌/牌组/场上/墓地/领袖）
    for (var i = 0; i < n; i++) {
        var c = all.add(OFF.array_elements + i * PTR).readPointer();
        if (c.isNull()) continue;
        var loc = ri32(c.add(OFF.card_pos_location));
        var piid = c.add(OFF.card_instance_id).readU16();
        var pid = ru8(c.add(OFF.card_pos_player));
        // 我方卡组收集：开局真实卡牌（生成牌不在 g_deckIids 中），位置不限；
        // 排除对方打出的牌 —— 间谍落在我方半场，不属于我方卡组
        if (g_deckIids[piid] && pid === local) {
            var pb = g_playedByIids[piid];
            if (pb === undefined || pb === local) {
                var mt = rp(c.add(OFF.card_template));
                if (!mt.isNull()) {
                    var mtid = ri32(mt.add(OFF.template_id));
                    if (mtid > 0) md[mtid] = (md[mtid] || 0) + 1;
                }
            }
        }
        // 只统计场上两排（近战1/远程2，Homecoming 后无攻城排）。
        // 【不能加 loc 4】——ELocation 是位标志：4=EventExecuting（事件
        // 执行中的牌），先手方开局先执行事件，正在结算的牌会被抓进点数，
        // 造成"谁先手谁默认多 1 点"。生成牌同样计——只统计开局真实
        // 卡牌（g_deckIids）会漏算"打出后生成自身复制"之类的效果牌
        if (loc !== 1 && loc !== 2) continue;
        var pw = cardPower(c);
        if (pw > 0) {
            if (pid === 1) { s1 += pw; any = true; }
            else if (pid === 2) { s2 += pw; any = true; }
        }
        var tpl = rp(c.add(OFF.card_template));
        if (tpl.isNull()) continue;
        var iid = c.add(OFF.card_instance_id).readU16();
        if (!g_deckIids[iid]) continue;          // 生成的牌不记录、不计人口
        var by = g_playedByIids[iid];            // 归属：优先出牌历史（权威，
                                                 // 间谍落点在打出者对面半场）
        if (!by) {
            by = ru8(c.add(OFF.card_played_by));
            if (by === 0) by = pid;              // 兜底：打出者字段 → 所在半场
        }
        if (by !== 1 && by !== 2) continue;
        if (boardSent[iid]) continue;            // 已上报过：不再重复发送
        boardSent[iid] = 1;
        seq++;
        send({
            t: 'board', seq: seq, pid: by,
            tid: ri32(tpl.add(OFF.template_id)),
            prov: ri32(tpl.add(OFF.template_provision)),
            tier: ru8(tpl.add(OFF.template_tier)),
            iid: iid,
            local: local
        });
    }
    if (s1 !== lastS1 || s2 !== lastS2) {
        lastS1 = s1; lastS2 = s2;
        send({ t: 'points', s1: s1, s2: s2, local: local });
    }
    // 我方卡组变化时上报（新一场对局 tick 会重置 lastMyDeck 触发重发）
    var mdKeys = Object.keys(md);
    if (mdKeys.length > 0) {
        var mdKey = mdKeys.length + ":" + JSON.stringify(md);
        if (mdKey !== lastMyDeck) {
            lastMyDeck = mdKey;
            send({ t: 'mydeck', cards: md });
        }
    }
}

function leaderProvision(gc, ltid) {
    var cm = rp(gc.add(OFF.gamecontroller_cardmanager));
    if (cm.isNull()) return -1;
    var all = rp(cm.add(OFF.cardmanager_allcards));
    if (all.isNull()) return -1;
    var n = ri32(all.add(OFF.array_length));
    if (n <= 0 || n > 8192) return -1;
    for (var i = 0; i < n; i++) {
        var c = all.add(OFF.array_elements + i * PTR).readPointer();
        if (c.isNull()) continue;
        if (ri32(c.add(OFF.card_definition)) !== ltid) continue;
        var tpl = rp(c.add(OFF.card_template));
        if (tpl.isNull()) continue;
        var prov = ri32(tpl.add(OFF.template_provision));
        if (prov > 0) return prov;
    }
    return -1;
}

// il2cpp System.String → JS 字符串（len @0x10，utf16 chars @0x14）
function readStr(s) {
    if (s.isNull()) return "";
    try {
        var n = ri32(s.add(0x10));
        if (n <= 0 || n > 128) return "";
        var str = s.add(0x14).readUtf16String(n);
        return str === null ? "" : str;
    } catch (e) { return ""; }
}

// Player* → { name, level, mmr, rank, crowns, passed, ended }
function readPlayerInfo(p) {
    var o = {};
    var info = rp(p.add(OFF.player_info));
    if (!info.isNull()) {
        o.name = readStr(rp(info.add(OFF.pinfo_name)));
        o.level = ri32(info.add(OFF.pinfo_level));
        o.mmr = ri32(info.add(OFF.pinfo_mmr));
        o.rank = ri32(info.add(OFF.pinfo_rank));
    }
    o.crowns = ri32(p.add(OFF.player_crowns));
    if (OFF.player_haspassed !== undefined)
        o.passed = ru8(p.add(OFF.player_haspassed)) !== 0;
    if (OFF.player_hasendedturn !== undefined)
        o.ended = ru8(p.add(OFF.player_hasendedturn)) !== 0;
    return o;
}

var g_miKey = "";   // 上次 matchinfo 摘要（变化才上报，避免每帧刷屏）
// 对局信息：游戏模式 / 回合归属 / 双方昵称 / 等级 / 天梯分（MMR）/ 小局胜数
// EGameMode（GameInstance._gameMode）: 0=SinglePlayer(人机) 1=Client 2=Server
function checkMatchInfo(gc, local) {
    var gi = rp(gc.add(OFF.gamecontroller_gameinstance));
    if (gi.isNull()) return;
    var mode = ru8(gi.add(OFF.gameinstance_gamemode));
    var pm = rp(gc.add(OFF.gamecontroller_playermanager));
    if (pm.isNull()) return;
    var localId = ru8(pm.add(OFF.pm_localuserid));
    var curId = ru8(pm.add(OFF.pm_currentplayerid));
    var players = rp(pm.add(OFF.playermanager_players));
    if (players.isNull()) return;
    var n = ri32(players.add(OFF.array_length));
    if (n <= 0 || n > 8) return;
    var mine = null, opp = null;
    for (var i = 0; i < n; i++) {
        var p = players.add(OFF.array_elements + i * PTR).readPointer();
        if (p.isNull()) continue;
        var pid = ru8(p.add(OFF.player_id));
        if (pid === local) mine = readPlayerInfo(p);
        else if (pid === 1 || pid === 2) opp = readPlayerInfo(p);
    }
    if (!mine || !opp) return;
    var turn = (curId === localId) ? "mine" : "opp";
    var key = JSON.stringify([mode, turn, mine, opp]);
    if (key === g_miKey) return;
    g_miKey = key;
    send({ t: 'matchinfo', mode: mode, turn: turn, mine: mine, opp: opp });
}

function checkLeader(gc, local) {
    var pm = rp(gc.add(OFF.gamecontroller_playermanager));
    if (pm.isNull()) return;
    var players = rp(pm.add(OFF.playermanager_players));
    if (players.isNull()) return;
    var n = ri32(players.add(OFF.array_length));
    if (n <= 0 || n > 8) return;
    for (var i = 0; i < n; i++) {
        var p = players.add(OFF.array_elements + i * PTR).readPointer();
        if (p.isNull()) continue;
        var pid = ru8(p.add(OFF.player_id));
        if (pid !== 1 && pid !== 2) continue;
        if (g_leaderProv[pid] > 0) continue;   // 该玩家领袖人口已知，不重复扫描
        var deck = rp(p.add(OFF.player_deckstate));
        if (deck.isNull()) continue;
        var ltid = ri32(deck.add(OFF.deck_leader_ability));
        if (ltid <= 0) continue;
        var prov = leaderProvision(gc, ltid);
        if (prov <= 0) continue;               // 本帧没读到，下帧再试
        g_leaderTid[pid] = ltid;
        g_leaderProv[pid] = prov;
        send({ t: 'leader', pid: pid, tid: ltid, prov: prov, local: local });
    }
}

function tick() {
    try {
        if (!setup()) { return; }
        var bh = rp(g_staticBH);              // BattleHandler.Instance
        if (bh.isNull()) {
            if (lastState !== 0) { lastState = 0; send({ t: 'state', in_match: false }); }
            lastBH = null; lastIdx = 0; nullStreak = 0;
            return;
        }
        var gc = rp(bh.add(OFF.battlehandler_gamecontroller));
        var hist = gc.isNull() ? ptr(0) : rp(gc.add(OFF.gamecontroller_battlehistory));
        var list = hist.isNull() ? ptr(0) : rp(hist.add(OFF.battlehistory_entries));
        var items = list.isNull() ? ptr(0) : rp(list.add(OFF.list_items));
        if (gc.isNull() || hist.isNull() || list.isNull() || items.isNull()) {
            // 对局对象被置空 = 已回主菜单（游戏只清 GameController 而保留
            // BattleHandler 单例）。连续 2 帧才认定，避免加载瞬间的误判。
            if (++nullStreak >= 2 && lastState !== 0) {
                lastState = 0;
                send({ t: 'state', in_match: false });
                lastBH = null; lastIdx = 0;
            }
            return;
        }
        nullStreak = 0;
        var lu = rp(bh.add(OFF.battlehandler_localuser));
        var local = lu.isNull() ? 0 : ru8(lu.add(OFF.player_id));
        if (local !== 1 && local !== 2) {
            // localuser 尚未就绪：此时敌我判断不可靠，跳过本帧等下一次
            return;
        }
        var size = ri32(list.add(OFF.list_size));

        if (lastState !== 1) { lastState = 1; send({ t: 'state', in_match: true, local: local, count: size }); }

        // 对局对象更换或历史列表清空（新一场）→ 全量重放
        if (lastBH === null || !bh.equals(lastBH) || size < lastIdx) {
            lastBH = bh;
            g_leaderTid = {};
            g_leaderProv = {};
            g_deckIids = {};
            boardSent = {};             // 新一场：场上扫描去重集合重置
            g_playedByIids = {};        // 新一场：打出者归属表重置（随重放重建）
            g_moSent = false;           // 新一场：对局结束标记重置
            lastS1 = -1; lastS2 = -1;   // 新一场：强制重发点数
            lastMyDeck = "";            // 新一场：强制重发我方卡组
            g_miKey = "";               // 新一场：强制重发对局信息
            checkLeader(gc, local);   // 先下发领袖人口，再重放历史
            replay(null, items, size, local);
            scanBoard(gc, local);     // 中途附加：跟随打出的牌无历史条目，只能扫场上
            checkMatchInfo(gc, local);
            return;
        }
        for (var i = lastIdx; i < size; i++) {
            emitEntry(items.add(OFF.array_elements + i * PTR).readPointer(), local);
        }
        checkLeader(gc, local);
        scanBoard(gc, local);
        checkMatchInfo(gc, local);
    } catch (e) {
        send({ t: 'error', msg: String(e),
               stack: String(e.stack || '').split('\n').slice(0, 3).join(' <- ') });
    }
}
setInterval(tick, 250);
send({ t: 'hello' });
"""


# ---- 卡组库直读 agent（构筑界面 CollectionManager 链路，只读内存） ----
DECKLIB_JS = r"""
var OFF = __OFFSETS__;
var api = null, f = null;

function findExport(name) {
    try {
        var mod = Process.getModuleByName('GameAssembly.dll');
        if (mod.findExportByName) { var p0 = mod.findExportByName(name); if (p0) return p0; }
    } catch (e) {}
    try { if (typeof Module.findExportByName === 'function') {
        var p1 = Module.findExportByName('GameAssembly.dll', name); if (p1) return p1; } } catch (e) {}
    try { if (typeof Module.getGlobalExportByName === 'function') {
        var p2 = Module.getGlobalExportByName(name); if (p2) return p2; } } catch (e) {}
    return null;
}
function cstr(p) { return p.isNull() ? '' : p.readCString(); }
function rp(a) { return a.readPointer(); }
function ri32(a) { return a.readS32(); }
function ru64(a) { return a.readU64(); }

var FNS = ['il2cpp_domain_get','il2cpp_domain_get_assemblies','il2cpp_assembly_get_image',
    'il2cpp_image_get_name','il2cpp_class_from_name','il2cpp_class_get_name',
    'il2cpp_class_get_fields','il2cpp_field_get_name','il2cpp_field_get_offset',
    'il2cpp_class_get_static_field_data'];

function init() {
    if (f) return true;
    try { if (!Process.getModuleByName('GameAssembly.dll')) return false; } catch (e) { return false; }
    api = {}; f = {}; var ok = true;
    FNS.forEach(function (n) { var p = findExport(n); if (p) api[n] = p; else ok = false; });
    if (!ok) return false;
    f.domain_get = new NativeFunction(api.il2cpp_domain_get, 'pointer', []);
    f.domain_asms = new NativeFunction(api.il2cpp_domain_get_assemblies, 'pointer', ['pointer','pointer']);
    f.asm_image = new NativeFunction(api.il2cpp_assembly_get_image, 'pointer', ['pointer']);
    f.image_name = new NativeFunction(api.il2cpp_image_get_name, 'pointer', ['pointer']);
    f.class_from_name = new NativeFunction(api.il2cpp_class_from_name, 'pointer', ['pointer','pointer','pointer']);
    f.class_name = new NativeFunction(api.il2cpp_class_get_name, 'pointer', ['pointer']);
    f.class_fields = new NativeFunction(api.il2cpp_class_get_fields, 'pointer', ['pointer','pointer']);
    f.field_name = new NativeFunction(api.il2cpp_field_get_name, 'pointer', ['pointer']);
    f.field_off = new NativeFunction(api.il2cpp_field_get_offset, 'int32', ['pointer']);
    f.static_data = new NativeFunction(api.il2cpp_class_get_static_field_data, 'pointer', ['pointer']);
    return true;
}

function getImage() {
    var domain = f.domain_get();
    var cnt = Memory.alloc(8);
    var arr = f.domain_asms(domain, cnt);
    var n = cnt.readU32();
    for (var i = 0; i < n; i++) {
        var img = f.asm_image(arr.add(i * Process.pointerSize).readPointer());
        if (!img.isNull() && cstr(f.image_name(img)) === 'Assembly-CSharp.dll') return img;
    }
    return null;
}

function fieldOff(klass, name) {
    var iter = Memory.alloc(8); iter.writePointer(ptr(0));
    var fk;
    while (!(fk = f.class_fields(klass, iter)).isNull()) {
        if (cstr(f.field_name(fk)) === name) return f.field_off(fk);
    }
    return -1;
}

// ASingleton<T>.m_Instance：父类泛型单例的静态字段
function singletonOf(klass) {
    var parent = rp(klass.add(OFF.klass_parent));
    if (parent.isNull()) return null;
    var off = fieldOff(parent, 'm_Instance');
    if (off < 0) off = 0;
    var sb = f.static_data(parent);
    if (sb.isNull()) return null;
    return rp(sb.add(off));
}

// il2cpp Dictionary`2 遍历（Entry: hash +0x0 / next +0x4 / key +0x8 / value +0x10）。
// valPtr: value 为引用类型（true，读指针）或值类型 int（false）。
// 注意 key 永远按原始指针宽读：m_Decks 的 key 是 ulong（卡组ID），不做解引用校验。
function dictEntries(dict, valPtr) {
    if (!dict || dict.isNull()) return null;
    var entriesArr = rp(dict.add(OFF.dict_entries));
    if (entriesArr.isNull()) return null;
    var count = ri32(dict.add(OFF.dict_count));
    if (count <= 0 || count > 512) return null;
    var arrLen = ri32(entriesArr.add(OFF.array_length));
    if (arrLen < count || arrLen > count * 2 + 8) return null;   // 不是数组
    var base = entriesArr.add(OFF.array_elements);
    var out = [];
    for (var i = 0; i < arrLen; i++) {
        var ent = base.add(i * 24);
        var h = ri32(ent);
        if (h < 0) continue;                     // hashCode<0 = 空闲/已删除槽
        if (h === 0 && ri32(ent.add(4)) === 0) continue;    // 全零空槽
        var k, v;
        try { k = rp(ent.add(8)); v = valPtr ? rp(ent.add(16)) : ri32(ent.add(16)); }
        catch (e) { continue; }
        // 只校验指针字段：m_Decks 只有 value 是指针；m_Cards 只有 key 是指针
        var pv = valPtr ? v : k;
        try {
            if (pv.isNull()) continue;
            if (pv.toString() === '0xffffffffffffffff') continue;
            if (rp(pv).isNull()) continue;
        } catch (e) { continue; }
        out.push({ key: k, value: v });
    }
    return out;
}

// CollectionCard → 模板 {tid, prov, gold}
function cardTpl(collCard) {
    if (!collCard || collCard.isNull()) return null;
    var card = rp(collCard.add(OFF.collcard_card));
    if (card.isNull()) return null;
    var tpl = rp(card.add(OFF.card_template));
    if (tpl.isNull()) return null;
    try {
        return {
            tid: ri32(tpl.add(OFF.template_id)),
            prov: ri32(tpl.add(OFF.template_provision)),
            gold: tpl.add(OFF.template_tier).readU8() === 8
        };
    } catch (e) { return null; }
}

// EFactionId 位标志 → 库内阵营代码
var FAC_FLAG = { 1: 'neu', 2: 'mon', 4: 'nil', 8: 'nor', 16: 'sco', 32: 'syn', 64: 'ske' };

function probeDecks() {
    if (!init()) return { t: 'err', msg: 'GameAssembly 未就绪（请稍后重试）' };
    var img = getImage();
    if (!img) return { t: 'err', msg: '未找到游戏主程序集' };
    var kCM = f.class_from_name(img, Memory.allocUtf8String('GwentVisuals'),
                                Memory.allocUtf8String('CollectionManager'));
    if (kCM.isNull()) return { t: 'err', msg: 'CollectionManager 类不存在（游戏版本不兼容）' };
    // CurrentController 按字段名动态解析（新版游戏 MonoBehaviour 布局 +8），失败回退 offsets
    var offCtrl = fieldOff(kCM, '<CurrentController>k__BackingField');
    if (offCtrl > 0) OFF.cm_controller = offCtrl;
    var inst = singletonOf(kCM);
    if (!inst || inst.isNull())
        return { t: 'err', msg: '尚未检测到卡组数据：请先启动游戏并登录到主菜单' };
    var ctrl = rp(inst.add(OFF.cm_controller));
    if (ctrl.isNull()) return { t: 'err', msg: '收藏控制器未初始化，请进入游戏主菜单后重试' };
    var data = rp(ctrl.add(OFF.acd_data));
    if (data.isNull()) return { t: 'err', msg: '收藏数据未加载，请进入主菜单后重试' };
    var decksDict = rp(data.add(OFF.cd_decks));
    var ents = dictEntries(decksDict, true);
    if (ents === null) return { t: 'err', msg: '卡组列表为空或读取失败，请进入卡组构筑界面后重试' };
    var decks = [];
    for (var i = 0; i < ents.length; i++) {
        var idStr = uint64(ents[i].key.toString()).toString();
        var deck = ents[i].value;
        if (!deck || deck.isNull()) continue;
        try {
            var cards = {}, provs = {}, gold = [], ncards = 0, prov = 0;
            var cents = dictEntries(rp(deck.add(OFF.deck_cards)), false);
            if (cents) {
                for (var j = 0; j < cents.length; j++) {
                    var tpl = cardTpl(cents[j].key);
                    if (!tpl || tpl.tid <= 0) continue;
                    var tidStr = String(tpl.tid);
                    cards[tidStr] = (cards[tidStr] || 0) + cents[j].value;
                    provs[tidStr] = tpl.prov;
                    if (tpl.gold && gold.indexOf(tidStr) < 0) gold.push(tidStr);
                    ncards += cents[j].value;
                    prov += tpl.prov * cents[j].value;
                }
            }
            if (!ncards) continue;                    // 空卡组不收录
            var name = '';
            var namePtr = rp(deck.add(OFF.deck_name));
            try {
                if (!namePtr.isNull()) {
                    var slen = ri32(namePtr.add(0x10));
                    if (slen > 0 && slen < 256)
                        name = namePtr.add(OFF.string_chars).readUtf16String(slen);
                }
            } catch (e2) {}
            var leader = null, strat = null;
            try { leader = cardTpl(rp(deck.add(OFF.deck_leader))); } catch (e3) {}
            try { strat = cardTpl(rp(deck.add(OFF.deck_strat))); } catch (e4) {}
            decks.push({
                id: idStr,
                name: name,
                faction: FAC_FLAG[ri32(deck.add(OFF.deck_faction))] || '',
                ability: ri32(deck.add(OFF.deck_ability)),
                leader: leader ? String(leader.tid) : '',
                leader_prov: leader ? leader.prov : 0,
                stratagem: strat ? String(strat.tid) : '',
                cards: cards,
                provs: provs,
                gold: gold,
                prov_total: prov,
                card_count: ncards
            });
        } catch (e5) { /* 坏槽跳过 */ }
    }
    decks.sort(function (a, b) { return b.card_count - a.card_count; });
    return { t: 'decks', decks: decks };
}

// 脚本加载即执行一次直读（不依赖 RPC 调用时机）。
// 启动期未登录/引擎未就绪属预期失败，静默跳过（按需读取走 RPC probeDecks，
// 卡组库界面的按钮会再次触发）——不再向 python 端刷 error 事件
try {
    var r0 = probeDecks();
    if (r0 && r0.t === 'decks') send(r0);
} catch (e) { /* 启动期预期失败：静默 */ }
"""


def _find_game_pid():
    """按进程名查找 Gwent；可能同时存在已退出的僵尸进程，优先返回存活的。"""
    import frida
    try:
        pids = [p.pid for p in frida.get_local_device().enumerate_processes()
                if p.name.lower() in ("gwent.exe", "gwent")]
    except Exception:  # noqa: BLE001
        return None
    for pid in pids:
        if _is_alive(pid):
            return pid
    return None


def _is_alive(pid):
    """僵尸进程已无任何线程，此时注入必定失败，直接跳过。"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPTHREAD = 0x00000004

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return False
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    count = 0
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            count += 1
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return count > 0


def load_offsets():
    off = dict(DEFAULT_OFFSETS)
    try:
        with open(OFFSETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in off:
                off[k] = int(v)
    except (OSError, ValueError):
        pass
    return off


# EFactionId 位标志 → 库内阵营代码（与 DECKLIB_JS 内 FAC_FLAG 一致）
FAC_FLAG = {1: "neu", 2: "mon", 4: "nil", 8: "nor", 16: "sco", 32: "syn", 64: "ske"}


def read_deck_library(timeout=10.0):
    """一次性直读游戏内存中的用户卡组库（构筑界面 CollectionManager 数据）。

    用户在游戏里创建的卡组登录后常驻内存，无需处于构筑界面。
    只读内存，不写入任何游戏数据。
    返回 (True, [deck_dict...], '') 或 (False, [], 错误说明)。

    deck_dict: {id, name, faction, leader, leader_prov, stratagem,
                cards: {tid: n}, provs: {tid: p}, gold: [tid],
                prov_total, card_count}
    """
    import frida
    pid = _find_game_pid()
    if pid is None:
        return False, [], "未检测到正在运行的游戏，请先启动 GWENT 并登录"
    holder = {}
    done = threading.Event()

    def on_message(message, _data):
        if message.get("type") == "error":
            desc = message.get("description", "脚本错误")
            stack = (message.get("stack") or "").splitlines()
            if stack:
                desc = "%s | %s" % (desc, stack[0])
            _dbg("decklib SCRIPT ERROR: " + desc)
            holder["err"] = "内存读取脚本错误（详见 agent_debug.log）"
            done.set()
        elif message.get("type") == "send":
            m = message.get("payload") or {}
            if m.get("t") == "decks":
                holder["decks"] = m.get("decks") or []
                _dbg("decklib read OK: %d decks" % len(holder["decks"]))
            elif m.get("t") == "err":
                holder["err"] = m.get("msg") or "读取失败"
            done.set()

    try:
        session = frida.attach(pid)
    except Exception as e:  # noqa: BLE001
        return False, [], "附加游戏进程失败：%s" % e
    try:
        script = session.create_script(
            DECKLIB_JS.replace("__OFFSETS__", json.dumps(load_offsets())))
        script.on("message", on_message)
        script.load()
        done.wait(timeout)
    except Exception as e:  # noqa: BLE001
        holder["err"] = holder.get("err") or "注入失败：%s" % e
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001
            pass
    if "err" in holder and not holder.get("decks"):
        return False, [], holder["err"]
    decks = holder.get("decks")
    if decks is None:
        return False, [], "读取超时，请确认游戏已登录到主菜单后重试"
    return True, decks, ""


class MemSource(threading.Thread):
    """与 Detector 接口兼容：on_card(card_dict, meta) / on_status(dict)。

    额外支持 on_round(round_no) 与 on_reset() 回调（可选传入）。
    """

    def __init__(self, db, on_card, on_status, config,
                 on_round=None, on_reset=None, on_leader=None, on_points=None,
                 on_mydeck=None, on_matchover=None, on_matchinfo=None,
                 on_roundend=None):
        super().__init__(daemon=True)
        self.db = db
        self.on_card = on_card
        self.on_status = on_status
        self.on_round = on_round or (lambda r: None)
        self.on_reset = on_reset or (lambda: None)
        self.on_matchover = on_matchover or (lambda mo: None)
        self.on_leader = on_leader or (lambda tid, prov, mine=False: None)
        self.on_points = on_points or (lambda s1, s2, local: None)
        self.on_mydeck = on_mydeck or (lambda cards: None)
        self.on_matchinfo = on_matchinfo or (lambda mi: None)
        self.on_roundend = on_roundend or (lambda re_: None)
        self.cfg = config
        self.offsets = load_offsets()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._session = None
        self._detach_ev = None   # 脚本级致命错误时置位，触发重连
        self._seen_iids = set()   # 同一张牌实例只计一次（召唤 + 打出会重复上报）
        self._seen_gold = set()   # 金卡按 (tid, 玩家) 去重：回响金卡返还手牌后重复打出不再计

    # ------- 控制接口 -------
    def stop(self):
        self._stop.set()
        try:
            if self._session is not None:
                self._session.detach()
        except Exception:  # noqa: BLE001
            pass

    def pause(self, paused):
        if paused:
            self._pause.set()
        else:
            self._pause.clear()

    # ------- frida 消息 -------
    def _on_message(self, message, _data):
        try:
            self._dispatch_message(message)
        except Exception:  # noqa: BLE001
            # frida 消息回调线程的异常不会传到主线程也不会弹窗，必须自己记录
            import traceback
            _dbg("on_message EXC: " + traceback.format_exc())
            self._emit_status("error", "消息处理异常（详见 agent_debug.log）")

    def _dispatch_message(self, message):
        mtype = message.get("type")
        if mtype != "send":
            if mtype == "error":
                desc = message.get("description", "")
                stack = (message.get("stack") or "").splitlines()
                if stack:
                    desc = "%s | %s" % (desc, stack[0])
                _dbg("SCRIPT ERROR: " + desc)
                self._emit_status("error", desc)
                # 脚本级异常（如引擎尚未就绪导致的初始化失败）：置位分离事件，
                # 让 run() 循环重建脚本重连，避免脚本静默死亡后永远收不到数据
                if self._detach_ev is not None:
                    self._detach_ev.set()
            return
        m = message.get("payload") or {}
        t = m.get("t")
        if t in ("played", "spawned", "board", "discarded"):
            _dbg("msg %s seq=%s pid=%s tid=%s iid=%s" % (
                t, m.get("seq"), m.get("pid"), m.get("tid"), m.get("iid")))
        elif t in ("reset", "state", "round", "matchover"):
            _dbg("msg %s %s" % (t, {k: v for k, v in m.items() if k != "t"}))
        elif t == "unknown_entry":
            _dbg("history entry class: " + str(m.get("name")))
        elif t == "bh_off":
            _dbg("BH 字段偏移动态解析: gc=%s lu=%s（-1=解析失败，回退硬编码值）"
                 % (m.get("gc"), m.get("lu")))
        if t == "plat_probe":
            _dbg("平台探测: path=%s gog=%s steam=%s => %s"
                 % (m.get("path"), m.get("gog"), m.get("steam"), m.get("plat")))
        if t == "hello":
            self._emit_status("mem_attached")
        elif t == "ready":
            self._emit_status("mem_ready", platform=m.get("platform", ""))
        elif t == "state":
            self._emit_status("mem_state", "", in_match=m.get("in_match"))
        elif t == "reset":
            self._seen_iids.clear()
            self._seen_gold.clear()
            self.on_reset()
        elif t == "matchover":
            if not self._pause.is_set():
                self.on_matchover(m)
        elif t == "roundend":
            if not self._pause.is_set():
                self.on_roundend(m)
        elif t == "round":
            if not self._pause.is_set():
                base = int(self.offsets.get("round_id_base", 0))
                self.on_round(int(m.get("id", 0)) + 1 - base)
        elif t == "leader":
            # pid == local 表示是我方领袖（双方领袖人口可能不同，需分开使用）
            mine = int(m.get("pid", 0) or 0) == int(m.get("local", -1))
            self.on_leader(int(m.get("tid", 0)), int(m.get("prov", -1)), mine)
        elif t == "points":
            self.on_points(int(m.get("s1", 0)), int(m.get("s2", 0)),
                           int(m.get("local", 0) or 0))
        elif t == "mydeck":
            self.on_mydeck(m.get("cards") or {})
        elif t == "matchinfo":
            if not self._pause.is_set():
                self.on_matchinfo(m)
        elif t in ("played", "spawned", "board", "discarded"):
            if self._pause.is_set():
                return
            iid = int(m.get("iid", 0) or 0)
            if iid and iid in self._seen_iids:
                return          # 同一张牌实例已计过（召唤/打出/场上扫描重复上报）
            tier = int(m.get("tier", 0))
            if tier & 8:
                # 金卡卡组只允许多 1 张：同一方重复打出同一金卡 = 回响返还后再打出，不重复记录
                gkey = (int(m.get("tid", 0)), int(m.get("pid", 0)))
                if gkey in self._seen_gold:
                    return
                self._seen_gold.add(gkey)
            if iid:
                self._seen_iids.add(iid)
            card = self.db.by_id.get(str(m.get("tid")))
            if card is None:
                self._emit_status("mem_unknown", str(m.get("tid")))
                return
            mine = int(m.get("pid", 0)) == int(m.get("local", -1))
            live = m.get("prov", -1)
            if isinstance(live, int) and live > 0:
                # 人口以游戏运行时数据为准（平衡补丁会改人口，本地卡库可能过期）
                card = dict(card, provision=live)
            card = dict(card, gold=bool(tier & 8))
            meta = {"conf": 1.0, "text": card["name_zh"], "mem": True,
                    "mine": mine, "spawned": t == "spawned", "auto": t == "board",
                    "time": time.time()}
            self.on_card(card, meta)
        elif t == "error":
            msg = m.get("msg", "")
            if m.get("stack"):
                msg = "%s || %s" % (msg, m.get("stack"))
            # 同一条错误只上报一次：tick 周期性失败（同一地址 access
            # violation 反复读）若不去重会每 250ms 刷一条事件
            if msg and msg != getattr(self, "_last_err", None):
                self._last_err = msg
                self._emit_status("error", msg)

    def _emit_status(self, state, detail="", **kw):
        d = {"state": state, "detail": detail}
        d.update(kw)
        try:
            self.on_status(d)
        except Exception:  # noqa: BLE001
            pass

    # ------- 主循环 -------
    def run(self):
        import frida
        js = AGENT_JS.replace("__OFFSETS__", json.dumps(self.offsets))
        while not self._stop.is_set():
            session = None
            try:
                pid = _find_game_pid()
                if pid is None or not _is_alive(pid):
                    # 未启动，或残留的僵尸进程（线程已退出）
                    self._emit_status("mem_no_process")
                    time.sleep(2.5)
                    continue
                self._emit_status("mem_connecting")
                _dbg("attach pid=%s" % pid)
                session = frida.attach(pid)
                script = session.create_script(js)
                script.on("message", self._on_message)
                script.load()
                self._session = session
                detached = threading.Event()
                self._detach_ev = detached

                def on_detached(*_a):
                    _dbg("session detached")
                    detached.set()

                session.on("detached", on_detached)
                # 等待：分离 / 停止
                while not self._stop.is_set() and not detached.is_set():
                    time.sleep(0.5)
                self._session = None
                if not self._stop.is_set():
                    self._emit_status("mem_detached")
            except frida.ProcessNotFoundError:
                self._emit_status("mem_no_process")
                time.sleep(2.5)
            except frida.NotSupportedError as e:
                # 常见于进程正在退出/受保护，稍后重试
                _dbg("attach NotSupported: %s" % e)
                self._emit_status("mem_attach_fail", str(e))
                time.sleep(3.0)
            except Exception as e:  # noqa: BLE001
                import traceback
                _dbg("run EXC: %s | %s" % (
                    e, traceback.format_exc().splitlines()[-1]))
                self._emit_status("error", str(e))
                time.sleep(3.0)
            finally:
                self._session = None
                self._detach_ev = None
                if session is not None:
                    try:
                        session.detach()
                    except Exception:  # noqa: BLE001
                        pass


if __name__ == "__main__":
    # 独立测试：python mem_source.py —— 附加游戏并打印全部出牌事件
    import sys
    sys.path.insert(0, BASE_DIR)
    from card_db import build_or_load, find_game_dir

    class _DBStub:
        by_id = {}

    try:
        db, err = build_or_load(find_game_dir(),
                                os.path.join(BASE_DIR, "cards_zh.json"))
        print("卡库: %d 张" % len(db.cards))
    except Exception as e:  # noqa: BLE001
        print("卡库加载失败（不影响内存读取测试）: %s" % e)
        db = _DBStub()

    def on_card(card, meta):
        print("[出牌] tid=%s name=%s" % (card.get("id"), card.get("name_zh")))

    def on_status(st):
        print("[状态]", st)

    def on_round(r):
        print("[回合] 第 %d 小局" % r)

    src = MemSource(db, on_card, on_status, {}, on_round=on_round,
                    on_reset=lambda: print("[重置] 重放历史"))
    src.start()
    print("运行中… Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        src.stop()
