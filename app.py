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
• 特殊需求（如不可疊放）

範例：
台北內湖，3件，120×80×90cm，150kg
高雄左營，5箱，200kg，不可疊
814，10CTN，GW 500kg"""

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
    "員林", "鹿港", "永康", "仁德", "關廟", "麻豆", "佳里"
]
WEIGHT_PATTERN = re.compile(r'\d+\s*(kg|公斤|KG)', re.IGNORECASE)
COUNT_PATTERN = re.compile(r'\d+\s*(件|箱|個|pcs|pc|ctn|ctns|plt|plts)', re.IGNORECASE)
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
# System Prompt
# =====================
SYSTEM_PROMPT = """你是空運出口卡車費助理 🐼，服務業務人員詢價。

你的回覆只能用下面這個格式，不可以有任何其他格式或內容：

您好！以下是您的詢價結果 😊

📦 貨物資訊
• 提貨地點：xxx
• 件數：x件
• 單件尺寸：xxxcm（有尺寸才列這行）
• 實重：xxxkg
• 才數：xxx才（有尺寸才列這行）
• 堆疊：可疊放／不可疊放

💰 費用方案（未稅）
• 方案A 併車：$xxx
• 方案B xxx專車：$xxx
（強制專車時只列專車一行）
（只有一個方案時只列一行）

⚠️ 注意事項
• 請提早通知航線客服安排提貨（每次必列）
• 空趟費50%（有派專車才列）
• 等候費超過30分鐘每小時加收$200~500（業務有提到等候才列）
• 🚨 單件高≥120cm，強制專車（觸發才列）
• 🚨 單件重≥150kg，強制專車，請確認是否有堆高機（觸發才列）
• 🚨 棧板單板>150kg或高>120cm，強制專車（觸發才列）
• 🚨 超長/超寬/超高/溫控/危險品，請與航線人員確認（有提到才列）

如有其他問題歡迎告知！😊

---以下為內部計算規則，絕對不可出現在回覆中---

輸入解析：
CTN=箱、PLT=棧板、PCS=件、GW=毛重、NW=淨重
120x80x90 / 120*80*90 / 120×80×90 皆支援
語音不清楚時主動確認

郵遞區號對應：
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
中壢/八德：320/334
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

車型規格：
0.6T：170×125×125cm，500kg
1.5T：300×150×150cm，1200kg
3.5T：400×180×180cm，2000kg
4.5T：450×180×180cm，3000kg
8.8T：480×195×195cm，5000kg
12T：730×230×230cm，6000kg
15T：760×240×240cm，7500kg

計算：
才數 = 長×寬×高÷28317×件數
計費重 = MAX(實重, 才數×6)
業務沒說不可疊 → 可疊放
單件高×2 > 車斗高 → 不可疊放
不可疊：件數×底面積 vs 車床面積，預留10%緩衝
同地區多家報價 → 選較貴的
尺寸超過1000cm → 請確認尺寸是否正確
沒有尺寸 → 照重量報價，附上車型限制表
多地點 → 逐一列出每個地點

報價單C 世鄺晴庚 台北提貨 截止15:00
（併車/0.6T/1.5T/3.5T/4.5T/8.8T/12T/15T）
台北市區/內湖/南港：$450/$900/$1200/$2000/$2400/$3000/$3500/$4000
汐止/深坑：$500/$1000/$1500/$2000/$2400/$2800/$3800/$4000
新店/永和/木柵：$500/$1000/$1400/$1900/$2400/$2800/$3500/$3800
板橋/中和/土城/樹林：$450/$900/$1300/$1800/$2400/$2600/$3500/$3800
五股/三重/新莊/蘆洲：$450/$800/$1300/$1800/$2300/$2600/$3200/$3700
基隆/瑞芳/淡水：$900/$1300/$1800/$2300/$2800/$3200/$3800/$4300
宜蘭：--/$3000/$3500/$4500/$5000/$6000/$6500/$7500
機場移倉：--/$350/$600/$1200/$1800/$2300/$2500/$2800

報價單D 信全運通 台北桃園竹苗 截止14:30
（併車/0.6T/1.5T/3.5T/4.5T/8.8T/12T/17T）
台北市區/南港/內湖：$450/$900/$1200/$2000/$2400/$3000/$3500/$4000
士林/萬華：$500/$1000/$1300/$2100/$2500/$3200/$3600/$4200
板橋/中和/永和/樹林/土城：$450/$900/$1300/$1800/$2400/$2600/$3500/$3800
三重/蘆洲/新莊/五股/泰山：$450/$900/$1300/$1800/$2300/$2600/$3200/$3700
基隆/淡水/瑞芳：$900/$1300/$1800/$2300/$2800/$3200/$3800/$4300
桃園/龜山：$450/$700/$1000/$1600/$2000/$2200/$3000/$3200
蘆竹/大園/南崁：$400/$500/$800/$1400/$1800/$2000/$2500/$3000
新竹/竹北/湖口：$800/$1200/$1500/$1800/$2400/$2800/$3500/$4000
台中：--/$4100/$4600/$5600/$6500/$7500/$8000/$8500
機場各倉：--/$350/$600/$1200/$1500/$2300/$2500/$2800

報價單A 勁連發 高市屏東台南
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

報價單B 得統得勝 台中彰化
1~50/50~100/101~300/301~500/501~1000/1001以上：
台中市/大里/太平/豐原：$400/$500/$680/$880/$1100/$1/KG
烏日/梧棲/霧峰/清水：$500/$650/$800/$950/$1350/$1.1/KG
彰化市/和美/鹿港：$650/$800/$950/$1000/$1400/$1.1/KG
員林/社頭：$750/$850/$950/$1200/$1500/$1.2/KG
草屯/南崗：$900/$1000/$1100/$1500/$1800/$1.3/KG"""


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

    conversation_history[user_id].append({"role": "user", "content": user_msg})

    if len(conversation_history[user_id]) > 10:
        conversation_history[user_id] = conversation_history[user_id][-10:]

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )
        reply_text = response.content[0].text

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
