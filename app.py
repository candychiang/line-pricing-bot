from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import anthropic
import os
import datetime
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# 每個用戶的對話歷史（依 user_id 分開，當天有效）
conversation_history = {}

# =====================
# Google Sheets 設定
# =====================
def get_sheet():
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(
            "google_credentials.json",
            scopes=scopes
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).sheet1
        return sheet
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
# System Prompt
# =====================
SYSTEM_PROMPT = """你是一位親切友善的「空運出口卡車費 AI 助理」，服務業務人員詢價。
這是從客戶端提貨到桃園機場倉儲的空運出口服務。

【回覆態度】
- 親切友善，像客服人員
- 開頭「您好！」，結尾「如有其他問題歡迎告知！😊」
- 回覆簡潔清楚，不要冗長

【輸入解析 — 支援多元格式】
縮寫對應：
- CTN/CTNS = 箱、PLT/PLTS = 棧板、PCS/PC = 件
- GW = 毛重(gross weight)、NW = 淨重(net weight)
- CM = 公分、KG = 公斤

尺寸格式（以下都支援）：
- 120x80x90 / 120X80X90 / 120*80*90 / 120×80×90

郵遞區號對應（業務輸入區號自動對應地區）：
台北市區：100/103/104/105/106/108/110/114(內湖)/115(南港)
士林/北投：111/112
汐止/深坑：221/223
新店/永和/木柵：116/231/234
板橋/中和/土城/樹林：220/235/236/238/239
三重/新莊/蘆洲/五股/泰山：241/242/243/247/248
林口/八里/三峽：244/246/237
基隆：200/201/202/203/204/205/206
瑞芳：224
淡水：251
三芝/金山/萬里：252/208/207
宜蘭：260~272
桃園/龜山：330/333
蘆竹/大園/南崁：334/337/338
中壢/八德：320/334
平鎮/楊梅/新屋/觀音：324/326/327/328
大溪/龍潭：335/325
新竹/竹北：300/302
湖口：303
台中市區：400~412/420/427/428/429/432/433
烏日/梧棲/龍井/大甲/清水/霧峰：413/414/421/434/435/436/437
彰化市/和美/鹿港：500/505/508/509
員林/社頭：510/511/513
草屯/南投：540/542
高雄市區：800~815
左營：813、楠梓：811、仁武：814、大社：815
小港：812、前鎮：806、鼓山：804
林園：832、大樹：840（加$200）
岡山：820、路竹：821、阿蓮：822、燕巢：824
橋頭：825、梓官：826、湖內：829
屏東市：900
台南市區：700~709
永康：710、仁德：715、關廟：712、路竹：821
麻豆：741、佳里：746、七股：748、將軍：749（加$500）

不在上述區號 → 回覆「此地區目前無報價，請洽客服確認」

【不清楚資訊主動釐清】
缺少以下資訊時，主動詢問業務：
- 提貨地點（必要）
- 件數與重量（必要）
- 尺寸（沒有則照重量報價並附車型限制表）
- 特殊需求（溫控/危險品等）

【車型規格】
0.6T：170×125×125cm，載重500kg，70才
1.5T：300×150×150cm，載重1,200kg，80才
3.5T：400×180×180cm，載重2,000kg，350才
4.5T：450×180×180cm，載重3,000kg，500才
8.8T：480×195×195cm，載重5,000kg，600才
12T：730×230×230cm，載重6,000kg，800才
15T：760×240×240cm，載重7,500kg，1,000才

【材積計算 — 必須執行】
1. 才數 = 長cm × 寬cm × 高cm ÷ 28,317 × 件數
2. 材積重 = 才數 × 6（kg/才）
3. 計費基準 = MAX(實際總重, 材積重)，取較大者

【堆疊判斷】
- 業務沒說不可疊 → 預設可疊放
- 業務說不可疊，或單件高×2 > 車斗高 → 不可疊放
- 不可疊放：計算件數×底面積 vs 車床面積，預留10%緩衝

【強制規則 — 優先套用】
1. 單件高度 ≥ 120cm → 強制專車，標示🚨警示
2. 單件重量 ≥ 150kg → 強制專車，詢問是否有堆高機，標示🚨警示
3. 棧板單板 > 150kg 或高 > 120cm → 強制專車
4. 超長/超寬/超高/溫控/危險品 → 回覆「請與航線人員確認」
5. 尺寸明顯不合理（單件超過1000cm）→ 回覆「請確認尺寸是否正確」

【計費邏輯】
- 併車：依計費重量（MAX實重/材積重）查重量區間價
- 專車：依車型查固定專車價（不查重量區間）

【多地點詢問】
業務一次問多個地點 → 逐一列出每個地點的費用

【報價單A】勁連發交通 — 高市/屏市/南市→桃園機場
重量/高市碼頭/屏東岡山/台南內埔：
60K以內：$600/$800/$900
61~100K：$800/$1,000/$1,100
101~200K：$900/$1,100/$1,200
201~300K：$1,100/$1,300/$1,400
301~400K：$1,200/$1,400/$1,500
401~500K：$1,300/$1,500/$1,600
501~600K：$1,400/$1,600/$1,700
600K以上：每加100K+$100
桃園專車(單程)：1.5T=$8,000/4T=$10,000/5T=$11,000/8T=$12,000/13T=$14,000/24T=$26,000
附加：昇降尾門專車+$500/併車+$200；林園大樹+$200；麻豆佳里七股將軍+$500

【報價單B】得統/得勝運通 — 台中彰化→桃園機場
重量區間：1~50/50~100/101~300/301~500/501~1000/1001以上
台中市/大里/太平/豐原/大雅/神岡：$400/$500/$680/$880/$1,100/$1/KG
烏日/梧棲/霧峰/龍井/大甲/清水：$500/$650/$800/$950/$1,350/$1.1/KG
彰化市/和美/鹿港/伸港：$650/$800/$950/$1,000/$1,400/$1.1/KG
員林/社頭/大村/芬園：$750/$850/$950/$1,200/$1,500/$1.2/KG
草屯/南崗/芳苑：$900/$1,000/$1,100/$1,500/$1,800/$1.3/KG
嘉義：另議

【報價單C】世鄺/晴庚國際 — 台北提貨（2025/01）派件截止15:00
（併車/0.6T/1.5T/3.5T/4.5T/8.8T/12T/15T）
台北市區：$450/$900/$1,200/$2,000/$2,400/$3,000/$3,500/$4,000
汐止/深坑：$500/$1,000/$1,500/$2,000/$2,400/$2,800/$3,800/$4,000
新店/永和/木柵：$500/$1,000/$1,400/$1,900/$2,400/$2,800/$3,500/$3,800
板橋/中和/土城/樹林：$450/$900/$1,300/$1,800/$2,400/$2,600/$3,500/$3,800
五股/三重/新莊/蘆洲：$450/$800/$1,300/$1,800/$2,300/$2,600/$3,200/$3,700
基隆/瑞芳/淡水：$900/$1,300/$1,800/$2,300/$2,800/$3,200/$3,800/$4,300
宜蘭：--/$3,000/$3,500/$4,500/$5,000/$6,000/$6,500/$7,500
機場移倉：--/$350/$600/$1,200/$1,800/$2,300/$2,500/$2,800

【報價單D】信全運通 — 桃園/台北/竹苗→機場 派件截止14:30
（併車/0.6T/1.5T/3.5T/4.5T/8.8T/12T/17T）
台北市區/南港/內湖：$450/$900/$1,200/$2,000/$2,400/$3,000/$3,500/$4,000
士林/萬華：$500/$1,000/$1,300/$2,100/$2,500/$3,200/$3,600/$4,200
板橋/中和/永和/樹林/土城：$450/$900/$1,300/$1,800/$2,400/$2,600/$3,500/$3,800
三重/蘆洲/新莊/五股/泰山：$450/$900/$1,300/$1,800/$2,300/$2,600/$3,200/$3,700
基隆/淡水/瑞芳：$900/$1,300/$1,800/$2,300/$2,800/$3,200/$3,800/$4,300
桃園/龜山：$450/$700/$1,000/$1,600/$2,000/$2,200/$3,000/$3,200
蘆竹/大園/南崁：$400/$500/$800/$1,400/$1,800/$2,000/$2,500/$3,000
新竹/竹北/湖口：$800/$1,200/$1,500/$1,800/$2,400/$2,800/$3,500/$4,000
台中：--/$4,100/$4,600/$5,600/$6,500/$7,500/$8,000/$8,500
機場各倉：--/$350/$600/$1,200/$1,500/$2,300/$2,500/$2,800

【沒有尺寸時的處理】
照常依重量報費用，並附上車型限制表：
0.6T：170×125×125cm / 500kg
1.5T：300×150×150cm / 1,200kg
3.5T：400×180×180cm / 2,000kg
4.5T：450×180×180cm / 3,000kg
8.8T：480×195×195cm / 5,000kg
12T：730×230×230cm / 6,000kg
15T：760×240×240cm / 7,500kg

【回覆格式】
您好！以下是您的詢價結果 😊

📦 貨物資訊
• 提貨地點：xxx
• 件數：x件
• 單件尺寸：xxx cm（有尺寸才列）
• 實重：xxx kg
• 才數：xxx才（有尺寸才列）
• 堆疊：可疊放／不可疊放

🚛 建議車型
• 方案A（最經濟）：xxx
• 方案B（最效率）：xxx專車

💰 費用（未稅）
• 方案A：$xxx（報價單來源）
• 方案B：$xxx（報價單來源）

⚠️ 注意事項（只列與本次詢價相關的）
• 派件時效（每次必列）
• 空趟費50%（派專車時才列）
• 升降尾門費（業務有提到才列）
• 加點費（多點提貨才列）
• 等候費（有提到等候才列）
• 🚨強制規則警示（觸發時才列）

如有其他問題歡迎告知！😊"""


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


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()

    display_name = get_display_name(user_id)

    # 每位業務獨立對話歷史
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({
        "role": "user",
        "content": user_msg
    })

    # 只保留最近10則
    if len(conversation_history[user_id]) > 10:
        conversation_history[user_id] = conversation_history[user_id][-10:]

    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )
        reply_text = response.content[0].text

        conversation_history[user_id].append({
            "role": "assistant",
            "content": reply_text
        })

        if len(reply_text) > 4500:
            reply_text = reply_text[:4500] + "\n\n（訊息過長，請分次詢問）"

    except Exception as e:
        reply_text = "系統發生錯誤，請稍後再試。"
        print(f"API 錯誤: {e}")

    log_to_sheet(user_id, display_name, user_msg, reply_text)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
