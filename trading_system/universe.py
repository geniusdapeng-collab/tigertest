"""全市场股票池（Universe）。

支持四种模式：
  core     — 仅大盘核心（约 120 只，快速模式）
  extended — 内嵌高流动性池（约 460 只，默认）
  full     — 真实全市场：NASDAQ Trader 官方上市清单（NASDAQ + NYSE/AMEX，
             约 6000+ 只普通股），下载后本地缓存 7 天，离线回退 extended
  file     — 从用户 CSV/TXT 自定义加载（一行一个代码）

扫描器会对池内每只票做硬过滤 + 量化排序，产出动态 watchlist。
v4.0 起系统不再内置任何“默认自选股”。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# 大盘核心（快速模式 / 冒烟测试用）
CORE_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "AMD", "NFLX",
    "COST", "LLY", "JPM", "V", "UNH", "XOM", "MA", "HD", "PG", "JNJ",
    "CRM", "BAC", "ORCL", "CVX", "MRK", "ABBV", "KO", "PEP", "WMT", "CSCO",
    "ACN", "MCD", "TMO", "ABT", "LIN", "ADBE", "DHR", "TXN", "PM", "NEE",
    "QCOM", "INTU", "AMGN", "IBM", "GS", "MS", "CAT", "GE", "BA", "HON",
    "PLTR", "UBER", "SHOP", "XYZ", "SNOW", "DDOG", "CRWD", "PANW", "ANET", "SMCI",
    "MU", "LRCX", "AMAT", "KLAC", "MRVL", "ON", "ARM", "TSM", "ASML", "DELL",
    "COIN", "MARA", "RIOT", "HOOD", "SOFI", "AFRM", "RBLX", "U", "TTD", "APP",
    "ENPH", "SEDG", "FSLR", "RUN", "MELI", "ARRY", "NXT", "CEG", "VST", "GEV",
    "OKLO", "SMR", "CCJ", "UUUU", "DNN", "LEU", "NNE", "BWXT", "PWR", "EME",
    "IONQ", "RGTI", "QBTS", "QUBT", "SOUN", "BBAI", "AI", "PATH", "UPST", "LMND",
    "RKLB", "LUNR", "ASTS", "SPCE", "JOBY", "ACHR", "EH", "ONDS", "RCAT", "KTOS",
]

# 扩展池（在核心之上追加，合计 ~460 只高流动性标的）
EXTENDED_UNIVERSE = CORE_UNIVERSE + [
    # 科技/软件
    "ADP", "ADI", "APH", "CDNS", "CDW", "CTSH", "DXCM", "EPAM", "FICO", "FTNT",
    "GDDY", "GEN", "GLW", "GRMN", "HPE", "HPQ", "INTC", "JKHY", "KEYS", "LOGI",
    "MCHP", "MPWR", "MSI", "NTAP", "NXPI", "PAYC", "PTC", "ROP", "SNPS",
    "STX", "SWKS", "TDY", "TER", "TRMB", "TYL", "UI", "VRSN", "WDC", "WDAY",
    "ZBRA", "ZM", "DOCU", "OKTA", "NET", "ESTC", "MDB", "GTLB", "CFLT", "BILL",
    "HUBS", "VEEV", "NOW", "TEAM", "MNDY", "ASAN", "SMAR", "PCTY", "PAYX", "BR",
    # 通信/消费互联网
    "ABNB", "BKNG", "CMCSA", "CHTR", "DASH", "EA", "EXPE", "LYV", "MTCH", "PINS",
    "ROKU", "SPOT", "T", "TMUS", "TRIP", "VZ", "WBD", "YELP", "ZG", "Z",
    # 金融
    "AIG", "ALL", "AON", "AXP", "BLK", "BX", "CB", "CME", "COF", "DFS",
    "FIS", "FI", "ICE", "KKR", "MCO", "MET", "NDAQ", "PNC", "PYPL", "SCHW",
    "SPGI", "SYF", "TFC", "TRV", "USB", "WFC", "WTW", "ZION", "C", "KEY", "MTB",
    # 医疗
    "AMGN", "BIIB", "BMY", "CI", "CVS", "DXCM", "ELV", "EW", "GILD", "HCA",
    "HUM", "IDXX", "ILMN", "INCY", "ISRG", "MCK", "MDT", "MOH", "MRNA", "PFE",
    "REGN", "RMD", "SYK", "VRTX", "ZBH", "BSX", "BDX", "BAX", "ALGN", "DXCM",
    # 工业/材料
    "AOS", "DOV", "EMR", "ETN", "FAST", "FDX", "GD", "HWM", "IR", "ITT",
    "LMT", "MMM", "NOC", "OTIS", "PCAR", "PH", "RTX", "SWK", "TT", "UPS",
    "URI", "VMC", "WAB", "XYL", "AA", "ALB", "APD", "CF", "CLF", "FCX",
    "MLM", "NEM", "NUE", "PPG", "SHW", "STLD", "VST", "CE", "DD", "DOW",
    # 能源
    "APA", "BKR", "COP", "CTRA", "DVN", "EOG", "EQT", "FANG", "HAL", "HES",
    "MPC", "MRO", "OKE", "OXY", "PSX", "SLB", "VLO", "WMB", "XOM", "CVX",
    # 消费
    "AEO", "ANF", "BBY", "BURL", "CCL", "CMG", "DECK", "DG", "DLTR", "DPZ",
    "DRI", "EBAY", "EL", "F", "GM", "GPS", "HAS", "HLT", "KMX", "LVS",
    "MAR", "MGM", "NCLH", "NKE", "ORLY", "RCL", "ROST", "SBUX", "TJX", "TGT",
    "ULTA", "VFC", "WYNN", "YUM", "AZO", "BBWI", "CPB", "GIS", "HSY", "K",
    "KHC", "KR", "MDLZ", "MNST", "SJM", "STZ", "TAP", "TSN", "WBA", "CL",
    "CHD", "CLX", "COTY", "EL", "ENR", "KMB", "KVUE", "MKC", "NWL", "PG",
    # 公用事业/REIT
    "AEP", "AWK", "CMS", "CNP", "D", "DTE", "DUK", "ED", "EIX", "ES",
    "ETR", "EXC", "FE", "NEE", "NI", "NRG", "PEG", "PNW", "PPL", "SO",
    "SRE", "WEC", "XEL", "AMT", "ARE", "AVB", "BXP", "CPT", "DLR", "EQIX",
    "EQR", "ESS", "EXR", "INVH", "IRM", "MAA", "O", "PLD", "PSA", "REG",
    "SBAC", "SPG", "UDR", "VICI", "VTR", "WELL", "WY",
    # 新能源/电动车/航空
    "RIVN", "LCID", "NIO", "XPEV", "LI", "FSR", "CHPT", "EVGO", "BLNK", "PLUG",
    "FCEL", "BE", "BLDP", "SPWR", "MAXN", "JKS", "CSIQ", "DQ", "DAL", "UAL",
    "AAL", "LUV", "SAVE", "ALK", "JBLU", "HA", "SKYW",
    # 稀土/材料主题
    "MP", "LAC", "ALTM", "SQM", "LTHM", "PLL", "SGML", "SLI", "TMC", "UUUU",
    # 太空/无人机/机器人
    "AVAV", "TXT", "LHX", "LDOS", "SAIC", "CACI", "MRCY", "VSAT", "IRDM", "GSAT",
    # 量子/AI 二线
    "IONQ", "RGTI", "QBTS", "QUBT", "ARQQ", "SOUN", "BBAI", "VERI", "GFAI", "AISP",
    # 加密/金融科技
    "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF", "CORZ", "IREN", "WULF",
    "HOOD", "SOFI", "AFRM", "UPST", "LMND", "NU", "MQ", "FOUR", "TOST", "PAGS",
]

# Symbols observed as absent from the current NASDAQ Trader common-stock
# directory during the 2026-11-11 completed-session audit. They are removed
# from embedded scan modes but remain visible here as an auditable snapshot;
# explicit file/full-list modes are never silently filtered.
RETIRED_UNIVERSE = frozenset({
    "ALTM", "AVB", "BITF", "CFLT", "CTRA", "DFS", "EA", "EQR", "FI", "FSR",
    "GPS", "HA", "HES", "K", "KVUE", "LTHM", "MAXN", "MRO", "PLL", "ROKU",
    "SAVE", "SMAR", "SQ", "WBA", "WBD",
})
CORE_UNIVERSE = [ticker for ticker in CORE_UNIVERSE if ticker not in RETIRED_UNIVERSE]
EXTENDED_UNIVERSE = [ticker for ticker in EXTENDED_UNIVERSE if ticker not in RETIRED_UNIVERSE]

# 去重保序
_seen: set[str] = set()
EXTENDED_UNIVERSE = [t for t in EXTENDED_UNIVERSE if not (t in _seen or _seen.add(t))]

# The production research scan keeps a focused, high-liquidity universe.
# Chain tickers are still fetched separately for breadth, sector and risk
# context, but they do not all become daily research candidates.
FOCUSED_UNIVERSE = list(dict.fromkeys(CORE_UNIVERSE))


# ============================================================
# 真实全市场清单（NASDAQ Trader 官方 symdir，每日更新，免费无需 key）
# ============================================================
_NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
_OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
_FULL_CACHE = Path(config.CACHE_DIR) / "universe_full.json"
_FULL_TTL_S = 7 * 86400

# 证券名称中排除非普通股的关键词（权证/单位/优先股/债券类）
# 注意：纯子串匹配会误杀——"unit" 曾把 UnitedHealth/UPS/United Airlines 等
# United 系公司全部静默剔除出 full 池（v5.4 修复：单位/权证类用词边界正则）。
import re as _re
_BAD_NAME_KW = (
    "warrant", " right", "preferred", "preference", "depositary",
    "notes", "note ", "debenture", "baby bond", "%", "etn", "etf",
)
_BAD_NAME_RE = _re.compile(r"\bunits?\b")   # "Unit(s)" 独立单词才排除


def _bad_name(name: str) -> bool:
    return any(kw in name for kw in _BAD_NAME_KW) or bool(_BAD_NAME_RE.search(name))


def _clean_symbol(sym: str) -> str | None:
    """清洗代码：只保留字母数字，类别股分隔符统一为 Yahoo 风格的 '-'。"""
    s = sym.strip().upper().replace("/", "-").replace(".", "-").replace("^", "-")
    if not s or any(not ch.isalnum() and ch != "-" for ch in s):
        return None
    if len(s) > 6 or s.endswith("W") and "-" in s:   # 过滤超长与权证尾缀
        return None
    return s


def _parse_symdir(text: str, sym_col: int, test_col: int, etf_col: int,
                  name_col: int, include_etfs: bool) -> list[str]:
    out: list[str] = []
    for ln in text.splitlines()[1:]:
        if ln.startswith("File Creation Time") or "|" not in ln:
            break
        parts = ln.split("|")
        if len(parts) <= max(sym_col, test_col, etf_col, name_col):
            continue
        if parts[test_col].strip() != "N":           # 排除测试证券
            continue
        if not include_etfs and parts[etf_col].strip() == "Y":
            continue
        name = parts[name_col].lower()
        if _bad_name(name):
            continue
        s = _clean_symbol(parts[sym_col])
        if s:
            out.append(s)
    return out


def fetch_full_universe(include_etfs: bool = False, timeout: int = 25) -> list[str]:
    """下载 NASDAQ Trader 官方清单，返回过滤后的全市场普通股代码。

    nasdaqlisted.txt 列: Symbol(0)|Security Name(1)|Market Category(2)|Test Issue(3)|
                         Financial Status(4)|Round Lot Size(5)|ETF(6)|NextShares(7)
    otherlisted.txt  列: ACT Symbol(0)|Security Name(1)|Exchange(2)|CQS Symbol(3)|
                         ETF(4)|Round Lot Size(5)|Test Issue(6)|NASDAQ Symbol(7)
    v5.4 修复：nasdaqlisted 的 etf_col 曾误写 7（NextShares 列），导致
    include_etfs=False 实际不过滤 ETF——QQQ 等名称不含 "etf" 字样的 ETF
    混入全市场股票池被当个股扫描。正确列索引为 6。
    """
    tickers: list[str] = []
    for url, cols in (
        (_NASDAQ_LISTED, (0, 3, 6, 1)),
        (_OTHER_LISTED, (0, 6, 4, 1)),
    ):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        tickers += _parse_symdir(text, *cols, include_etfs)
    return sorted(set(tickers))


def load_full_universe(include_etfs: bool = False) -> tuple[list[str], str]:
    """加载全市场清单：在线下载 → 本地缓存（7天） → 回退 extended。

    返回 (tickers, source)，source ∈ {nasdaqtrader, cache, fallback}。
    """
    try:
        tickers = fetch_full_universe(include_etfs)
        if len(tickers) >= 2000:
            _FULL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _FULL_CACHE.write_text(json.dumps(
                {"ts": time.time(), "tickers": tickers}))
            logger.info("全市场清单下载成功: %d 只（已缓存）", len(tickers))
            return tickers, "nasdaqtrader"
    except Exception as exc:
        logger.warning("全市场清单下载失败: %s", exc)
    if _FULL_CACHE.exists():
        try:
            blob = json.loads(_FULL_CACHE.read_text())
            age = time.time() - blob.get("ts", 0)
            if age < _FULL_TTL_S and len(blob.get("tickers", [])) >= 2000:
                logger.info("使用缓存全市场清单: %d 只（缓存 %.1f 天）",
                            len(blob["tickers"]), age / 86400)
                return blob["tickers"], "cache"
        except Exception:
            pass
    logger.warning("全市场清单不可用，回退内嵌 extended 池（%d 只）", len(EXTENDED_UNIVERSE))
    return list(dict.fromkeys(EXTENDED_UNIVERSE)), "fallback"


def load_universe(mode: str = "extended", file_path: str | None = None) -> list[str]:
    if mode == "focused":
        return list(FOCUSED_UNIVERSE)
    if mode == "core":
        return list(dict.fromkeys(CORE_UNIVERSE))
    if mode == "file":
        if not file_path:
            raise ValueError("mode=file 需要提供 file_path")
        p = Path(file_path)
        tickers = [ln.strip().upper() for ln in p.read_text().splitlines()
                   if ln.strip() and not ln.startswith("#")]
        return list(dict.fromkeys(tickers))
    if mode == "full":
        tickers, _src = load_full_universe()
        return tickers
    return list(dict.fromkeys(EXTENDED_UNIVERSE))


# ============================================================
# v6.3 S4 多市场股票池（CN 沪深 A 股 / HK 港股主板）
# ============================================================
# 内嵌演示/兜底池（demo 与 full 拉取失败时使用；yahoo 风格代码后缀，
# provider 侧符号映射由各自 provider 负责）
CN_UNIVERSE = [
    # 沪市主板
    "600519.SS", "601318.SS", "600036.SS", "601166.SS", "600030.SS", "600276.SS",
    "601888.SS", "600585.SS", "601012.SS", "600900.SS", "601398.SS", "600028.SS",
    "601857.SS", "600050.SS", "601988.SS", "600104.SS", "601668.SS", "600048.SS",
    "601288.SS", "600009.SS", "601899.SS", "600309.SS", "601088.SS", "600690.SS",
    "601766.SS", "600031.SS", "601985.SS", "600406.SS", "601100.SS", "600887.SS",
    "601633.SS", "600745.SS", "601877.SS", "600196.SS", "601689.SS", "600346.SS",
    "601225.SS", "600660.SS", "601138.SS", "600438.SS", "601919.SS", "600570.SS",
    "601601.SS", "600741.SS", "601336.SS", "600989.SS", "601658.SS", "600111.SS",
    "601066.SS", "600837.SS", "601211.SS",
    # 深市主板/创业板/科创板代表
    "000001.SZ", "000858.SZ", "000333.SZ", "000651.SZ", "000725.SZ", "000063.SZ",
    "000100.SZ", "000157.SZ", "000166.SZ", "000338.SZ", "000423.SZ", "000538.SZ",
    "000568.SZ", "000596.SZ", "000625.SZ", "000661.SZ", "000776.SZ", "000792.SZ",
    "000895.SZ", "000963.SZ", "002001.SZ", "002007.SZ", "002027.SZ", "002032.SZ",
    "002049.SZ", "002050.SZ", "002120.SZ", "002142.SZ", "002179.SZ", "002230.SZ",
    "002236.SZ", "002241.SZ", "002252.SZ", "002271.SZ", "002304.SZ", "002311.SZ",
    "002352.SZ", "002371.SZ", "002410.SZ", "002415.SZ", "002460.SZ", "002466.SZ",
    "002475.SZ", "002493.SZ", "002555.SZ", "002594.SZ", "002601.SZ", "002648.SZ",
    "002714.SZ", "002812.SZ", "002841.SZ", "002916.SZ", "002920.SZ", "300014.SZ",
    "300015.SZ", "300033.SZ", "300059.SZ", "300122.SZ", "300124.SZ", "300142.SZ",
    "300274.SZ", "300308.SZ", "300316.SZ", "300347.SZ", "300408.SZ", "300413.SZ",
    "300433.SZ", "300450.SZ", "300498.SZ", "300529.SZ", "300558.SZ", "300595.SZ",
    "300601.SZ", "300628.SZ", "300661.SZ", "300750.SZ", "300760.SZ", "300782.SZ",
    "300896.SZ", "300919.SZ", "300957.SZ", "300999.SZ", "688981.SS", "688012.SS",
    "688008.SS", "688036.SS", "688111.SS", "688169.SS", "688187.SS", "688256.SS",
    "688271.SS", "688303.SS", "688363.SS", "688396.SS", "688599.SS", "688777.SS",
    "688041.SS", "688126.SS", "688223.SS", "688561.SS", "688766.SS", "688819.SS",
]
HK_UNIVERSE = [
    "0700.HK", "0005.HK", "0941.HK", "1299.HK", "0388.HK", "0939.HK", "1398.HK",
    "3988.HK", "2318.HK", "9988.HK", "3690.HK", "9618.HK", "1810.HK", "9888.HK",
    "9999.HK", "0883.HK", "0857.HK", "0386.HK", "0001.HK", "0016.HK", "0011.HK",
    "0012.HK", "0017.HK", "0002.HK", "0003.HK", "0006.HK", "0027.HK", "0066.HK",
    "0101.HK", "0151.HK", "0175.HK", "0267.HK", "0288.HK", "0291.HK", "0322.HK",
    "0669.HK", "0688.HK", "0762.HK", "0823.HK", "0868.HK", "0881.HK", "0960.HK",
    "0968.HK", "0981.HK", "1038.HK", "1044.HK", "1088.HK", "1093.HK", "1109.HK",
    "1113.HK", "1177.HK", "1211.HK", "1928.HK", "1997.HK", "2015.HK", "2020.HK",
    "2269.HK", "2313.HK", "2319.HK", "2331.HK", "2382.HK", "2388.HK", "2628.HK",
    "2688.HK", "2899.HK", "3692.HK", "6618.HK", "6690.HK", "6862.HK", "9633.HK",
    "9961.HK", "1929.HK", "1099.HK", "0144.HK", "0135.HK", "0083.HK", "0241.HK",
    "0293.HK", "0836.HK",
]

_CN_CACHE = Path(config.CACHE_DIR) / "universe_cn.json"
_HK_CACHE = Path(config.CACHE_DIR) / "universe_hk.json"

# 东财 clist 免费接口（无需 key）：沪深 A 股 / 港股主板清单
_EM_CLIST = ("https://push2.eastmoney.com/api/qt/clist/get"
             "?pn=1&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields=f12")
_EM_FS_CN = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"   # 深A(主板+创业板)+沪A(主板+科创板)
_EM_FS_HK = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"  # 港股主板


def _fetch_em_clist(fs: str, code2ticker, min_n: int, timeout: int = 25) -> list[str]:
    """东财 clist 全量清单拉取（分页 pz=5000 单页覆盖）。"""
    url = _EM_CLIST.format(fs=fs, pz=6000)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = json.loads(resp.read().decode("utf-8", errors="replace"))
    diff = ((blob.get("data") or {}).get("diff")) or []
    tickers = [code2ticker(str(d.get("f12", ""))) for d in diff]
    out = sorted({t for t in tickers if t})
    if len(out) < min_n:
        raise RuntimeError(f"东财清单数量异常（{len(out)} < {min_n}）")
    return out


def fetch_cn_universe(timeout: int = 25) -> list[str]:
    """沪深 A 股全量清单（东财免费源，约 5000+ 只；yahoo 风格后缀）。"""
    def c2t(code: str) -> str | None:
        if not code:
            return None
        if code.startswith(("60", "68", "11", "5")):
            return f"{code}.SS"
        if code.startswith(("00", "30", "12", "15")):
            return f"{code}.SZ"
        return None
    return _fetch_em_clist(_EM_FS_CN, c2t, 4000, timeout)


def fetch_hk_universe(timeout: int = 25) -> list[str]:
    """港股主板全量清单（东财免费源；代码补零至 4 位 + .HK）。"""
    def c2t(code: str) -> str | None:
        if not code or not code.isdigit():
            return None
        return f"{int(code):04d}.HK"
    return _fetch_em_clist(_EM_FS_HK, c2t, 1500, timeout)


def _load_market_full(market_id: str, fetch, cache: Path,
                      fallback: list[str], min_n: int) -> tuple[list[str], str]:
    """全量清单：在线 → 缓存（7天） → 内嵌兜底（复刻 US 两级模式）。"""
    try:
        tickers = fetch()
        if len(tickers) >= min_n:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": time.time(), "tickers": tickers}))
            logger.info("%s 全量清单下载成功: %d 只（已缓存）", market_id, len(tickers))
            return tickers, "eastmoney"
    except Exception as exc:
        logger.warning("%s 全量清单下载失败: %s", market_id, exc)
    if cache.exists():
        try:
            blob = json.loads(cache.read_text())
            age = time.time() - blob.get("ts", 0)
            if age < _FULL_TTL_S and len(blob.get("tickers", [])) >= min_n:
                logger.info("使用缓存 %s 清单: %d 只（缓存 %.1f 天）",
                            market_id, len(blob["tickers"]), age / 86400)
                return blob["tickers"], "cache"
        except Exception:
            pass
    logger.warning("%s 清单不可用，回退内嵌池（%d 只）", market_id, len(fallback))
    return list(dict.fromkeys(fallback)), "fallback"


def load_market_universe(market_id: str, mode: str = "extended",
                         file_path: str | None = None) -> tuple[list[str], str]:
    """CN/HK 股票池加载。返回 (tickers, source)。

    full → 东财全量清单（两级拉取模式复刻 US：清单缓存 + 内嵌兜底）；
    其他模式 → 内嵌演示池（demo/冒烟默认）。
    """
    mid = (market_id or "").lower()
    if mid not in ("cn", "hk"):
        raise ValueError(f"load_market_universe 仅支持 cn/hk: {market_id}")
    if file_path:
        return load_universe("file", file_path), "file"
    embedded = CN_UNIVERSE if mid == "cn" else HK_UNIVERSE
    if mode == "full":
        if mid == "cn":
            return _load_market_full(mid, fetch_cn_universe, _CN_CACHE,
                                     embedded, 4000)
        return _load_market_full(mid, fetch_hk_universe, _HK_CACHE,
                                 embedded, 1500)
    return list(dict.fromkeys(embedded)), "embedded"
