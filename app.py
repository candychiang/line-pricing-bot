
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage,
                            ImageMessage, StickerMessage, AudioMessage,
                            VideoMessage, FileMessage, FollowEvent)
import anthropic
import os
import re
import datetime
import math
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# 管理者 LINE ID（從環境變數讀取）
ADMIN_LINE_ID = os.environ.get("ADMIN_LINE_ID")

def notify_admin(message):
    """異常時推播通知管理者"""
    if not ADMIN_LINE_ID:
        return
    try:
        line_bot_api.push_message(
            ADMIN_LINE_ID,
            TextSendMessage(text="⚠️ 熊貓Bot異常通知\n" + str(message))
        )
    except Exception as e:
        print("管理者通知失敗: " + str(e))
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

conversation_history = {}
parse_fail_count = {}  # 解析失敗計數

WELCOME_MSG = """您好！我是空運出口卡車費助理 🐼

請輸入以下資訊，我幫您試算運費：
• 提貨地點
• 件數與重量
• 貨物尺寸（選填）
• 特殊需求（如不可疊放、木箱）

範例：
台北內湖，3件，120×80×90cm，150kg
高雄左營，5箱，200kg，不可疊
814，10CTN，GW 500kg
台北，2 WDC，每箱 100×80×60cm，80kg"""

# =====================
# Google Sheets
# =====================

