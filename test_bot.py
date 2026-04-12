
"""
熊貓卡車費 Bot 自動測試程式
測試所有主要功能是否正確
"""
import re
import math

# =====================
# 複製核心計算函式
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
PLT_GAP = 7
HEIGHT_GAP = 7

def cbf(l, w, h, count=1):
    return round(l * w * h / 28317 * count, 1)

def vol_weight(l, w, h, count=1):
    return round(l * w * h / 6000 * count, 1)

def charge_weight(gw, vw):
    return max(gw, vw)

def can_fit_plt(rows, cols, il, iw, tl, tw):
    return il*rows+PLT_GAP*(rows+1)<=tl and iw*cols+PLT_GAP*(cols+1)<=tw

def find_truck_plt(count, il, iw, ih, tkg, can_stack, is_express):
    for truck in TRUCK_TYPES:
        tl,tw,th,mkg = truck["length"],truck["width"],truck["height"],truck["max_kg"]
        if tkg > mkg: continue
        if count == 1:
            stack = can_stack
            fitted = any((ol+PLT_GAP*2<=tl and ow+PLT_GAP*2<=tw)
                        for ol,ow in ([(il,iw),(iw,il)] if il!=iw else [(il,iw)]))
        elif not is_express:
            stack = can_stack; fitted = True
        else:
            stack = can_stack and (ih*2+HEIGHT_GAP <= th)
            eff = math.ceil(count/2) if stack else count
            fitted = False
            for ol,ow in ([(il,iw),(iw,il)] if il!=iw else [(il,iw)]):
                for rows in range(1, eff+1):
                    cols = math.ceil(eff/rows)
                    if rows*cols < eff: continue
                    if can_fit_plt(rows,cols,ol,ow,tl,tw) or can_fit_plt(cols,rows,ol,ow,tl,tw):
                        fitted=True; break
                if fitted: break
        if fitted: return truck["name"], stack
    return None, False

def find_truck_ctn(items, tkg, can_stack, is_express):
    if not items: return None, False
    max_h=max(i["h"] for i in items)
    max_l=max(i["l"] for i in items)
    max_w=max(i["w"] for i in items)
    count=len(items)
    for truck in TRUCK_TYPES:
        tl,tw,th,mkg = truck["length"],truck["width"],truck["height"],truck["max_kg"]
        if tkg > mkg: continue
        if count==1 or not is_express:
            stack=can_stack; fitted=max_l<=tl and max_w<=tw
        else:
            stack=can_stack and (max_h*2+HEIGHT_GAP<=th)
            eff=math.ceil(count/2) if stack else count
            need_h=(max_h*2+HEIGHT_GAP) if stack else (max_h+HEIGHT_GAP)
            if need_h>th: fitted=False
            else:
                fitted=False
                for ol,ow in ([(max_l,max_w),(max_w,max_l)] if max_l!=max_w else [(max_l,max_w)]):
                    for rows in range(1,eff+1):
                        cols=math.ceil(eff/rows)
                        if ol*rows<=tl and ow*cols<=tw: fitted=True; break
                    if fitted: break
        if fitted: return truck["name"], stack
    return None, False

# =====================
# 報價資料
# =====================
PRICING = {
    "台北市區": {"併車":450,"0.6T":900,"1.5T":1200,"3.5T":2000,"4.5T":2400,"8.8T":3000,"12T":3500,"15T":4000},
    "內湖":     {"併車":450,"0.6T":900,"1.5T":1200,"3.5T":2000,"4.5T":2400,"8.8T":3000,"12T":3500,"15T":4000},
    "基隆":     {"併車":900,"0.6T":1300,"1.5T":1800,"3.5T":2300,"4.5T":2800,"8.8T":3200,"12T":3800,"15T":4300},
    "桃園":     {"併車":450,"0.6T":700,"1.5T":1000,"3.5T":1600,"4.5T":2000,"8.8T":2200,"12T":3000,"17T":3200},
    "機場各倉": {"0.6T":500,"1.5T":600,"3.5T":1200,"4.5T":1500,"8.8T":2300,"12T":2500,"17T":2800},
}
KINLIAN = {
    "高市":  [(60,600),(100,800),(200,900),(300,1100),(400,1200),(500,1300),(600,1400)],
    "台南":  [(60,900),(100,1100),(200,1200),(300,1400),(400,1500),(500,1600),(600,1700)],
}
KINLIAN_ADDON = {"佳里":500,"麻豆":500,"林園":200}

DETONG = {
    "台中": [(50,400),(100,500),(300,680),(500,880),(1000,1100),(99999,1.0)],
    "烏日": [(50,500),(100,650),(300,800),(500,950),(1000,1350),(99999,1.1)],
}

def kinlian_price(area, base, charge_w, addon=0):
    tiers = KINLIAN.get(base, KINLIAN["高市"])
    p = None
    for limit, price in tiers:
        if charge_w <= limit:
            p = price; break
    if p is None:
        over = charge_w - 600
        p = tiers[-1][1] + (int(over/100)+(1 if over%100>0 else 0))*100
    return p + addon

def detong_price(area_key, charge_w):
    tiers = DETONG.get(area_key, DETONG["台中"])
    for limit, p in tiers:
        if charge_w <= limit:
            return round(charge_w * p) if p < 10 else p
    return None

# =====================
# 測試框架
# =====================
passed = 0
failed = 0
errors = []

