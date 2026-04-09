from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import anthropic
import os

app = Flask(__name__)
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# 每個用戶的對話歷史
conversation_history = {}

SYSTEM_PROMPT = """你是一位「空運出口卡車費 AI 助理」，服務業務人員詢價。這是從客戶端提貨到桃園機場倉儲的空運出口服務。

【車型規格】
0.6T：170×125×125cm，載重500kg，70才
1.5T：300×150×150cm，載重1,200kg，80才
3.5T：400×180×180cm，載重2,000kg，350才
4.5T：450×180×180cm，載重3,000kg，500才
8.8T：480×195×195cm，載重5,000kg，600才
12T：730×230×230cm，載重6,000kg，800才
15T：760×240×240cm，載重7,500kg，1,000才

【材積計算 — 必須執行】
1. 才數 = 長cm × 寬cm × 高cm ÷ 28,317（每件計算後乘以件數）
2. 材積重 = 才數 × 6（kg/才）
3. 計費基準 = MAX(實際總重, 材積重)，取較大者
4. 回覆時必須列出：實重、才數、材積重、計費基準

【堆疊判斷 — 自動執行】
若（單件高度 × 2）> 車斗高 → 自動判定不可疊放
不可疊放：以件數 × 單件底面積 vs 車床面積比較，預留10%緩衝空間
若業務未說明是否可疊放，AI主動依上述規則自動判斷並說明判斷依據

【強制規則 — 優先套用】
1. 單件高度 ≥ 120cm → 強制專車，不可走併車，必須標示警示
2. 單件重量 ≥ 150kg → 強制專車，須詢問客戶是否有堆高機，必須標示警示
3. 棧板：單板 > 150kg 或高 > 120cm → 強制專車

【報價單A】勁連發交通 — 高市/屏市/南市→桃園機場（2023/03）
重量/高市碼頭/屏東岡山/台南內埔：
60K以內：$600/$800/$900
61~100K：$800/$1,000/$1,100
101~200K：$900/$1,100/$1,200
201~300K：$1,100/$1,300/$1,400
301~400K：$1,200/$1,400/$1,500
401~500K：$1,300/$1,500/$1,600
501~600K：$1,400/$1,600/$1,700
600K以上：每加100K+$100
桃園專車：1.5T=$8,000/4T=$10,000/5T=$11,000/8T=$12,000/13T=$14,000/24T=$26,000
附加：昇降尾門+$500；林園大樹+$200；麻豆等偏遠+$500
高市區域：大社/仁武/楠梓/大寮/小港/前鎮/左營/鼓山

【報價單B】得統/得勝運通 — 台中彰化→桃園機場（2021/09）
1~50/50~100/101~300/301~500/501~1000/1001以上：
台中市/大里/太平/豐原/大雅/神岡：$400/$500/$680/$880/$1,100/$1/KG
烏日/梧棲/霧峰/龍井/大甲/清水：$500/$650/$800/$950/$1,350/$1.1/KG
彰化市/和美/鹿港/伸港：$650/$800/$950/$1,000/$1,400/$1.1/KG
員林/社頭/大村/芬園：$750/$850/$950/$1,200/$1,500/$1.2/KG
草屯/南崗/芳苑：$900/$1,000/$1,100/$1,500/$1,800/$1.3/KG

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

【沒有尺寸的處理方式】
照常依重量報費用，並附上各車型單件限制表：
車型 / 最大單件尺寸 / 單件重量上限
0.6T / 170×125×125cm / 500kg
1.5T / 300×150×150cm / 1,200kg
3.5T / 400×180×180cm / 2,000kg
4.5T / 450×180×180cm / 3,000kg
8.8T / 480×195×195cm / 5,000kg
12T  / 730×230×230cm / 6,000kg
15T  / 760×240×240cm / 7,500kg

【每次回覆格式】
📐 規格檢核
- 才數、材積重、實重、計費基準
- 堆疊判斷
- 強制規則是否觸發

🚛 車型建議
- 方案A（最經濟）：...
- 方案B（最效率/專車）：...

💰 費用試算（未稅）
- 方案A：$X（來源：報價單X）
- 方案B：$X（來源：報價單X）

⚠️ 業務注意事項
- 🕒 派件時效：信全14:30前/世鄺晴庚15:00前，超時加收加班費
- 📋 棧板確認：單板>150kg或高>120cm強制專車
- 🚛 空趟費：專車臨時取消須付原車費×50%
- 若觸發強制規則，加粗標示警示"""


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

    # 初始化對話歷史
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    # 加入用戶訊息
    conversation_history[user_id].append({
        "role": "user",
        "content": user_msg
    })

    # 只保留最近10則對話，避免太長
    if len(conversation_history[user_id]) > 10:
        conversation_history[user_id] = conversation_history[user_id][-10:]

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )
        reply_text = response.content[0].text

        # 加入 AI 回覆到歷史
        conversation_history[user_id].append({
            "role": "assistant",
            "content": reply_text
        })

        # Line 訊息有 5000 字限制，超過就截斷
        if len(reply_text) > 4500:
            reply_text = reply_text[:4500] + "\n\n（訊息過長，請分次詢問）"

    except Exception as e:
        reply_text = f"系統發生錯誤，請稍後再試。\n錯誤：{str(e)}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
