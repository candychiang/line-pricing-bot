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
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

conversation_history = {}

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

def get_sheet():
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(
            "google_credentials.json", scopes=scopes)
        client = gspread.authorize(creds)
        return client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).sheet1
    except Exception as e:
        print(f"Google Sheets 連線失敗: {e}")
        return None

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
    "中壢", "八德", "蘆竹"
]
WEIGHT_PATTERN = re.compile(r'\d+\s*(kg|公斤|KG)', re.IGNORECASE)
COUNT_PATTERN = re.compile(r'\d+\s*(件|箱|個|pcs|pc|ctn|ctns|plt|plts|wdc)', re.IGNORECASE)
POSTAL_PATTERN = re.compile(r'\b\d{3}\b')

def has_location(text):
    if POSTAL_PATTERN.search(text):
        return True
    return any(kw in text for kw in LOCATION_KEYWORDS)

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

GAP = 7  # 棧板間及與牆壁間距7cm

# =====================
# 選車邏輯（程式碼計算）
# =====================

def can_fit_arrangement(rows, cols, item_l, item_w, truck_l, truck_w):
    """
    檢查 rows×cols 排列是否放得進車廂
    棧板間及牆壁間距各 GAP cm
    rows = 沿長度方向幾個, cols = 沿寬度方向幾個
    """
    needed_l = item_l * rows + GAP * (rows + 1)
    needed_w = item_w * cols + GAP * (cols + 1)
    return needed_l <= truck_l and needed_w <= truck_w

def find_best_truck(count, item_l, item_w, item_h, total_kg, can_stack, force_truck=False):
    """
    找出最小可用車型
    can_stack: 是否可疊放
    force_truck: 強制專車（不考慮併車）
    回傳 (truck_name, truck_height, is_stackable_result)
    """
    # 如果可疊放，先確認疊放後高度是否超過車斗
    # 疊放：件數減半（往高疊）
    for truck in TRUCK_TYPES:
        truck_l = truck["length"]
        truck_w = truck["width"]
        truck_h = truck["height"]
        truck_kg = truck["max_kg"]

        if total_kg > truck_kg:
            continue

        # 判斷實際可疊放（高度限制）
        actually_stackable = can_stack and (item_h * 2 <= truck_h)

        if actually_stackable:
            # 可疊放：件數除以2（無條件進位）
            effective_count = math.ceil(count / 2)
        else:
            effective_count = count

        # 嘗試所有排列方式（含旋轉）
        fitted = False
        orientations = [(item_l, item_w), (item_w, item_l)] if item_l != item_w else [(item_l, item_w)]

        for ol, ow in orientations:
            for rows in range(1, effective_count + 1):
                cols = math.ceil(effective_count / rows)
                if rows * cols < effective_count:
                    continue
                if can_fit_arrangement(rows, cols, ol, ow, truck_l, truck_w):
                    fitted = True
                    break
                # 也試試 cols×rows
                if can_fit_arrangement(cols, rows, ol, ow, truck_l, truck_w):
                    fitted = True
                    break
            if fitted:
                break

        if fitted:
            return truck["name"], truck_h, actually_stackable

    return None, None, False  # 找不到合適車型

def calculate_cargo(count, item_l, item_w, item_h, total_kg, can_stack):
    """
    主計算函式，回傳計算結果 dict
    """
    result = {}

    # 才數
    cbf_per_item = (item_l * item_w * item_h) / 28317
    total_cbf = cbf_per_item * count
    result["cbf"] = round(total_cbf, 1)

    # 材積重 & 計費重
    vol_weight = total_cbf * 6
    charge_weight = max(total_kg, vol_weight)
    result["charge_weight"] = round(charge_weight, 1)

    # 強制規則
    force_truck = False
    warnings = []

    if item_h >= 120:
        force_truck = True
        warnings.append("🚨 單件高≥120cm，強制專車")
    if (total_kg / count) >= 150:
        force_truck = True
        warnings.append("🚨 單件重≥150kg，強制專車，請確認是否有堆高機")

    result["force_truck"] = force_truck
    result["warnings"] = warnings

    # 選車
    truck_name, truck_h, actually_stackable = find_best_truck(
        count, item_l, item_w, item_h, total_kg, can_stack, force_truck
    )

    result["truck"] = truck_name
    result["stackable"] = actually_stackable

    return result

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

【重要】選車計算已由程式完成，user訊息中會附上「[系統計算]」區塊，裡面包含才數、計費重、建議車型、堆疊結果。你只需要：
1. 依據系統計算結果填入格式
2. 查報價單給出費用
3. 不需要自己重新計算