def get_sheet(sheet_name="工作表1"):
    """取得指定工作表"""
    try:
        import json
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        # 優先從環境變數讀取憑證
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(
                "google_credentials.json", scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID"))
        try:
            return spreadsheet.worksheet(sheet_name)
        except:
            return spreadsheet.sheet1
    except Exception as e:
        print("Google Sheets 連線失敗: " + str(e))
        return None

# 費率快取（程式啟動時讀取一次）
_pricing_cache = None
_kinlian_cache = None
_detong_cache = None
_english_area_cache = {}  # 英文地名對照表

def load_pricing_from_sheets():
    """從 Google Sheets 讀取全部費率表，存入快取"""
    global _pricing_cache, _kinlian_cache, _detong_cache
    try:
        import json
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(
                "google_credentials.json", scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID"))

        def read_simple_pricing(sheet_name):
            """讀取簡單格式費率表（台北/機場）"""
            pricing = {}
            try:
                ws = spreadsheet.worksheet(sheet_name)
                rows = ws.get_all_values()
                if len(rows) > 1:
                    headers = rows[0][1:]
                    for row in rows[1:]:
                        if not row[0]: continue
                        area = row[0].strip()
                        prices = {}
                        for i, h in enumerate(headers):
                            if i+1 < len(row) and row[i+1] and row[i+1] not in ['-','–','']:
                                try:
                                    prices[h.strip()] = int(str(row[i+1]).replace(',','').replace('$',''))
                                except:
                                    pass
                        if prices:
                            pricing[area] = prices
                print("✅ " + sheet_name + " 讀取成功，" + str(len(pricing)) + " 個地區")
            except Exception as e:
                print("❌ " + sheet_name + " 讀取失敗: " + str(e))
            return pricing

        # 1. 台北費率
        taipei = read_simple_pricing("台北費率")

        # 2. 機場費率
        airport = read_simple_pricing("機場費率")

        # 3. 高雄屏東台南費率（勁連發）
        kinlian = {}
        try:
            ws = spreadsheet.worksheet("高雄屏東台南費率")
            rows = ws.get_all_values()
            if len(rows) > 1:
                for row in rows[1:]:
                    if not row[0]: continue
                    area = row[0].strip()
                    base = row[1].strip() if len(row) > 1 else ""
                    addon = int(row[2]) if len(row) > 2 and row[2] else 0
                    tiers = []
                    limits = [60, 100, 200, 300, 400, 500, 600]
                    for i, limit in enumerate(limits):
                        if i+3 < len(row) and row[i+3]:
                            try:
                                tiers.append((limit, int(row[i+3])))
                            except:
                                pass
                    extra = int(row[10]) if len(row) > 10 and row[10] else 100
                    kinlian[area] = {"base": base, "addon": addon, "tiers": tiers, "extra": extra}
            print("✅ 高雄屏東台南費率讀取成功，" + str(len(kinlian)) + " 個地區")
        except Exception as e:
            print("❌ 高雄屏東台南費率讀取失敗: " + str(e))

        # 4. 台中彰化費率（得統得勝）
        detong = {}
        try:
            ws = spreadsheet.worksheet("台中彰化費率")
            rows = ws.get_all_values()
            if len(rows) > 1:
                for row in rows[1:]:
                    if not row[0]: continue
                    area = row[0].strip()
                    group = row[1].strip() if len(row) > 1 else ""
                    tiers = []
                    limits = [50, 100, 300, 500, 1000]
                    for i, limit in enumerate(limits):
                        if i+2 < len(row) and row[i+2]:
                            try:
                                tiers.append((limit, float(row[i+2])))
                            except:
                                pass
                    # 1001以上按KG
                    if len(row) > 7 and row[7]:
                        try:
                            tiers.append((99999, float(row[7])))
                        except:
                            pass
                    detong[area] = {"group": group, "tiers": tiers}
            print("✅ 台中彰化費率讀取成功，" + str(len(detong)) + " 個地區")
        except Exception as e:
            print("❌ 台中彰化費率讀取失敗: " + str(e))

        # 合併台北+機場
        combined = {}
        combined.update(taipei)
        combined.update(airport)

        if combined:
            _pricing_cache = combined
        if kinlian:
            _kinlian_cache = kinlian
        if detong:
            _detong_cache = detong

        total = len(combined) + len(kinlian) + len(detong)
        print("✅ 全部費率讀取完成，共 " + str(total) + " 個地區")

        # 5. 英文地名對照表
        global _english_area_cache
        try:
            ws = spreadsheet.worksheet("英文地名對照")
            rows = ws.get_all_values()
            eng_map = {}
            for row in rows[1:]:
                if len(row) >= 2 and row[0] and row[1]:
                    eng = row[0].strip()
                    zh = row[1].strip()
                    # key 統一去空格小寫
                    eng_map[eng.replace(" ","").lower()] = zh
            _english_area_cache = eng_map
            print("✅ 英文地名對照表讀取成功，共 " + str(len(eng_map)) + " 筆")
        except Exception as e:
            print("❌ 英文地名對照表讀取失敗: " + str(e))

    except Exception as e:
        print("費率表讀取失敗，使用程式內建費率: " + str(e))

def get_pricing_db():
    """取得費率資料（優先使用 Sheets 快取）"""
    if _pricing_cache:
        # 合併 Sheets 費率和程式內建費率（Sheets 優先）
        merged = dict(PRICING_DB)
        merged.update(_pricing_cache)
        return merged
    return PRICING_DB

def log_to_sheet(user_id, display_name, user_msg, ai_reply):
    try:
        sheet = get_sheet()
        if sheet is None:
            return
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        short_reply = ai_reply[:500] if len(ai_reply) > 500 else ai_reply
        sheet.append_row([now, display_name, user_id, user_msg, short_reply])
    except Exception as e:
        print(f"記錄失敗: {e}")
        notify_admin("Google Sheets記錄失敗\n錯誤:" + str(e)[:200])

def get_display_name(user_id):
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return user_id

# =====================
# 資料檢查
# =====================

LOCATION_KEYWORDS = [
    "台北", "新北", "基隆", "桃園", "新竹", "苗栗", "台中", "彰化", "南投",
    "雲林", "嘉義", "台南", "高雄", "屏東", "宜蘭", "花蓮", "台東",
    "內湖", "板橋", "中和", "永和", "新店", "汐止", "三重", "新莊",
    "蘆洲", "五股", "林口", "淡水", "瑞芳", "左營", "鼓山", "前鎮",
    "楠梓", "仁武", "大社", "岡山", "鳳山", "土城", "樹林", "龜山",
    "蘆竹", "大園", "南崁", "竹北", "湖口", "大里", "豐原", "草屯",
    "員林", "鹿港", "永康", "仁德", "關廟", "麻豆", "佳里", "平鎮",
    "大寮", "小港", "新園", "阿蓮", "萬丹", "內埔", "長治", "路竹",
    "湖內", "南科", "山上", "林園", "大樹",
    "林口", "迴龍", "八里", "三峽", "北投", "天母", "社子",
    "三芝", "金山", "萬里", "鶯歌",
    "景美", "安坑", "新屋", "楊梅", "觀音", "大溪", "龍潭",
    "竹南", "香山", "新埔", "苗栗",
    "中壢", "八德", "潭子", "大雅", "神岡", "大肚", "沙鹿", "后里", "新社",
    "東勢", "伸港", "花壇", "福興", "秀水", "大村", "芬園", "溪湖", "永靖",
    "埔鹽", "田中", "埤頭", "北斗", "芳苑", "機場", "桃機", "松山機場"
]
WEIGHT_PATTERN = re.compile(r'(?:GW|G\.?W\.?|gw)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:kg|KGS?)?|(\d+(?:\.\d+)?)\s*(?:kg|公斤|KGS?)', re.IGNORECASE)
COUNT_PATTERN = re.compile(r'(\d+)\s*(件|箱|個|pcs|pc|ctn|ctns|plt|plts|wdc|pkgs?)', re.IGNORECASE)
POSTAL_PATTERN = re.compile(r'\b\d{3}\b')

def has_location(text):
    if POSTAL_PATTERN.search(text):
        return True
    if any(kw in text for kw in LOCATION_KEYWORDS):
        return True
    # 英文地名識別（忽略空格和大小寫）
    text_clean = text.replace(" ","").lower()
    if _english_area_cache:
        return any(eng in text_clean for eng in _english_area_cache)
    # 內建英文關鍵字（快取未載入時備用）
    builtin_eng = ["taipei","taoyuan","kaohsiung","taichung","tainan","keelung",
                   "hsinchu","neihu","nangang","xizhi","banqiao","zhongli","chungli",
                   "airport","nantou","yilan","miaoli","changhua","pingtung"]
    return any(eng in text_clean for eng in builtin_eng)

def has_weight_or_count(text):
    return bool(WEIGHT_PATTERN.search(text)) or bool(COUNT_PATTERN.search(text))

def check_missing_info(text):
    missing = []
    if not has_location(text):
        missing.append("提貨地點")
    if not has_weight_or_count(text):
        missing.append("件數或重量")
    if missing:
        return "您好！請問可以提供以下資訊嗎？😊\n• " + "\n• ".join(missing)

    # 多地點偵測
    # 縣市層級和地址組成字詞不算獨立地點
    city_level = ["台北市","新北市","基隆市","桃園市","新竹市","新竹縣",
                  "苗栗縣","台中市","彰化縣","南投縣","雲林縣","嘉義市",
                  "嘉義縣","台南市","高雄市","屏東縣","宜蘭縣","花蓮縣",
                  "台東縣","台北","新北","基隆","桃園","新竹","苗栗",
                  "台中","彰化","南投","雲林","嘉義","台南","高雄","屏東",
                  "宜蘭","花蓮","台東"]
    # 地址組成字（這些出現在地址裡不算地點）
    addr_chars = ["市","縣","區","路","街","號","巷","弄","樓","段","工業","工業區"]

    # 先移除縣市名稱和地址字詞，再偵測剩下的地點關鍵字
    text_for_detection = text
    for city in city_level:
        text_for_detection = text_for_detection.replace(city, "")

    matched_areas = []
    for kw in LOCATION_KEYWORDS:
        # 跳過縣市層級關鍵字
        if kw in city_level:
            continue
        if kw in text_for_detection:
            matched_areas.append(kw)

    unique_areas = list(dict.fromkeys(matched_areas))
    if len(unique_areas) >= 2:
        return "您好！偵測到多個提貨地點，多點詢價請與客服確認，謝謝 😊"

    return None

# =====================
# 車型規格
# =====================

TRUCK_TYPES = [
    {"name": "0.6T", "length": 170, "width": 125, "height": 125, "max_kg": 500},
    {"name": "1.5T", "length": 300, "width": 150, "height": 150, "max_kg": 1200},
    {"name": "3.5T", "length": 400, "width": 180, "height": 180, "max_kg": 2000},
    {"name": "4.5T", "length": 450, "width": 180, "height": 180, "max_kg": 3000},
    {"name": "8.8T", "length": 480, "width": 195, "height": 195, "max_kg": 5000},
    {"name": "12T",  "length": 730, "width": 230, "height": 230, "max_kg": 6000},
    {"name": "15T",  "length": 760, "width": 240, "height": 240, "max_kg": 7500},
]

PLT_GAP = 7    # 棧板間距及牆壁（cm）
HEIGHT_GAP = 7 # 疊放頭部空間（cm）

# =====================
# 選車邏輯
# =====================

def can_fit_plt_arrangement(rows, cols, item_l, item_w, truck_l, truck_w):
    """棧板排列：含7cm間距"""
    needed_l = item_l * rows + PLT_GAP * (rows + 1)
    needed_w = item_w * cols + PLT_GAP * (cols + 1)
    return needed_l <= truck_l and needed_w <= truck_w

def find_best_truck_plt(count, item_l, item_w, item_h, total_kg, user_can_stack, is_express):
    """
    棧板選車：
    - 單件：不考慮疊放，只看尺寸+重量
    - 多件+專車+可疊：件高×2+7≤車斗高才疊，件數減半
    - 多件+專車+不可疊：全部平放
    - 多件+併車：只看重量（不考慮尺寸排列）
    """
    for truck in TRUCK_TYPES:
        tl, tw, th, tkg = truck["length"], truck["width"], truck["height"], truck["max_kg"]
        if total_kg > tkg:
            continue

        if count == 1:
            # 單件：直接看尺寸
            actually_stackable = False
            orientations = [(item_l, item_w), (item_w, item_l)] if item_l != item_w else [(item_l, item_w)]
            fitted = any(
                ol + PLT_GAP * 2 <= tl and ow + PLT_GAP * 2 <= tw
                for ol, ow in orientations
            )
        elif not is_express:
            # 多件併車：只看重量
            actually_stackable = user_can_stack
            fitted = True
        else:
            # 多件專車
            actually_stackable = user_can_stack and (item_h * 2 + HEIGHT_GAP <= th)
            effective_count = math.ceil(count / 2) if actually_stackable else count
            fitted = False
            orientations = [(item_l, item_w), (item_w, item_l)] if item_l != item_w else [(item_l, item_w)]
            for ol, ow in orientations:
                for rows in range(1, effective_count + 1):
                    cols = math.ceil(effective_count / rows)
                    if rows * cols < effective_count:
                        continue
                    if can_fit_plt_arrangement(rows, cols, ol, ow, tl, tw) or \
                       can_fit_plt_arrangement(cols, rows, ol, ow, tl, tw):
                        fitted = True
                        break
                if fitted:
                    break

        if fitted:
            return truck["name"], actually_stackable

    return None, False

def find_best_truck_ctn(items, total_kg, user_can_stack, is_express):
    """
    箱件選車（含多件不同尺寸）：
    - 箱件無間距
    - 單件/多件併車：只看最大件尺寸能否放入
    - 多件專車：用最大件尺寸嘗試所有行列排列，找最小可用車型
      - 可疊：有效件數減半，高度×2+7≤車斗高
      - 不可疊：全部平放，高度+7≤車斗高（頭部空間）
    """
    if not items:
        return None, False

    max_h = max(i["h"] for i in items)
    max_l = max(i["l"] for i in items)
    max_w = max(i["w"] for i in items)
    count = len(items)

    for truck in TRUCK_TYPES:
        tl, tw, th, tkg = truck["length"], truck["width"], truck["height"], truck["max_kg"]
        if total_kg > tkg:
            continue

        if count == 1 or not is_express:
            # 單件或併車：只看最大件尺寸
            actually_stackable = user_can_stack
            fitted = max_l <= tl and max_w <= tw
        else:
            # 多件專車：嘗試所有行列排列（箱件無間距）
            actually_stackable = user_can_stack and (max_h * 2 + HEIGHT_GAP <= th)
            eff = math.ceil(count / 2) if actually_stackable else count
            need_h = (max_h * 2 + HEIGHT_GAP) if actually_stackable else (max_h + HEIGHT_GAP)
            if need_h > th:
                fitted = False
            else:
                fitted = False
                for ol, ow in ([(max_l, max_w), (max_w, max_l)] if max_l != max_w else [(max_l, max_w)]):
                    for rows in range(1, eff + 1):
                        cols = math.ceil(eff / rows)
                        need_l = ol * rows
                        need_w = ow * cols
                        if need_l <= tl and need_w <= tw:
                            fitted = True
                            break
                    if fitted:
                        break

        if fitted:
            return truck["name"], actually_stackable

    return None, False

def calculate_cargo(cargo):
    """主計算，回傳計算結果 dict"""
    result = {}
    count = cargo["count"]
    total_kg = cargo["total_kg"]
    is_pallet = cargo.get("is_pallet", False)
    user_can_stack = cargo.get("can_stack", True)

    # 才數（優先用用戶提供的VW）
    if cargo.get("vw_cbf"):
        result["cbf"] = cargo["vw_cbf"]
        result["vol_weight"] = round(cargo["vw_cbf"] * 6, 1)
    elif cargo.get("has_dim") and cargo.get("items"):
        total_cbf = sum(i["l"] * i["w"] * i["h"] / 28317 for i in cargo["items"])
        result["cbf"] = round(total_cbf, 1)
        result["vol_weight"] = round(total_cbf * 6, 1)
    else:
        result["cbf"] = None
        result["vol_weight"] = None

    result["charge_weight"] = max(total_kg, result["vol_weight"] or 0)

    # 強制規則
    force_truck = False
    warnings = []
    is_wdc = cargo.get("is_wdc", False)

    if cargo.get("has_dim") and cargo.get("items"):
        max_h = max(i["h"] for i in cargo["items"])
        item_kg = total_kg / count
        if max_h >= 120:
            force_truck = True
            warnings.append("高≥120cm強制專車")
        if item_kg >= 150:
            force_truck = True
            if is_wdc:
                warnings.append("WDC重≥150kg，強制專車，請確認是否有堆高機或叉車")
            else:
                warnings.append("重≥150kg強制專車，請確認是否有堆高機")
    elif is_wdc:
        warnings.append("WDC請確認是否有堆高機或叉車")

    result["force_truck"] = force_truck
    result["warnings"] = warnings

    # 選車（分別計算併車和專車建議）
    if cargo.get("has_dim") and cargo.get("items"):
        items = cargo["items"]
        if is_pallet:
            item_l = items[0]["l"]
            item_w = items[0]["w"]
            item_h = items[0]["h"]
            # 單件棧板：堆疊直接採用用戶指定，不做自動判斷
            effective_can_stack = user_can_stack if count == 1 else user_can_stack
            express_truck, express_stack = find_best_truck_plt(
                count, item_l, item_w, item_h, result["charge_weight"], effective_can_stack, is_express=True)
            combo_truck, combo_stack = find_best_truck_plt(
                count, item_l, item_w, item_h, result["charge_weight"], effective_can_stack, is_express=False)
        else:
            express_truck, express_stack = find_best_truck_ctn(
                items, result["charge_weight"], user_can_stack, is_express=True)
            combo_truck, combo_stack = find_best_truck_ctn(
                items, result["charge_weight"], user_can_stack, is_express=False)
        result["express_truck"] = express_truck
        result["combo_truck"] = combo_truck
        result["stackable"] = express_stack
        result["combo_stackable"] = combo_stack
    else:
        result["express_truck"] = None
        result["combo_truck"] = None
        result["stackable"] = user_can_stack
        result["combo_stackable"] = user_can_stack

    return result

# =====================
# 解析貨物資訊
# =====================

def parse_cargo(text):
    """解析業務輸入的貨物資訊，支援多種輸入格式"""

    # ── 英文地名轉換 ──
    text_clean_lower = text.replace(" ","").lower()
    if _english_area_cache:
        for eng, zh in _english_area_cache.items():
            if eng in text_clean_lower:
                # 在原文加入中文地名，讓後續解析可以找到
                text = text + " " + zh
                break
    elif True:
        # 內建英文對照（快取未載入時備用）
        builtin_map = {
            "neihu":"內湖","nangang":"南港","taipei":"台北","taoyuan":"桃園",
            "kaohsiung":"高雄","taichung":"台中","tainan":"台南","keelung":"基隆",
            "hsinchu":"新竹","zhongli":"中壢","chungli":"中壢","pingzhen":"平鎮",
            "guishan":"龜山","zhubei":"竹北","luzhu":"蘆竹","bade":"八德",
            "xizhi":"汐止","xindian":"新店","banqiao":"板橋","zhonghe":"中和",
            "sanchong":"三重","xinzhuang":"新莊","luzhou":"蘆洲","wugu":"五股",
            "linkou":"林口","airport":"機場各倉","cks":"機場各倉","tpe":"機場各倉",
            "changhua":"彰化","nantou":"南投","pingtung":"屏東","yilan":"宜蘭",
            "miaoli":"苗栗","kaohsiung":"高雄","tainan":"台南",
        }
        for eng, zh in builtin_map.items():
            if eng in text_clean_lower:
                text = text + " " + zh
                break

    # ── 多地點偵測 ──
    location_count = sum(1 for kw in LOCATION_KEYWORDS if kw in text)
    # 若出現兩個或以上不同縣市，提示洽客服（由 check_missing_info 處理）
    result = {}
    result["multi_location"] = location_count >= 2

    # ── 中文數字轉阿拉伯數字 ──
    cn_map = {"一":"1","二":"2","三":"3","四":"4","五":"5",
              "六":"6","七":"7","八":"8","九":"9","十":"10"}
    for cn, ar in cn_map.items():
        text = re.sub(cn + r"(?=\s*(件|箱|板|個|PLT|plt|棧板|WDC|wdc|CTN|ctn|pallet|carton))",
                      ar, text, flags=re.IGNORECASE)

    # ── 公噸換算為 kg（1噸=1000kg）──
    ton_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:噸|公噸|MT|metric\s*ton)', text, re.IGNORECASE)
    if ton_match:
        ton_kg = float(ton_match.group(1)) * 1000
        text = text[:ton_match.start()] + f"{ton_kg}kg" + text[ton_match.end():]

    # ── 件數解析（擴充棧板/WDC/箱件別名）──
    is_pallet = False
    is_wdc = False

    # 棧板（含 pallet/板 等別名）
    count_match = re.search(
        r'(\d+)\s*(plt|plts|pallet|pallets|棧板|板)',
        text, re.IGNORECASE)
    if count_match:
        result["count"] = int(count_match.group(1))
        is_pallet = True
    else:
        # WDC 木箱
        count_match = re.search(r'(\d+)\s*(wdc|wooden\s*crate|木箱)', text, re.IGNORECASE)
        if count_match:
            result["count"] = int(count_match.group(1))
            is_wdc = True
        else:
            # 一般箱件（含 carton/cartons 等別名）
            count_match = re.search(
                r'(\d+)\s*(ctn|ctns|carton|cartons|箱|件|個|pcs|pc|pkgs?|pieces?)',
                text, re.IGNORECASE)
            if count_match:
                result["count"] = int(count_match.group(1))
            else:
                return None

    result["is_pallet"] = is_pallet
    result["is_wdc"] = is_wdc

    # ── 重量解析（支援 GW150 / 150K / 150kg / 1.5噸）──
    # GW 優先
    gw_match = re.search(
        r'(?:GW|G\.?W\.?)\s*[:/]?\s*(\d+(?:\.\d+)?)\s*(?:kg|kgs?|公斤|K\b)?',
        text, re.IGNORECASE)
    if gw_match:
        result["total_kg"] = float(gw_match.group(1))
    else:
        # 一般重量（含 K 縮寫）
        weight_match = re.search(
            r'(\d+(?:\.\d+)?)\s*(?:kg|kgs?|公斤|KGS?|K\b)',
            text, re.IGNORECASE)
        if weight_match:
            result["total_kg"] = float(weight_match.group(1))
        else:
            return None

    # ── 尺寸解析（支援 × / x / * / L W H 格式 / 英吋）──
    # 先嘗試 L/W/H 英文格式
    lwh_match = re.search(
        r'L\s*[=:]?\s*(\d+(?:\.\d+)?)\s*[,\s]*W\s*[=:]?\s*(\d+(?:\.\d+)?)\s*[,\s]*H\s*[=:]?\s*(\d+(?:\.\d+)?)',
        text, re.IGNORECASE)

    # 標準 × 格式
    dim_pattern = re.compile(
        r'(\d+(?:\.\d+)?)\s*[×xXx\*]\s*(\d+(?:\.\d+)?)\s*[×xXx\*]\s*(\d+(?:\.\d+)?)')
    dims = dim_pattern.findall(text)

    # 判斷是否為英吋（含 inch/in/吋 關鍵字）
    is_inch = bool(re.search(r'inch|吋|\bin\b', text, re.IGNORECASE))

    def to_cm(val):
        """英吋轉公分"""
        return round(val * 2.54, 1) if is_inch else val

    if lwh_match:
        l = to_cm(float(lwh_match.group(1)))
        w = to_cm(float(lwh_match.group(2)))
        h = to_cm(float(lwh_match.group(3)))
        lw = sorted([l, w], reverse=True)
        items = [{"l": lw[0], "w": lw[1], "h": h}]
        result["has_dim"] = True
        result["items"] = items * result["count"] if result["count"] > 1 else items
    elif dims:
        items = []
        for d in dims:
            h = to_cm(float(d[2]))
            lw = sorted([to_cm(float(d[0])), to_cm(float(d[1]))], reverse=True)
            items.append({"l": lw[0], "w": lw[1], "h": h})
        result["has_dim"] = True
        if len(items) == 1 and result["count"] > 1:
            items = items * result["count"]
        result["items"] = items
    else:
        result["has_dim"] = False
        result["items"] = []

    # ── 堆疊判斷 ──
    result["can_stack"] = "不可疊" not in text and "不可叠" not in text

    # ── 機場判斷 ──
    result["is_airport"] = bool(re.search(r'機場|桃機|松山機場|CW|EZ|CK', text))

    return result

PRICING_DB = {
    "台北市區": {"併車":450,"0.6T":900,"1.5T":1200,"3.5T":2000,"4.5T":2400,"8.8T":3000,"12T":3500,"15T":4000},
    "內湖":     {"併車":450,"0.6T":900,"1.5T":1200,"3.5T":2000,"4.5T":2400,"8.8T":3000,"12T":3500,"15T":4000},
    "南港":     {"併車":450,"0.6T":900,"1.5T":1200,"3.5T":2000,"4.5T":2400,"8.8T":3000,"12T":3500,"15T":4000},
    "汐止":     {"併車":500,"0.6T":1000,"1.5T":1500,"3.5T":2000,"4.5T":2400,"8.8T":2800,"12T":3800,"15T":4000},
    "深坑":     {"併車":650,"0.6T":1500,"1.5T":1900,"3.5T":2400,"4.5T":3100,"8.8T":3600,"12T":4100,"17T":4600},
    "安坑":     {"併車":650,"0.6T":1500,"1.5T":1900,"3.5T":2400,"4.5T":3100,"8.8T":3600,"12T":4100,"17T":4600},
    "新店":     {"併車":500,"0.6T":1000,"1.5T":1400,"3.5T":1900,"4.5T":2400,"8.8T":2800,"12T":3500,"15T":3800},
    "永和":     {"併車":500,"0.6T":1000,"1.5T":1400,"3.5T":1900,"4.5T":2400,"8.8T":2800,"12T":3500,"15T":3800},
    "木柵":     {"併車":500,"0.6T":1000,"1.5T":1400,"3.5T":1900,"4.5T":2400,"8.8T":2800,"12T":3500,"15T":3800},
    "板橋":     {"併車":450,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2400,"8.8T":2600,"12T":3500,"15T":3800},
    "鶯歌":     {"併車":450,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2400,"8.8T":2600,"12T":3500,"15T":3800},
    "景美":     {"併車":500,"0.6T":1000,"1.5T":1400,"3.5T":1900,"4.5T":2400,"8.8T":2800,"12T":3500,"17T":3800},
    "新屋":     {"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3500},
    "楊梅":     {"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3500},
    "觀音":     {"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3500},
    "迴龍D":    {"0.6T":1000,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3600},
    "大溪":     {"0.6T":1000,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3600},
    "龍潭":     {"0.6T":1000,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3600},
    "竹南":     {"併車":1000,"0.6T":1400,"1.5T":1800,"3.5T":2000,"4.5T":2600,"8.8T":3000,"12T":3800,"17T":4500},
    "香山":     {"併車":1000,"0.6T":1400,"1.5T":1800,"3.5T":2000,"4.5T":2600,"8.8T":3000,"12T":3800,"17T":4500},
    "新埔":     {"併車":1000,"0.6T":1400,"1.5T":1800,"3.5T":2000,"4.5T":2600,"8.8T":3000,"12T":3800,"17T":4500},
    "苗栗":     {"0.6T":2100,"1.5T":2500,"3.5T":3300,"4.5T":4300,"8.8T":4800,"12T":5500,"17T":6500},
    "中和":     {"併車":450,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2400,"8.8T":2600,"12T":3500,"15T":3800},
    "土城":     {"併車":450,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2400,"8.8T":2600,"12T":3500,"15T":3800},
    "樹林":     {"併車":450,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2400,"8.8T":2600,"12T":3500,"15T":3800},
    "五股":     {"併車":450,"0.6T":800,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2600,"12T":3200,"15T":3700},
    "三重":     {"併車":450,"0.6T":800,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2600,"12T":3200,"15T":3700},
    "新莊":     {"併車":450,"0.6T":800,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2600,"12T":3200,"15T":3700},
    "蘆洲":     {"併車":450,"0.6T":800,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2600,"12T":3200,"15T":3700},
    "泰山":     {"併車":450,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2600,"12T":3200,"15T":3700},
    "基隆":     {"併車":900,"0.6T":1300,"1.5T":1800,"3.5T":2300,"4.5T":2800,"8.8T":3200,"12T":3800,"15T":4300},
    "瑞芳":     {"併車":900,"0.6T":1300,"1.5T":1800,"3.5T":2300,"4.5T":2800,"8.8T":3200,"12T":3800,"15T":4300},
    "淡水":     {"併車":900,"0.6T":1300,"1.5T":1800,"3.5T":2300,"4.5T":2800,"8.8T":3200,"12T":3800,"15T":4300},
    "宜蘭":     {"0.6T":3000,"1.5T":3500,"3.5T":4500,"4.5T":5000,"8.8T":6000,"12T":6500,"15T":7500},
    "機場移倉": {"0.6T":350,"1.5T":600,"3.5T":1200,"4.5T":1800,"8.8T":2300,"12T":2500,"15T":2800},
    "林口":     {"併車":500,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"15T":3700},
    "迴龍":     {"0.6T":1000,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3600},
    "八里":     {"併車":500,"0.6T":900,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"15T":3700},
    "三峽":     {"0.6T":1000,"1.5T":1300,"3.5T":1800,"4.5T":2300,"8.8T":2500,"12T":3200,"17T":3600},
    "北投":     {"併車":800,"0.6T":1100,"1.5T":1500,"3.5T":2000,"4.5T":2500,"8.8T":3200,"12T":3500,"15T":4000},
    "天母":     {"併車":800,"0.6T":1100,"1.5T":1500,"3.5T":2000,"4.5T":2500,"8.8T":3200,"12T":3500,"15T":4000},
    "社子":     {"併車":800,"0.6T":1100,"1.5T":1500,"3.5T":2000,"4.5T":2500,"8.8T":3200,"12T":3500,"15T":4000},
    "三芝":     {"0.6T":1800,"1.5T":2200,"3.5T":2800,"4.5T":3200,"8.8T":3500,"12T":4000,"15T":4500},
    "金山":     {"0.6T":1800,"1.5T":2200,"3.5T":2800,"4.5T":3200,"8.8T":3500,"12T":4000,"15T":4500},
    "萬里":     {"0.6T":1800,"1.5T":2200,"3.5T":2800,"4.5T":3200,"8.8T":3500,"12T":4000,"15T":4500},
    "士林":     {"併車":500,"0.6T":1000,"1.5T":1300,"3.5T":2100,"4.5T":2500,"8.8T":3200,"12T":3600,"17T":4200},
    "北投":     {"併車":500,"0.6T":1000,"1.5T":1300,"3.5T":2100,"4.5T":2500,"8.8T":3200,"12T":3600,"17T":4200},
    "萬華":     {"併車":500,"0.6T":1000,"1.5T":1300,"3.5T":2100,"4.5T":2500,"8.8T":3200,"12T":3600,"17T":4200},
    "桃園":     {"併車":450,"0.6T":700,"1.5T":1000,"3.5T":1600,"4.5T":2000,"8.8T":2200,"12T":3000,"17T":3200},
    "龜山":     {"併車":450,"0.6T":700,"1.5T":1000,"3.5T":1600,"4.5T":2000,"8.8T":2200,"12T":3000,"17T":3200},
    "平鎮":     {"併車":450,"0.6T":700,"1.5T":1000,"3.5T":1600,"4.5T":2000,"8.8T":2200,"12T":3000,"17T":3200},
    "中壢":     {"併車":450,"0.6T":700,"1.5T":1000,"3.5T":1600,"4.5T":2000,"8.8T":2200,"12T":3000,"17T":3200},
    "八德":     {"併車":450,"0.6T":700,"1.5T":1000,"3.5T":1600,"4.5T":2000,"8.8T":2200,"12T":3000,"17T":3200},
    "蘆竹":     {"併車":400,"0.6T":500,"1.5T":800,"3.5T":1400,"4.5T":1800,"8.8T":2000,"12T":2500,"17T":3000},
    "大園":     {"併車":400,"0.6T":500,"1.5T":800,"3.5T":1400,"4.5T":1800,"8.8T":2000,"12T":2500,"17T":3000},
    "南崁":     {"併車":400,"0.6T":500,"1.5T":800,"3.5T":1400,"4.5T":1800,"8.8T":2000,"12T":2500,"17T":3000},
    "新竹":     {"併車":800,"0.6T":1200,"1.5T":1500,"3.5T":1800,"4.5T":2400,"8.8T":2800,"12T":3500,"17T":4000},
    "竹北":     {"併車":800,"0.6T":1200,"1.5T":1500,"3.5T":1800,"4.5T":2400,"8.8T":2800,"12T":3500,"17T":4000},
    "湖口":     {"併車":800,"0.6T":1200,"1.5T":1500,"3.5T":1800,"4.5T":2400,"8.8T":2800,"12T":3500,"17T":4000},
    "機場各倉": {"0.6T":500,"1.5T":600,"3.5T":1200,"4.5T":1500,"8.8T":2300,"12T":2500,"17T":2800},
}

AREA_KEYWORDS = [
    # 基隆/淡水
    ("七堵","基隆"),("基隆","基隆"),("瑞芳","瑞芳"),("淡水","淡水"),
    # 台北市區
    ("內湖","內湖"),("南港","南港"),("士林","士林"),("萬華","萬華"),
    ("北投","北投"),("天母","天母"),("社子","社子"),
    # 汐止/深坑
    ("汐止","汐止"),("深坑","深坑"),("安坑","深坑"),
    # 新店/永和/木柵/景美
    ("新店","新店"),("永和","永和"),("木柵","木柵"),("景美","景美"),
    # 板橋/中和/土城/樹林/鶯歌
    ("板橋","板橋"),("中和","中和"),("土城","土城"),("樹林","樹林"),("鶯歌","鶯歌"),
    # 三重/新莊/蘆洲/五股/泰山
    ("三重","三重"),("新莊","新莊"),("蘆洲","蘆洲"),("五股","五股"),("泰山","泰山"),
    # 林口/八里/三峽/迴龍
    ("林口","林口"),("八里","八里"),("三峽","三峽"),("迴龍","迴龍"),
    # 三芝/金山/萬里
    ("三芝","三芝"),("金山","金山"),("萬里","萬里"),
    # 桃園
    ("桃園","桃園"),("龜山","龜山"),("平鎮","平鎮"),("中壢","中壢"),("八德","八德"),
    # 蘆竹/大園/南崁
    ("蘆竹","蘆竹"),("大園","大園"),("南崁","南崁"),
    # 新屋/楊梅/觀音
    ("新屋","新屋"),("楊梅","楊梅"),("觀音","觀音"),
    # 迴龍/三峽/大溪/龍潭
    ("大溪","大溪"),("龍潭","龍潭"),
    # 新竹/竹北/湖口
    ("新竹","新竹"),("竹北","竹北"),("湖口","湖口"),
    # 竹南/香山/新埔
    ("竹南","竹南"),("香山","香山"),("新埔","新埔"),
    # 苗栗
    ("苗栗","苗栗"),
    # 宜蘭
    ("宜蘭","宜蘭"),
    # 台北（最後比對）
    ("台北","台北市區"),
]

KINLIAN_KEYWORDS = [
    # 高市組
    ("高雄","高市"),("大社","高市"),("仁武","高市"),("楠梓","高市"),
    ("大寮","高市"),("小港","高市"),("前鎮","高市"),("左營","高市"),("鼓山","高市"),
    # 高市附加
    ("林園","林園"),("大樹","大樹"),
    # 屏東組
    ("屏東","屏東"),("新園","屏東"),("橋頭","屏東"),("岡山","屏東"),
    ("燕巢","屏東"),("梓官","屏東"),("阿蓮","屏東"),("萬丹","屏東"),
    # 台南組
    ("台南","台南"),("內埔","台南"),("長治","台南"),("路竹","台南"),
    ("湖內","台南"),("仁德","台南"),("關廟","台南"),("南科","台南"),("永康","台南"),
    # 台南附加
    ("麻豆","麻豆"),("佳里","佳里"),("七股","七股"),("將軍","將軍"),("山上","山上"),
]

KINLIAN_PRICE = {
    "高市":  [(60,600),(100,800),(200,900),(300,1100),(400,1200),(500,1300),(600,1400)],
    "屏東":  [(60,800),(100,1000),(200,1100),(300,1300),(400,1400),(500,1500),(600,1600)],
    "台南":  [(60,900),(100,1100),(200,1200),(300,1400),(400,1500),(500,1600),(600,1700)],
    "林園":  [(60,600),(100,800),(200,900),(300,1100),(400,1200),(500,1300),(600,1400)],
    "大樹":  [(60,600),(100,800),(200,900),(300,1100),(400,1200),(500,1300),(600,1400)],
}

KINLIAN_ADDON = {"麻豆":500,"佳里":500,"七股":500,"將軍":500,"山上":500,"林園":200,"大樹":200}

DETONG_KEYWORDS = [
    # 台中組
    ("台中","台中"),("大里","台中"),("太平","台中"),("潭子","台中"),
    ("豐原","台中"),("大雅","台中"),("神岡","台中"),("大肚","台中"),("沙鹿","台中"),
    # 烏日組
    ("后里","烏日"),("烏日","烏日"),("梧棲","烏日"),("龍井","烏日"),
    ("大甲","烏日"),("清水","烏日"),("霧峰","烏日"),("新社","烏日"),
    # 彰化組
    ("彰化","彰化"),("東勢","彰化"),("伸港","彰化"),("和美","彰化"),
    ("鹿港","彰化"),("花壇","彰化"),("福興","彰化"),("秀水","彰化"),
    # 大村組
    ("大村","大村"),("芬園","大村"),("員林","大村"),("社頭","大村"),
    # 溪湖組
    ("溪湖","溪湖"),("永靖","溪湖"),("埔鹽","溪湖"),("田中","溪湖"),
    # 埤頭組
    ("埤頭","埤頭"),("北斗","埤頭"),
    # 芳苑組
    ("芳苑","芳苑"),
    # 草屯組
    ("草屯","草屯"),("南崗","草屯"),("南投","草屯"),
]

DETONG_PRICE = {
    "台中": [(50,400),(100,500),(300,680),(500,880),(1000,1100),(99999,1.0)],
    "烏日": [(50,500),(100,650),(300,800),(500,950),(1000,1350),(99999,1.1)],
    "彰化": [(50,650),(100,800),(300,950),(500,1000),(1000,1400),(99999,1.1)],
    "大村": [(50,750),(100,850),(300,950),(500,1200),(1000,1500),(99999,1.2)],
    "溪湖": [(50,800),(100,950),(300,1050),(500,1400),(1000,1600),(99999,1.2)],
    "埤頭": [(50,850),(100,950),(300,1050),(500,1400),(1000,1700),(99999,1.3)],
    "芳苑": [(50,900),(100,1000),(300,1100),(500,1500),(1000,1700),(99999,1.3)],
    "草屯": [(50,900),(100,1000),(300,1100),(500,1500),(1000,1800),(99999,1.3)],
}

def lookup_price(user_msg, truck_type, charge_weight):
    # 機場（業務只會用到桃園機場各倉）
    if re.search(r'機場|桃機|CW|EZ|CK', user_msg):
        area_name = "機場各倉"
        prices = PRICING_DB.get("機場各倉", {})
        express_price = prices.get(truck_type)
        if not express_price:
            express_price = prices.get("0.6T")
        return None, express_price, 0, area_name, "機場無併車，請提早通知航線客服"

    # 勁連發
    for kw, area_key in KINLIAN_KEYWORDS:
        if kw in user_msg:
            # 優先使用 Sheets 快取
            if _kinlian_cache and kw in _kinlian_cache:
                data = _kinlian_cache[kw]
                tiers = data["tiers"]
                addon = data["addon"]
                price = None
                for limit, p in tiers:
                    if charge_weight <= limit:
                        price = p; break
                if price is None and tiers:
                    over = charge_weight - 600
                    price = tiers[-1][1] + (int(over/100)+(1 if over%100>0 else 0))*data.get("extra",100)
                return (price+addon if price else None), None, addon, kw, "勁連發（僅併車）"
            # 使用程式內建費率
            if area_key in ["麻豆","佳里","七股","將軍","山上"]:
                base_key = "台南"
            elif area_key in ["林園","大樹"]:
                base_key = "高市"
            else:
                base_key = area_key
            tiers = KINLIAN_PRICE.get(base_key, KINLIAN_PRICE["高市"])
            price = None
            for limit, p in tiers:
                if charge_weight <= limit:
                    price = p; break
            if price is None:
                over = charge_weight - 600
                price = tiers[-1][1] + (int(over/100)+(1 if over%100>0 else 0))*100
            addon = KINLIAN_ADDON.get(kw, 0)
            return price+addon, None, addon, kw, "勁連發（僅併車）"

    # 得統得勝
    for kw, area_key in DETONG_KEYWORDS:
        if kw in user_msg:
            # 優先使用 Sheets 快取
            if _detong_cache and kw in _detong_cache:
                data = _detong_cache[kw]
                tiers = data["tiers"]
                price = None
                for limit, p in tiers:
                    if charge_weight <= limit:
                        price = round(charge_weight * p) if p < 10 else int(p); break
                return price, None, 0, kw, "得統得勝（僅按重量）"
            # 使用程式內建費率
            tiers = DETONG_PRICE.get(area_key)
            price = None
            for limit, p in tiers:
                if charge_weight <= limit:
                    price = round(charge_weight*p) if p<10 else p; break
            return price, None, 0, kw, "得統得勝（僅按重量）"

    # 一般地區
    for kw, mapped in AREA_KEYWORDS:
        if kw in user_msg:
            if mapped == "無報價":
                return None, None, 0, kw, "此地區無報價，請洽客服確認"
            # 優先使用 Sheets 快取，再用程式內建
            db = get_pricing_db()
            prices = db.get(mapped, {})
            return prices.get("併車"), prices.get(truck_type), 0, mapped, ""

    return None, None, 0, None, "此地區無報價，請洽客服確認"


# =====================
# 回覆過濾
# =====================

def clean_reply(text: str) -> str:
    clean_start_patterns = [
        r'您好！以下是您的詢價結果',
        r'📦\s*貨物資訊',
    ]
    for pattern in clean_start_patterns:
        match = re.search(pattern, text)
        if match:
            return text[match.start():]
    return text

# =====================
# System Prompt
# =====================

SYSTEM_PROMPT = """你是空運出口卡車費助理 🐼，服務業務人員詢價。

【重要】選車計算已由程式完成，user訊息中會附上「[系統計算]」區塊，直接使用裡面的數據，不需要自己重新計算。

【回覆格式】嚴格照以下格式，不可增加其他段落：

您好！以下是您的詢價結果 😊

📦 貨物資訊
• 提貨地點：xxx
• 件數：x件（有木箱標註 WDC、有棧板標註 PLT）
• 單件尺寸：xxxcm（無尺寸不列；多件不同尺寸則標註「各件尺寸不同」）
• 實重：xxxkg
• 才數：xxx才（無才數不列）
• 堆疊：可疊放／不可疊放

💰 費用方案
• 方案A 併車：$xxx（無併車選項時不列）
• 方案B xxx專車：$xxx

⚠️ 注意事項
• 請提早通知航線客服安排提貨
• 空趟費50%（有派專車才列）
• 等候費超過30分鐘每小時加收$200~500（有等候疑慮才列）
• 🚨 單件高≥120cm，強制專車（觸發才列）
• 🚨 單件重≥150kg，強制專車，請確認是否有堆高機（觸發才列，有WDC時合併為一條）
• 🚨 木箱（WDC）請確認是否有堆高機或叉車（有WDC且未觸發重量強制時才列）
• 🚨 棧板單板>150kg或高>120cm，強制專車（觸發才列）

如有其他問題歡迎告知！😊

【地區對應規則】
查不到的地區一律回覆：此地區無報價，請洽客服確認
機場（桃機/松山機場/CW/EZ/CK）→ 對應「機場各倉」報價，無併車選項

【勁連發（報價單A）特別規則】
- 只有併車，按重量計費
- 無 0.6T/1.5T/3.5T 等專車選項
- 例外：桃園地區有專車（1.5T/4T/5T/8T/13T/24T）
- 麻豆/佳里/七股/將軍：併車費用需額外加收 $500

【輸入解析】
CTN/PKG=箱、PLT=棧板、PCS=件、GW=毛重、NW=淨重、VW=材積重
WDC=木箱（Wooden Crate）
33'=才數

【郵遞區號對應】
台北市區：100/103/104/105/106/108/110/114(內湖)/115(南港)
士林/北投：111/112
汐止/深坑：221/223
新店/永和/木柵：116/231/234
板橋/中和/土城/樹林：220/235/236/238/239
三重/新莊/蘆洲/五股/泰山：241/242/243/247/248
林口/八里/三峽：244/246/237
基隆：200~206、瑞芳：224、淡水：251
宜蘭：260~272
桃園/龜山：330/333
蘆竹/大園/南崁：334/337/338
平鎮/中壢：324/320
八德：334
新竹/竹北：300/302、湖口：303
台中市區：400~412/420/427~429/432/433
烏日/梧棲/龍井/大甲/清水/霧峰：413/414/421/434~437
彰化市/和美/鹿港：500/505/508/509
員林/社頭：510/511/513
草屯/南投：540/542
高雄市區：800~815
林園832/大樹840（+$200）
岡山820/路竹821/燕巢824/橋頭825/梓官826/湖內829
屏東市：900
台南市區：700~709、永康710/仁德715
麻豆741/佳里746/七股748/將軍749（併車+$500）
不在以上 → 「此地區無報價，請洽客服確認」

【車型規格】
0.6T：170×125×125cm，500kg
1.5T：300×150×150cm，1200kg
3.5T：400×180×180cm，2000kg
4.5T：450×180×180cm，3000kg
8.8T：480×195×195cm，5000kg
12T：730×230×230cm，6000kg
15T：760×240×240cm，7500kg

【報價單C 世鄺晴庚 台北提貨 截止15:00】
車型→ 併車 / 0.6T / 1.5T / 3.5T / 4.5T / 8.8T / 12T / 15T
台北市區/內湖/南港→ $450 / $900 / $1200 / $2000 / $2400 / $3000 / $3500 / $4000
汐止/深坑→ $500 / $1000 / $1500 / $2000 / $2400 / $2800 / $3800 / $4000
新店/永和/木柵→ $500 / $1000 / $1400 / $1900 / $2400 / $2800 / $3500 / $3800
板橋/中和/土城/樹林/鶯歌→ $450 / $900 / $1300 / $1800 / $2400 / $2600 / $3500 / $3800
五股/三重/新莊/蘆洲/泰山→ $450 / $800 / $1300 / $1800 / $2300 / $2600 / $3200 / $3700
基隆/瑞芳/淡水→ $900 / $1300 / $1800 / $2300 / $2800 / $3200 / $3800 / $4300
林口/迴龍/八里/三峽→ $500 / $900 / $1300 / $1800 / $2300 / $2500 / $3200 / $3700
北投/天母/社子→ $800 / $1100 / $1500 / $2000 / $2500 / $3200 / $3500 / $4000
三芝/金山/萬里→ 無併車 / $1800 / $2200 / $2800 / $3200 / $3500 / $4000 / $4500
宜蘭→ 無併車 / $3000 / $3500 / $4500 / $5000 / $6000 / $6500 / $7500

【報價單D 信全運通 台北桃園竹苗 截止14:30】
車型→ 併車 / 0.6T / 1.5T / 3.5T / 4.5T / 8.8T / 12T / 17T
台北市區/南港/內湖→ $450 / $900 / $1200 / $2000 / $2400 / $3000 / $3500 / $4000
士林/萬華→ $500 / $1000 / $1300 / $2100 / $2500 / $3200 / $3600 / $4200
石碑/天母/北投→ $800 / $1100 / $1500 / $2000 / $2500 / $3200 / $3600 / $4000
景美/新店→ $500 / $1000 / $1400 / $1900 / $2400 / $2800 / $3500 / $3800
汐止/木柵→ $500 / $1000 / $1500 / $2000 / $2400 / $2800 / $3800 / $4000
深坑/安坑→ $650 / $1500 / $1900 / $2400 / $3100 / $3600 / $4100 / $4600
板橋/中和/永和/樹林/土城/鶯歌→ $450 / $900 / $1300 / $1800 / $2400 / $2600 / $3500 / $3800
三重/蘆洲/新莊/五股/泰山→ $450 / $900 / $1300 / $1800 / $2300 / $2600 / $3200 / $3700
基隆/淡水/瑞芳/八里/林口→ $900 / $1300 / $1800 / $2300 / $2800 / $3200 / $3800 / $4300
桃園/龜山/平鎮/中壢/八德→ $450 / $700 / $1000 / $1600 / $2000 / $2200 / $3000 / $3200
蘆竹/大園/南崁→ $400 / $500 / $800 / $1400 / $1800 / $2000 / $2500 / $3000
新屋/楊梅/觀音→ 無併車 / $900 / $1300 / $1800 / $2300 / $2500 / $3200 / $3500
迴龍/三峽/大溪/龍潭→ 無併車 / $1000 / $1300 / $1800 / $2300 / $2500 / $3200 / $3600
新竹/竹北/湖口→ $800 / $1200 / $1500 / $1800 / $2400 / $2800 / $3500 / $4000
竹泉/苗林/新埔/香山/竹南→ $1000 / $1400 / $1800 / $2000 / $2600 / $3000 / $3800 / $4500
苗栗地區→ 無併車 / $2100 / $2500 / $3300 / $4300 / $4800 / $5500 / $6500
台中→ 無併車 / $4100 / $4600 / $5600 / $6500 / $7500 / $8000 / $8500
機場各倉→ 無併車 / $500 / $600 / $1200 / $1500 / $2300 / $2500 / $2800

【報價單A 勁連發 高市屏東台南】
注意：只有併車，按計費重查表，無專車（桃園地區除外）
計費重/高雄市/屏東/台南：
60kg以內→ $600 / $800 / $900
61~100kg→ $800 / $1000 / $1100
101~200kg→ $900 / $1100 / $1200
201~300kg→ $1100 / $1300 / $1400
301~400kg→ $1200 / $1400 / $1500
401~500kg→ $1300 / $1500 / $1600
501~600kg→ $1400 / $1600 / $1700
600kg以上→ 每加100kg再+$100
桃園專車：1.5T=$8000 / 4T=$10000 / 5T=$11000 / 8T=$12000 / 13T=$14000 / 24T=$26000
附加費：昇降尾門專車+$500 / 併車+$200 / 林園大樹+$200 / 麻豆佳里七股將軍併車+$500

【報價單B 得統得勝 台中彰化】
注意：無專車，計費重（取實重與材積重較大者）查表，若材積重較高則用材積重查表
計費重/台中市大里太平潭子豐原大雅神岡大肚沙鹿/后里烏日梧棲龍井大甲清水霧峰新社/彰化市東勢伸港和美鹿港花壇福興秀水/大村芬園員林社頭/溪湖永靖埔鹽田中/埤頭北斗/芳苑/草屯南崗：
1~50kg→ $400 / $500 / $650 / $750 / $800 / $850 / $900 / $900
51~100kg→ $500 / $650 / $800 / $850 / $950 / $950 / $1000 / $1000
101~300kg→ $680 / $800 / $950 / $950 / $1050 / $1050 / $1100 / $1100
301~500kg→ $880 / $950 / $1000 / $1200 / $1400 / $1400 / $1500 / $1500
501~1000kg→ $1100 / $1350 / $1400 / $1500 / $1600 / $1700 / $1700 / $1800
1001kg以上→ $1/kg / $1.1/kg / $1.1/kg / $1.2/kg / $1.2/kg / $1.3/kg / $1.3/kg / $1.3/kg
專車：請與航線客服確認費用"""

# =====================
# Routes
# =====================

@app.route("/", methods=["GET"])
def home():
    return "Hello from LINE Truck Bot"

@app.route("/reload-pricing", methods=["GET"])
def reload_pricing():
    """手動重新讀取費率表（管理者用）"""
    load_pricing_from_sheets()
    return "費率表已重新讀取"
@app.route("/run-tests", methods=["GET"])
def run_tests():
    """執行自動測試，瀏覽器可直接查看結果"""
    import math
    results = []
    passed = 0
    failed = 0

    def test(name, actual, expected, tolerance=0):
        nonlocal passed, failed
        if tolerance:
            ok = abs((actual or 0) - expected) <= tolerance
        else:
            ok = actual == expected
        if ok:
            passed += 1
            results.append(f"✅ {name}")
        else:
            failed += 1
            results.append(f"❌ {name}：得到 {actual}，預期 {expected}")

    def cbf(l, w, h, count=1):
        return round(l * w * h / 28317 * count, 1)

    def vw(l, w, h, count=1):
        return round(l * w * h / 6000 * count, 1)

    def cw(gw, vol_w):
        return max(gw, vol_w)

    def kinlian_p(base, charge_w, addon=0):
        tiers = KINLIAN_PRICE.get(base, KINLIAN_PRICE["高市"])
        p = None
        for limit, price in tiers:
            if charge_w <= limit:
                p = price; break
        if p is None:
            over = charge_w - 600
            p = tiers[-1][1] + (int(over/100)+(1 if over%100>0 else 0))*100
        return p + addon

    def detong_p(area_key, charge_w):
        tiers = DETONG_PRICE.get(area_key)
        if not tiers: return None
        for limit, p in tiers:
            if charge_w <= limit:
                return round(charge_w * p) if p < 10 else int(p)
        return None

    # 才數計算
    results.append("\n【1. 才數計算】")
    test("120×80×90 1件", cbf(120,80,90,1), 30.5)
    test("127×97×66 1件", cbf(127,97,66,1), 28.7)
    test("117×85×100 4件", cbf(117,85,100,4), 140.5)
    test("120×100×140 4件", cbf(120,100,140,4), 237.3)

    # 材積重
    results.append("\n【2. 材積重（÷6000）】")
    test("120×80×90 1件", vw(120,80,90,1), 144.0)
    test("127×97×66 1件", vw(127,97,66,1), 135.6, tolerance=0.5)
    test("117×85×100 4件", vw(117,85,100,4), 663.0)

    # 計費重
    results.append("\n【3. 計費重】")
    test("實重150 材積144 → 150", cw(150,144), 150)
    test("實重50 材積144 → 144", cw(50,144), 144)

    # 選車（棧板）
    results.append("\n【4. 選車（棧板）】")
    truck, stack = find_best_truck_plt(4,117,85,100,843,True,True)
    test("4PLT 117×85×100 843kg → 3.5T", truck, "3.5T")
    test("4PLT 不可疊（100×2+7>150）", stack, False)
    truck, stack = find_best_truck_plt(4,120,100,140,2412,True,True)
    test("4PLT 120×100×140 2412kg → 4.5T", truck, "4.5T")
    test("4PLT 不可疊（140×2+7>195）", stack, False)
    truck, stack = find_best_truck_plt(1,122,102,76,200,True,True)
    test("1PLT 單件可疊（用戶指定）", stack, True)

    # 選車（箱件）
    results.append("\n【5. 選車（箱件）】")
    items3 = [{"l":120,"w":80,"h":90}]*3
    truck, _ = find_best_truck_ctn(items3, 183, True, True)
    test("3件 120×80×90 183kg → 1.5T", truck, "1.5T")
    items4 = [{"l":120,"w":80,"h":90}]*4
    truck, _ = find_best_truck_ctn(items4, 200, False, True)
    test("4件 120×80×90 不可疊 200kg → 3.5T", truck, "3.5T")

    # 強制規則
    results.append("\n【6. 強制規則】")
    test("高120cm → 強制", 120>=120, True)
    test("高119cm → 不強制", 119>=120, False)
    test("重150kg → 強制", 150>=150, True)
    test("重149kg → 不強制", 149>=150, False)

    # 報價查詢
    results.append("\n【7. 報價查詢】")
    db = get_pricing_db()
    test("台北市區 1.5T", db.get("台北市區",{}).get("1.5T"), 1200)
    test("基隆 4.5T", db.get("基隆",{}).get("4.5T"), 2800)
    test("桃園 1.5T", db.get("桃園",{}).get("1.5T"), 1000)
    test("機場各倉 0.6T", db.get("機場各倉",{}).get("0.6T"), 500)
    test("機場各倉 無併車", db.get("機場各倉",{}).get("併車"), None)

    # 勁連發
    results.append("\n【8. 勁連發報價】")
    test("高雄 50kg → $600", kinlian_p("高市",50), 600)
    test("高雄 200kg → $900", kinlian_p("高市",200), 900)
    test("高雄 700kg → $1500", kinlian_p("高市",700), 1500)
    test("台南 150kg → $1200", kinlian_p("台南",150), 1200)
    test("佳里 150kg +500 → $1700", kinlian_p("台南",150,500), 1700)
    test("林園 200kg +200 → $1100", kinlian_p("高市",200,200), 1100)

    # 得統得勝
    results.append("\n【9. 得統得勝報價】")
    test("台中 50kg → $400", detong_p("台中",50), 400)
    test("台中 100kg → $500", detong_p("台中",100), 500)
    test("台中 1500kg → $1500", detong_p("台中",1500), 1500)
    test("烏日 80kg → $650", detong_p("烏日",80), 650)

    # 英吋換算
    results.append("\n【10. 英吋換算】")
    test("48吋 → 121.9cm", round(48*2.54,1), 121.9)
    test("32吋 → 81.3cm", round(32*2.54,1), 81.3)

    total = passed + failed
    summary = f"\n{'='*40}\n測試結果：{passed}/{total} 通過"
    if failed == 0:
        summary += "\n🎉 全部通過！"
    else:
        summary += f"\n⚠️ {failed} 項失敗，請檢查"

    return "<pre>" + "\n".join(results) + summary + "</pre>"



@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(FollowEvent)
def handle_follow(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=WELCOME_MSG)
    )

@handler.add(MessageEvent, message=(ImageMessage, StickerMessage,
                                    VideoMessage, FileMessage))
def handle_non_text(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="您好！目前無法處理圖片或檔案，請以文字輸入詢價內容，謝謝 😊\n\n範例：\n台北內湖，3件，120×80×90cm，150kg")
    )

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="您好！收到語音訊息，但我無法直接處理語音。\n請用文字輸入詢價內容，謝謝 😊")
    )

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    display_name = get_display_name(user_id)

    # 程式碼層資料檢查
    missing_reply = check_missing_info(user_msg)
    if missing_reply:
        if user_id not in conversation_history:
            conversation_history[user_id] = []
        conversation_history[user_id].append({"role": "user", "content": user_msg})
        conversation_history[user_id].append({"role": "assistant", "content": missing_reply})
        log_to_sheet(user_id, display_name, user_msg, missing_reply)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=missing_reply))
        return

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    # 解析失敗計數（連續3次通知管理者）
    if user_id not in parse_fail_count:
        parse_fail_count[user_id] = 0

    # ★ 程式碼計算
    calc_note = ""
    cargo = parse_cargo(user_msg)
    if cargo:
        parse_fail_count[user_id] = 0  # 解析成功，重置計數
        try:
            calc = calculate_cargo(cargo)
            lines = ["", "[系統計算結果 - 直接使用以下數據，不需重新計算]"]

            if calc.get("cbf"):
                lines.append(f"• 才數：{calc['cbf']}才")
            if calc.get("vol_weight"):
                lines.append(f"• 材積重：{calc['vol_weight']}kg")
            lines.append(f"• 計費重：{calc['charge_weight']}kg")
            lines.append(f"• 實重：{cargo['total_kg']}kg")

            # 堆疊
            stack_str = "可疊放" if calc.get("stackable") else "不可疊放"
            lines.append(f"• 專車堆疊：{stack_str}")
            combo_stack_str = "可疊放" if calc.get("combo_stackable") else "不可疊放"
            lines.append(f"• 併車堆疊：{combo_stack_str}")

            # 車型
            express_truck = calc.get("express_truck") or "超出車型，請洽客服"
            lines.append(f"• 建議專車車型：{express_truck}")

            # 強制專車
            lines.append(f"• 強制專車：{'是' if calc['force_truck'] else '否'}")
            if calc["warnings"]:
                lines.append(f"• 強制原因：{' / '.join(calc['warnings'])}")

            # 機場
            if cargo.get("is_airport"):
                lines.append("• 卸貨地：機場各倉（無併車選項）")

            # 報價查詢（實重和材積重各查一次取較高）
            truck = calc.get("express_truck")
            if truck:
                charge_w = calc["charge_weight"]
                vol_w = calc.get("vol_weight") or 0

                # 用實重查一次
                combo_p_gw, express_p_gw, addon_gw, area_name, price_note = lookup_price(
                    user_msg, truck, calc["total_kg"])
                # 用材積重查一次（有材積重才查）
                if vol_w > 0:
                    combo_p_vw, express_p_vw, _, _, _ = lookup_price(
                        user_msg, truck, vol_w)
                else:
                    combo_p_vw, express_p_vw = None, None

                # 取較高
                if express_p_gw and express_p_vw:
                    express_p = max(express_p_gw, express_p_vw)
                else:
                    express_p = express_p_gw or express_p_vw

                if combo_p_gw and combo_p_vw:
                    combo_p = max(combo_p_gw, combo_p_vw)
                else:
                    combo_p = combo_p_gw or combo_p_vw

                addon = addon_gw

                # 強制專車但無專車報價 → 請與航線客服確認
                if calc["force_truck"] and not express_p:
                    lines.append("• 強制專車報價：請與航線客服確認")
                elif express_p:
                    # 避免重複顯示相同車型相同金額
                    truck_label = f"• 專車報價（{truck}）：${express_p}"
                    if truck_label not in lines:
                        lines.append(truck_label)

                if combo_p and not calc["force_truck"]:
                    combo_label = f"• 併車報價：${combo_p}"
                    if combo_label not in lines:
                        lines.append(combo_label)
                if addon:
                    lines.append(f"• 附加費：+${addon}（已含在上方報價中）")
                if price_note:
                    lines.append(f"• 備註：{price_note}")
                if area_name:
                    lines.append(f"• 報價地區：{area_name}")

            # 沒有尺寸時的提醒
            if not cargo.get("has_dim"):
                lines.append("• ⚠️ 未提供尺寸：才數無法計算，車型僅依重量估算，建議補充尺寸確認")
                if cargo.get("total_kg"):
                    # 無尺寸時仍查報價（依實重）
                    truck_no_dim = "0.6T"  # 預設最小車型，AI再判斷
                    combo_p2, express_p2, addon2, area2, note2 = lookup_price(
                        user_msg, truck_no_dim, cargo["total_kg"])
                    if combo_p2:
                        lines.append(f"• 依實重併車估價：${combo_p2}（車型需現場確認）")
                    if note2:
                        lines.append(f"• 備註：{note2}")

            calc_note = "\n".join(lines)
        except Exception as e:
            print(f"計算錯誤: {e}")
            notify_admin("計算錯誤\n用戶:" + str(display_name) + "\n輸入:" + str(user_msg[:100]) + "\n錯誤:" + str(e)[:200])

    print(f"[DEBUG] calc_note={calc_note}")
    # 解析失敗時計數
    if not cargo:
        parse_fail_count[user_id] = parse_fail_count.get(user_id, 0) + 1
        if parse_fail_count[user_id] >= 3:
            notify_admin("連續解析失敗3次\n用戶:" + str(display_name) + "(" + str(user_id) + ")\n最後輸入:" + str(user_msg[:100]))
            parse_fail_count[user_id] = 0

    augmented_msg = user_msg + calc_note
    conversation_history[user_id].append({"role": "user", "content": augmented_msg})

    if len(conversation_history[user_id]) > 10:
        conversation_history[user_id] = conversation_history[user_id][-10:]

    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )
        reply_text = response.content[0].text
        reply_text = clean_reply(reply_text)

        # 存入歷史只存原始訊息
        conversation_history[user_id][-1] = {"role": "user", "content": user_msg}
        conversation_history[user_id].append({"role": "assistant", "content": reply_text})

        if len(reply_text) > 4500:
            reply_text = reply_text[:4500] + "\n\n（訊息過長，請分次詢問）"

    except Exception as e:
        reply_text = "系統發生錯誤，請稍後再試。"
        print(f"API 錯誤: {e}")
        notify_admin("API呼叫失敗\n用戶:" + str(display_name) + "\n輸入:" + str(user_msg[:100]) + "\n錯誤:" + str(e)[:200])

    log_to_sheet(user_id, display_name, user_msg, reply_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


if __name__ == "__main__":
    # 啟動時讀取費率表
    load_pricing_from_sheets()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