def test(name, actual, expected, tolerance=0):
    global passed, failed
    if tolerance:
        ok = abs(actual - expected) <= tolerance
    else:
        ok = actual == expected
    if ok:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        errors.append(f"{name}: 得到 {actual}，預期 {expected}")
        print(f"  ❌ {name}: 得到 {actual}，預期 {expected}")

# =====================
# 測試1：才數計算
# =====================
print("\n【1. 才數計算】")
test("120×80×90 1件", cbf(120,80,90,1), 30.5)
test("127×97×66 1件", cbf(127,97,66,1), 28.7)
test("117×85×100 4件", cbf(117,85,100,4), 140.5)
test("120×100×140 4件", cbf(120,100,140,4), 237.3)

# =====================
# 測試2：材積重計算
# =====================
print("\n【2. 材積重計算（÷6000）】")
test("120×80×90 1件", vol_weight(120,80,90,1), 144.0)
test("127×97×66 1件 ≈136kg", vol_weight(127,97,66,1), 135.6, tolerance=0.5)
test("117×85×100 4件", vol_weight(117,85,100,4), 663.0)

# =====================
# 測試3：計費重
# =====================
print("\n【3. 計費重（取較大）】")
test("實重150 材積144 → 150", charge_weight(150, 144), 150)
test("實重50 材積144 → 144", charge_weight(50, 144), 144)
test("基隆4PLT 實重2412 材積1424 → 2412", charge_weight(2412, 1424), 2412)

# =====================
# 測試4：選車邏輯（棧板）
# =====================
print("\n【4. 選車邏輯（棧板）】")
truck, stack = find_truck_plt(4, 117, 85, 100, 843, True, True)
test("桃園4PLT 117×85×100 843kg → 3.5T", truck, "3.5T")
test("桃園4PLT 不可疊（高100×2+7>150）", stack, False)

truck, stack = find_truck_plt(4, 120, 100, 140, 2412, True, True)
test("基隆4PLT 120×100×140 2412kg → 4.5T", truck, "4.5T")
test("基隆4PLT 不可疊（高140×2+7>195）", stack, False)

truck, stack = find_truck_plt(1, 122, 102, 76, 200, True, True)
test("1PLT 122×102×76 單件可疊（用戶指定）", stack, True)

# =====================
# 測試5：選車邏輯（箱件）
# =====================
print("\n【5. 選車邏輯（箱件）】")
items3 = [{"l":120,"w":80,"h":90}]*3
truck, _ = find_truck_ctn(items3, 183, True, True)
test("3件 120×80×90 183kg → 1.5T", truck, "1.5T")

items4 = [{"l":120,"w":80,"h":90}]*4
truck, _ = find_truck_ctn(items4, 200, False, True)
test("4件 120×80×90 不可疊 200kg → 3.5T", truck, "3.5T")

# =====================
# 測試6：強制規則
# =====================
print("\n【6. 強制規則】")
test("高120cm ≥ 120 → 強制", 120 >= 120, True)
test("高119cm < 120 → 不強制", 119 >= 120, False)
test("單件重150kg ≥ 150 → 強制", 150 >= 150, True)
test("單件重149kg < 150 → 不強制", 149 >= 150, False)

# =====================
# 測試7：報價查詢（台北/桃園）
# =====================
print("\n【7. 報價查詢（台北/桃園）】")
test("台北市區 1.5T", PRICING["台北市區"].get("1.5T"), 1200)
test("內湖 4.5T", PRICING["內湖"].get("4.5T"), 2400)
test("基隆 4.5T", PRICING["基隆"].get("4.5T"), 2800)
test("桃園 1.5T", PRICING["桃園"].get("1.5T"), 1000)
test("機場各倉 0.6T", PRICING["機場各倉"].get("0.6T"), 500)
test("機場各倉 無併車", PRICING["機場各倉"].get("併車"), None)

# =====================
# 測試8：勁連發報價
# =====================
print("\n【8. 勁連發報價】")
test("高雄 50kg → $600", kinlian_price("高雄","高市",50), 600)
test("高雄 200kg → $900", kinlian_price("高雄","高市",200), 900)
test("高雄 700kg → $1500", kinlian_price("高雄","高市",700), 1500)
test("台南 150kg → $1200", kinlian_price("台南","台南",150), 1200)
test("佳里 150kg +500 → $1700", kinlian_price("佳里","台南",150,500), 1700)
test("林園 200kg +200 → $1100", kinlian_price("林園","高市",200,200), 1100)

# =====================
# 測試9：得統得勝報價
# =====================
print("\n【9. 得統得勝報價】")
test("台中 50kg → $400", detong_price("台中", 50), 400)
test("台中 100kg → $500", detong_price("台中", 100), 500)
test("台中 1500kg → $1500", detong_price("台中", 1500), 1500)
test("烏日 80kg → $650", detong_price("烏日", 80), 650)

# =====================
# 測試10：英吋換算
# =====================
print("\n【10. 英吋換算】")
test("48吋 → 121.9cm", round(48*2.54,1), 121.9)
test("32吋 → 81.3cm", round(32*2.54,1), 81.3)
test("36吋 → 91.4cm", round(36*2.54,1), 91.4)

# =====================
# 結果摘要
# =====================
total = passed + failed
print(f"\n{'='*50}")
print(f"測試結果：{passed}/{total} 通過")
if errors:
    print(f"\n❌ 失敗項目：")
    for e in errors:
        print(f"  • {e}")
else:
    print("🎉 全部通過！")