【回覆格式】嚴格照以下格式，不可增加其他段落：

您好！以下是您的詢價結果 😊

📦 貨物資訊
• 提貨地點：xxx
• 件數：x件（有木箱標註 WDC）
• 單件尺寸：xxxcm（無尺寸不列）
• 實重：xxxkg
• 才數：xxx才（無尺寸不列）
• 堆疊：可疊放／不可疊放

💰 費用方案
• 方案A 併車：$xxx（強制專車時不列此行）
• 方案B xxx專車：$xxx

⚠️ 注意事項
• 請提早通知航線客服安排提貨
• 空趟費50%（有派專車才列）
• 等候費超過30分鐘每小時加收$200~500
• 🚨 單件高≥120cm，強制專車（觸發才列）
• 🚨 單件重≥150kg，強制專車，請確認是否有堆高機（觸發才列）
• 🚨 棧板單板>150kg或高>120cm，強制專車（觸發才列）
• 🚨 木箱（WDC）與棧板 (PLT) 請確認是否有堆高機或叉車（有WDC與PLT才列）

如有其他問題歡迎告知！😊

【輸入解析】
CTN=箱、PLT=棧板、PCS=件、GW=毛重、NW=淨重
WDC=木箱（Wooden Crate）

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
蘆竹/大園/南崁/平鎮/中壢：334/337/338/324/320
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
麻豆741/佳里746/七股748/將軍749（+$500）
不在以上 → 「此地區無報價，請洽客服確認」

【報價單C 世鄺晴庚 台北提貨 截止15:00】
（併車/0.6T/1.5T/3.5T/4.5T/8.8T/12T/15T）
台北市區/內湖/南港：$450/$900/$1200/$2000/$2400/$3000/$3500/$4000
汐止/深坑：$500/$1000/$1500/$2000/$2400/$2800/$3800/$4000
新店/永和/木柵：$500/$1000/$1400/$1900/$2400/$2800/$3500/$3800
板橋/中和/土城/樹林：$450/$900/$1300/$1800/$2400/$2600/$3500/$3800
五股/三重/新莊/蘆洲：$450/$800/$1300/$1800/$2300/$2600/$3200/$3700
基隆/瑞芳/淡水：$900/$1300/$1800/$2300/$2800/$3200/$3800/$4300
宜蘭：–/$3000/$3500/$4500/$5000/$6000/$6500/$7500
機場移倉：–/$350/$600/$1200/$1800/$2300/$2500/$2800

【報價單D 信全運通 台北桃園竹苗 截止14:30】
（併車/0.6T/1.5T/3.5T/4.5T/8.8T/12T/17T）
台北市區/南港/內湖：$450/$900/$1200/$2000/$2400/$3000/$3500/$4000
士林/萬華：$500/$1000/$1300/$2100/$2500/$3200/$3600/$4200
板橋/中和/永和/樹林/土城：$450/$900/$1300/$1800/$2400/$2600/$3500/$3800
三重/蘆洲/新莊/五股/泰山：$450/$900/$1300/$1800/$2300/$2600/$3200/$3700
基隆/淡水/瑞芳：$900/$1300/$1800/$2300/$2800/$3200/$3800/$4300
桃園/龜山/平鎮/中壢/八德：$450/$700/$1000/$1600/$2000/$2200/$3000/$3200
蘆竹/大園/南崁：$400/$500/$800/$1400/$1800/$2000/$2500/$3000
新竹/竹北/湖口：$800/$1200/$1500/$1800/$2400/$2800/$3500/$4000
台中：–/$4100/$4600/$5600/$6500/$7500/$8000/$8500
機場各倉：–/$350/$600/$1200/$1500/$2300/$2500/$2800

【報價單A 勁連發 高市屏東台南】
重量/高市/屏東/台南：
60K以內：$600/$800/$900
61~100K：$800/$1000/$1100
101~200K：$900/$1100/$1200
201~300K：$1100/$1300/$1400
301~400K：$1200/$1400/$1500
401~500K：$1300/$1500/$1600
501~600K：$1400/$1600/$1700
600K以上每加100K+$100
桃園專車：1.5T=$8000/4T=$10000/5T=$11000/8T=$12000/13T=$14000/24T=$26000
附加：昇降尾門專車+$500/併車+$200；林園大樹+$200；麻豆佳里七股將軍+$500

【報價單B 得統得勝 台中彰化】
1~50/50~100/101~300/301~500/501~1000/1001以上：
台中市/大里/太平/豐原：$400/$500/$680/$880/$1100/$1/KG
烏日/梧棲/霧峰/清水：$500/$650/$800/$950/$1350/$1.1/KG
彰化市/和美/鹿港：$650/$800/$950/$1000/$1400/$1.1/KG
員林/社頭：$750/$850/$950/$1200/$1500/$1.2/KG
草屯/南崗：$900/$1000/$1100/$1500/$1800/$1.3/KG"""

# =====================
# 解析貨物資訊（供程式計算用）
# =====================

def parse_cargo(text):
    """
    嘗試從用戶訊息解析出尺寸、件數、重量、是否可疊
    回傳 dict 或 None（無法解析）
    """
    result = {}

    # 件數（PLT/CTN/箱/件/WDC）
    count_match = re.search(r'(\d+)\s*(plt|plts|棧板|plt)', text, re.IGNORECASE)
    if not count_match:
        count_match = re.search(r'(\d+)\s*(ctn|ctns|箱|件|個|pcs|pc|wdc)', text, re.IGNORECASE)
    if count_match:
        result["count"] = int(count_match.group(1))
        unit = count_match.group(2).lower()
        result["is_pallet"] = unit in ["plt", "plts", "棧板"]
        result["is_wdc"] = unit == "wdc"
    else:
        return None

    # 尺寸：支援 × * x 分隔，可能有 *件數 在後面
    dim_match = re.search(
        r'(\d+(?:\.\d+)?)\s*[×x*Xx]\s*(\d+(?:\.\d+)?)\s*[×x*Xx]\s*(\d+(?:\.\d+)?)(?:\s*[×x*Xx]\s*\d+)?',
        text
    )
    if dim_match:
        dims = sorted([float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))], reverse=True)
        result["item_l"] = dims[0]
        result["item_w"] = dims[1]
        result["item_h"] = dims[2]
        result["has_dim"] = True
    else:
        result["has_dim"] = False

    # 重量
    weight_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:kg|KG|公斤)', text, re.IGNORECASE)
    if weight_match:
        result["total_kg"] = float(weight_match.group(1))
    else:
        return None

    # 堆疊
    result["can_stack"] = "不可疊" not in text and "不可叠" not in text

    return result

# =====================
# Routes
# =====================

@app.route("/", methods=["GET"])
def home():
    return "Hello from LINE Truck Bot"

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
        TextSendMessage(text="無法處理此類訊息，請輸入文字 😊")
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

    # ★ 程式碼計算選車邏輯
    calc_note = ""
    cargo = parse_cargo(user_msg)
    if cargo and cargo.get("has_dim"):
        try:
            calc = calculate_cargo(
                count=cargo["count"],
                item_l=cargo["item_l"],
                item_w=cargo["item_w"],
                item_h=cargo["item_h"],
                total_kg=cargo["total_kg"],
                can_stack=cargo["can_stack"]
            )
            stack_str = "可疊放" if calc["stackable"] else "不可疊放"
            truck_str = calc["truck"] if calc["truck"] else "超出所有車型，請洽客服"
            warnings_str = "\n".join(calc["warnings"]) if calc["warnings"] else "無"
            force_str = "是" if calc["force_truck"] else "否"

            calc_note = f"""

[系統計算結果 - 直接使用以下數據，不需重新計算]
• 才數：{calc['cbf']}才
• 計費重：{calc['charge_weight']}kg
• 堆疊判斷：{stack_str}
• 建議最小車型：{truck_str}
• 強制專車：{force_str}
• 強制原因：{warnings_str}
"""
        except Exception as e:
            print(f"計算錯誤: {e}")

    # 組合訊息（原始訊息 + 系統計算結果）
    augmented_msg = user_msg + calc_note

    conversation_history[user_id].append({"role": "user", "content": augmented_msg})

    # 保留最近 10 則對話
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

        # 存入歷史時只存原始訊息（不帶計算區塊）
        conversation_history[user_id][-1] = {"role": "user", "content": user_msg}
        conversation_history[user_id].append({"role": "assistant", "content": reply_text})

        if len(reply_text) > 4500:
            reply_text = reply_text[:4500] + "\n\n（訊息過長，請分次詢問）"

    except Exception as e:
        reply_text = "系統發生錯誤，請稍後再試。"
        print(f"API 錯誤: {e}")

    log_to_sheet(user_id, display_name, user_msg, reply_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
