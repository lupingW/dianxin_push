#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电信套餐用量监控 + 话费福利统计 合并脚本
功能：
  1. 查询流量、语音、余额、流量包详情
  2. 统计金豆兑换、等级权益、抽奖所得话费
  3. 生成 HTML 报告，通过 WxPusher / 青龙通知推送（含套餐详细内容）
环境变量：CHINATELECOM_ACCOUNT  格式：手机号#密码 (多账号用 & 或换行隔开)
可选：DINGTALK_WEBHOOK, DINGTALK_SECRET, ENABLE_RUISHU
"""

import os, sys, json, re, base64, random, ssl, calendar, logging, hmac, hashlib, time
from datetime import datetime, date
from collections import defaultdict
try:
    import aiohttp
except ImportError:
    os.system("pip3 install aiohttp -q")
    import aiohttp
try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5, DES3
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    os.system("pip3 install pycryptodome -q")
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5, DES3
    from Crypto.Util.Padding import pad, unpad
try:
    import execjs
except ImportError:
    os.system("pip3 install PyExecJS -q")
    import execjs
try:
    from dateutil import parser
except ImportError:
    os.system("pip3 install python-dateutil -q")
    from dateutil import parser
import asyncio

# -------------------- 配置区域 --------------------
logging.basicConfig(filename='telecom_script.log', level=logging.ERROR,
                    format='%(asctime)s - %(levelname)s - %(message)s')

CHINATELECOM_ACCOUNT = os.environ.get('CHINATELECOM_ACCOUNT', '')

DINGTALK_WEBHOOK = os.environ.get('DINGTALK_WEBHOOK', '')
DINGTALK_SECRET = os.environ.get('DINGTALK_SECRET', '')

ENABLE_RUISHU = os.environ.get('ENABLE_RUISHU', 'true').lower() == 'true'

CACHE_FILE = './telecom_cache.json'

# -------------------- 全局密钥 --------------------
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBkLT15ThVgz6/NOl6s8GNPofd
WzWbCkWnkaAm7O2LjkM1H7dMvzkiqdxU02jamGRHLX/ZNMCXHnPcW/sDhiFCBN18
qFvy8g6VYb9QtroI09e176s+ZCtiv7hbin2cCTj99iUpnEloZm19lwHyo69u5UMi
PMpq0/XKBO8lYhN/gwIDAQAB
-----END PUBLIC KEY-----"""

PARAM_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC+ugG5A8cZ3FqUKDwM57GM4io6
JGcStivT8UdGt67PEOihLZTw3P7371+N47PrmsCpnTRzbTgcupKtUv8ImZalYk65
dU8rjC/ridwhw9ffW2LBwvkEnDkkKKRi2liWIItDftJVBiWOh17o6gfbPoNrWORc
Adcbpk2L+udld5kZNwIDAQAB
-----END PUBLIC KEY-----"""

DES3_KEY = b'1234567`90koiuyhgtfrdews'
DES3_IV  = b'\x00' * 8

# -------------------- 工具函数 --------------------
def rsa_encrypt_base64(plain: str, key_pem: str = PUBLIC_KEY) -> str:
    key = RSA.import_key(key_pem)
    return base64.b64encode(PKCS1_v1_5.new(key).encrypt(plain.encode())).decode()

def rsa_encrypt_hex(plain: str, key_pem: str = PARAM_PUBLIC_KEY) -> str:
    public_key = RSA.import_key(key_pem)
    cipher = PKCS1_v1_5.new(public_key)
    plain_bytes = plain.encode('utf-8')
    max_block = public_key.size_in_bytes() - 11
    result = b''
    for i in range(0, len(plain_bytes), max_block):
        result += cipher.encrypt(plain_bytes[i:i+max_block])
    return result.hex()

def caesar_shift(text: str, offset: int = 2) -> str:
    return ''.join(chr(ord(c) + offset) for c in text)

def triple_des_encrypt(text: str) -> str:
    cipher = DES3.new(DES3_KEY, DES3.MODE_CBC, DES3_IV)
    return cipher.encrypt(pad(text.encode(), DES3.block_size)).hex()

def triple_des_decrypt(hex_str: str) -> str:
    cipher = DES3.new(DES3_KEY, DES3.MODE_CBC, DES3_IV)
    return unpad(cipher.decrypt(bytes.fromhex(hex_str)), DES3.block_size).decode()

def extract_amount(text: str) -> float:
    m = re.search(r'(\d+(?:\.\d+)?)元话费', text)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)', text)
    if m:
        val = float(m.group(1))
        return val if not (val > 100 and '.' not in m.group(1)) else 0.0
    return 0.0

def format_amount(val: float) -> str:
    if val == 0:
        return "0"
    if val == int(val):
        return f'<b style="color:red;">{int(val)}</b>'
    return f'<b style="color:red;">{val:.1f}</b>'

# -------------------- 瑞数反爬 --------------------
ruishu_engine = None
cookie_jar = {}

async def fetch_ruishu(session: aiohttp.ClientSession):
    global ruishu_engine, cookie_jar
    url = 'https://wapact.189.cn:9001/gateway/standExchange/detailNew/exchange'
    try:
        async with session.post(url) as resp:
            text = await resp.text()
            content = text.split(' content="')[2].split('" r=')[0]
            code1 = text.split('$_ts=window')[1].split('</script><script type="text/javascript"')[0]
            js_code = '$_ts=window' + code1
            js_url = text.split('$_ts.lcd();</script><script type="text/javascript" charset="utf-8" src="')[1].split('" r=')[0]
            rsurl = '/'.join(url.split('/')[:3]) + js_url
            async with session.get(rsurl) as res:
                js_code += await res.text()
            js_code = f"""
delete __filename; delete __dirname; ActiveXObject = undefined; window = global; content="{content}";
navigator = {{"platform": "Linux aarch64", "userAgent": "CtClient;11.0.0;Android;13;22081212C;NTIyMTcw!#!MTUzNzY"}};
location={{"href": "https://","origin": "","protocol": "","host": "","hostname": "","port": "","pathname": "","search": "","hash": ""}};
i = {{length: 0}}; base = {{length: 0}}; div = {{ getElementsByTagName: function (res) {{ if (res === 'i') {{ return i; }} return '<div></div>'; }} }};
script = {{}}; meta = [ {{charset:"UTF-8"}}, {{ content: content, getAttribute: function (res) {{ if (res === 'r') {{ return 'm'; }} }}, parentNode: {{ removeChild: function (res) {{ return content; }} }}, }} ];
form = '<form></form>'; window.addEventListener= function (res) {{}}; document = {{
createElement: function (res) {{ if (res === 'div') {{ return div; }} else if (res === 'form') {{ return form; }} else {{ return res; }} }},
addEventListener: function (res) {{}}, appendChild: function (res) {{ return res; }}, removeChild: function (res) {{}},
getElementsByTagName: function (res) {{ if (res === 'script') {{ return script; }} if (res === 'meta') {{ return meta; }} if (res === 'base') {{ return base; }} }},
getElementById: function (res) {{ if (res === 'root-hammerhead-shadow-ui') {{ return null; }} }}
}}; setInterval = function () {{}}; setTimeout = function () {{}}; window.top = window; {js_code};
function main() {{ cookie = document.cookie.split(';')[0]; return cookie; }}
"""
            ruishu_engine = execjs.compile(js_code)
            set_cookie = resp.headers['Set-Cookie'].split(';')[0].split('=')
            cookie_jar[set_cookie[0]] = set_cookie[1]
    except Exception as e:
        logging.error(f"瑞数环境初始化失败: {e}")

def get_ruishu_cookie() -> dict:
    global ruishu_engine, cookie_jar
    if ruishu_engine:
        try:
            new_cookie = ruishu_engine.call("main").split('=')
            cookie_jar[new_cookie[0]] = new_cookie[1]
        except:
            pass
    return cookie_jar

# -------------------- 缓存管理 --------------------
def load_cache():
    try:
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_cache(data: dict):
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# -------------------- 电信 API 类 --------------------
class TelecomAPI:
    def __init__(self, session: aiohttp.ClientSession, phone: str):
        self.sess = session
        self.phone = phone
        self.token = None
        self.userId = None
        self.ticket = None
        self.sign = None
        self.accId = None

    async def do_login(self, password: str) -> bool:
        cache = load_cache().get(self.phone, {})
        if cache.get('token') and cache.get('userId'):
            self.token = cache['token']
            self.userId = cache['userId']
            print(f"♻️ 复用缓存 token: {self.phone[:3]}****{self.phone[-4:]}")
            return True

        alphabet = 'abcdef0123456789'
        uid = [
            ''.join(random.choices(alphabet, k=8)),
            ''.join(random.choices(alphabet, k=4)),
            '4' + ''.join(random.choices(alphabet, k=3)),
            ''.join(random.choices(alphabet, k=4)),
            ''.join(random.choices(alphabet, k=12))
        ]
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        raw = f"iPhone 14 15.4.{uid[0]}{uid[1]}{self.phone}{ts}{password[:6]}0$$$0."
        body = {
            "headerInfos": {
                "code": "userLoginNormal",
                "timestamp": ts,
                "broadAccount": "",
                "broadToken": "",
                "clientType": "#11.3.0#channel50#iPhone 14 Pro Max#",
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "token": "",
                "userLoginName": caesar_shift(self.phone)
            },
            "content": {
                "attach": "test",
                "fieldData": {
                    "loginType": "4",
                    "accountType": "",
                    "loginAuthCipherAsymmertric": rsa_encrypt_base64(raw, PUBLIC_KEY),
                    "deviceUid": uid[0] + uid[1] + uid[2],
                    "phoneNum": caesar_shift(self.phone),
                    "isChinatelecom": "0",
                    "systemVersion": "15.4.0",
                    "authentication": caesar_shift(password)
                }
            }
        }
        try:
            async with self.sess.post(
                'https://appgologin.189.cn:9031/login/client/userLoginNormal',
                json=body
            ) as resp:
                data = await resp.json()
            login_data = data.get('responseData', {}).get('data', {}).get('loginSuccessResult')
            if login_data:
                self.token = login_data['token']
                self.userId = login_data['userId']
                cache_all = load_cache()
                cache_all[self.phone] = {'token': self.token, 'userId': self.userId}
                save_cache(cache_all)
                print(f"✅ 登录成功: {self.phone[:3]}****{self.phone[-4:]}")
                return True
            else:
                print(f"❌ 登录失败: {data.get('responseData', {}).get('resultMsg', '未知错误')}")
                return False
        except Exception as e:
            print(f"❌ 登录异常: {e}")
            return False

    async def get_ticket(self) -> bool:
        if not self.token or not self.userId:
            return False
        try:
            xml_data = f'''<Request><HeaderInfos><Code>getSingle</Code><Timestamp>{datetime.now().strftime("%Y%m%d%H%M%S")}</Timestamp><ClientType>#9.6.1#channel50#iPhone 14 Pro Max#</ClientType><Source>110003</Source><SourcePassword>Sid98s</SourcePassword><Token>{self.token}</Token><UserLoginName>{self.phone}</UserLoginName></HeaderInfos><Content><Attach>test</Attach><FieldData><TargetId>{triple_des_encrypt(self.userId)}</TargetId><Url>4a6862274835b451</Url></FieldData></Content></Request>'''
            headers = {
                'User-Agent': 'CtClient;10.4.1;Android;13;22081212C;NTQzNzgx!#!MTgwNTg5',
                'Content-Type': 'application/xml'
            }
            async with self.sess.post(
                'https://appgologin.189.cn:9031/map/clientXML',
                data=xml_data, headers=headers
            ) as resp:
                text = await resp.text()
            tk_match = re.search(r'<Ticket>(.*?)</Ticket>', text)
            if tk_match:
                self.ticket = triple_des_decrypt(tk_match.group(1))
                return True
            return False
        except Exception as e:
            logging.error(f"get_ticket error: {e}")
            return False

    async def login_for_bill(self) -> bool:
        if not self.ticket:
            return False
        try:
            cookies = get_ruishu_cookie() if ENABLE_RUISHU else {}
            async with self.sess.get(
                'https://wappark.189.cn/jt-sign/ssoHomLoginForBill',
                params={'ticket': self.ticket},
                cookies=cookies
            ) as resp:
                data = await resp.json()
            self.accId = data.get('accId')
            self.sign = data.get('sign')
            return self.accId is not None
        except Exception as e:
            logging.error(f"login_for_bill error: {e}")
            return False

    async def qry_important_data(self) -> dict:
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        body = {
            "content": {
                "fieldData": {
                    "provinceCode": "600101",
                    "cityCode": "8441900",
                    "shopId": "20002",
                    "isChinatelecom": "0",
                    "account": caesar_shift(self.phone),
                },
            },
            "headerInfos": {
                "code": "userFluxPackage",
                "clientType": "#11.3.0#channel50#iPhone 14 Pro Max#",
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": caesar_shift(self.phone),
                "token": self.token,
            },
        }
        async with self.sess.post(
            "https://appfuwu.189.cn:9021/query/qryImportantData",
            json=body
        ) as resp:
            return await resp.json()

    async def user_flux_package(self) -> dict:
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        body = {
            "content": {
                "fieldData": {
                    "queryFlag": "0",
                    "accessAuth": "1",
                    "account": caesar_shift(self.phone),
                },
            },
            "headerInfos": {
                "code": "userFluxPackage",
                "clientType": "#11.3.0#channel50#iPhone 14 Pro Max#",
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": caesar_shift(self.phone),
                "token": self.token,
            },
        }
        async with self.sess.post(
            "https://appfuwu.189.cn:9021/query/userFluxPackage",
            json=body
        ) as resp:
            return await resp.json()

    def parse_usage_summary(self, data: dict) -> dict:
        if not data:
            return {}
        fi = data.get("flowInfo", {})
        ta = fi.get("totalAmount") or {}
        flow_use = int(ta.get("used", 0))
        flow_bal = int(ta.get("balance", 0))
        flow_total = flow_use + flow_bal
        cf = fi.get("commonFlow") or {}
        common_use = int(cf.get("used", 0))
        common_bal = int(cf.get("balance", 0))
        common_total = common_use + common_bal
        sa = fi.get("specialAmount") or {}
        special_use = int(sa.get("used", 0))
        special_bal = int(sa.get("balance", 0))
        special_total = special_use + special_bal
        voice = data.get("voiceInfo", {}).get("voiceDataInfo") or {}
        voice_used = int(voice.get("used", 0))
        voice_bal = int(voice.get("balance", 0))
        voice_total = int(voice.get("total", 0))
        bal_info = data.get("balanceInfo", {}).get("indexBalanceDataInfo") or {}
        balance = int(float(bal_info.get("balance", 0)) * 100)
        flow_items = []
        for item in fi.get("flowList", []):
            if "流量" not in item.get("title", ""):
                continue
            try:
                if "已用" in item.get("leftTitle","") and "剩余" in item.get("rightTitle",""):
                    use = self._conv(item["leftTitleHh"])
                    bal = self._conv(item["rightTitleHh"])
                    total = use + bal
                elif "超出" in item.get("leftTitle","") and "/" in item.get("rightTitleEnd",""):
                    bal = -self._conv(item["leftTitleHh"])
                    use = self._conv(item["rightTitleEnd"].split("/")[1]) - bal
                    total = use + bal
                elif "已用" in item.get("leftTitle","") and "降速" in item.get("rightTitle",""):
                    total = self._conv(re.search(r"(\d+[KMGT]B)", item["rightTitle"]).group(1))
                    use = self._conv(item["leftTitleHh"])
                    bal = total - use
                else:
                    continue
                flow_items.append({"name": item["title"], "use": use, "balance": bal, "total": total})
            except:
                continue
        return {
            "phone": self.phone,
            "balance": balance,
            "voiceUsage": voice_used,
            "voiceBalance": voice_bal,
            "voiceTotal": voice_total,
            "flowUse": flow_use,
            "flowTotal": flow_total,
            "flowOver": int(ta.get("over", 0)),
            "commonUse": common_use,
            "commonTotal": common_total,
            "commonOver": int(cf.get("over", 0)),
            "specialUse": special_use,
            "specialTotal": special_total,
            "createTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "flowItems": flow_items,
            "fluxDetail": ""  # 额外字段，后期填充
        }

    def _conv(self, s):
        if not s:
            return 0
        if isinstance(s, str):
            v, u = float(s[:-2]), s[-2:]
        else:
            v, u = s, "KB"
        unit_map = {"KB":1, "MB":1024, "GB":1024**2, "TB":1024**3}
        return int(v * unit_map.get(u, 1) / unit_map["KB"])

    async def get_coin_records(self) -> list:
        if not self.sign or not self.accId:
            return []
        params = {'accId': self.accId, 'page': 0, 'size': 150}
        data = rsa_encrypt_hex(json.dumps(params), PARAM_PUBLIC_KEY)
        try:
            cookies = get_ruishu_cookie()
            async with self.sess.post(
                'https://wappark.189.cn/jt-sign/paradise/getCoinMallExchangetRecords',
                json={'para': data},
                headers={'sign': self.sign},
                cookies=cookies
            ) as resp:
                return (await resp.json()).get('data', [])
        except:
            return []

    async def get_rights_records(self) -> list:
        if not self.sign or not self.accId:
            return []
        params = {'accId': self.accId, 'page': 0, 'size': 150}
        data = rsa_encrypt_hex(json.dumps(params), PARAM_PUBLIC_KEY)
        try:
            cookies = get_ruishu_cookie()
            async with self.sess.post(
                'https://wappark.189.cn/jt-sign/paradise/getRightsExchangetRecords',
                json={'para': data},
                headers={'sign': self.sign},
                cookies=cookies
            ) as resp:
                return (await resp.json()).get('data', [])
        except:
            return []

    async def get_prize_records(self) -> list:
        if not self.sign or not self.accId:
            return []
        params = {'phone': self.accId, 'page': 0, 'size': 150}
        data = rsa_encrypt_hex(json.dumps(params), PARAM_PUBLIC_KEY)
        try:
            cookies = get_ruishu_cookie()
            async with self.sess.post(
                'https://wappark.189.cn/jt-sign/webSign/getPrizeRecords',
                json={'para': data},
                headers={'sign': self.sign},
                cookies=cookies
            ) as resp:
                return (await resp.json()).get('data', [])
        except:
            return []

# -------------------- 全局存储 --------------------
today = date.today()
current_month = today.month
current_year = today.year
month_days = calendar.monthrange(current_year, current_month)[1]

daily_summary = [[] for _ in range(month_days)]
phone_summary = {}
USER_AMOUNT_INFO = defaultdict(lambda: {"exchange": 0, "prize": 0, "rights": 0})
TODAY_AMOUNT_INFO = {"exchange": 0, "prize": 0, "rights": 0}
TODAY_WINNING_RECORDS = []
MONTH_WINNING_RECORDS = []
all_accounts_msg = []

def add_benefit_record(time_str, amount_str, phone, record_type):
    try:
        parsed_time = parser.parse(time_str)
    except:
        return
    if not isinstance(parsed_time, datetime):
        parsed_time = datetime.combine(parsed_time, datetime.min.time())
    if parsed_time.year != current_year or parsed_time.month != current_month:
        return
    amount = extract_amount(amount_str)
    if amount == 0:
        return
    record = [parsed_time, amount_str, phone, record_type]
    day_idx = parsed_time.day - 1
    if 0 <= day_idx < len(daily_summary):
        daily_summary[day_idx].append(record)
    phone_summary.setdefault(phone, []).append(record)

    USER_AMOUNT_INFO[phone]["exchange"] += amount if record_type == "金豆兑换" else 0
    USER_AMOUNT_INFO[phone]["rights"]   += amount if record_type == "等级权益" else 0
    USER_AMOUNT_INFO[phone]["prize"]    += amount if record_type == "各种抽奖" else 0

    MONTH_WINNING_RECORDS.append({
        "time": parsed_time.strftime('%Y-%m-%d %H:%M'),
        "phone": phone,
        "amount": amount_str,
        "type": record_type
    })
    if parsed_time.day == today.day:
        TODAY_WINNING_RECORDS.append({
            "time": parsed_time.strftime('%H:%M'),
            "phone": phone,
            "amount": amount_str,
            "type": record_type
        })
        if record_type == "各种抽奖":
            TODAY_AMOUNT_INFO["prize"] += amount
        elif record_type == "等级权益":
            TODAY_AMOUNT_INFO["rights"] += amount
        elif record_type == "金豆兑换":
            TODAY_AMOUNT_INFO["exchange"] += amount

# -------------------- HTML 报告与推送 --------------------
def generate_html_report(usage_summaries: list) -> str:
    total_exchange = sum(u["exchange"] for u in USER_AMOUNT_INFO.values())
    total_prize    = sum(u["prize"] for u in USER_AMOUNT_INFO.values())
    total_rights   = sum(u["rights"] for u in USER_AMOUNT_INFO.values())
    total_month    = total_exchange + total_prize + total_rights
    total_today    = TODAY_AMOUNT_INFO["exchange"] + TODAY_AMOUNT_INFO["prize"] + TODAY_AMOUNT_INFO["rights"]

    # 套餐基本用量表格
    usage_rows = ""
    for s in usage_summaries:
        mask = f"{s['phone'][:3]}****{s['phone'][-4:]}"
        vp = s['voiceUsage']/s['voiceTotal']*100 if s['voiceTotal'] else 0
        tp = s['flowUse']/s['flowTotal']*100 if s['flowTotal'] else 0
        usage_rows += f"""
        <tr>
          <td>{mask}</td>
          <td>{s['balance']/100:.2f}元</td>
          <td>{s['voiceUsage']}/{s['voiceTotal']}分 ({vp:.0f}%)</td>
          <td>{s['flowUse']/1024:.1f}/{s['flowTotal']/1024:.1f}MB ({tp:.0f}%)</td>
        </tr>"""

    # 流量包明细表格（每个账号一个区块）
    flux_detail_html = ""
    for s in usage_summaries:
        if s.get('fluxDetail'):
            mask = f"{s['phone'][:3]}****{s['phone'][-4:]}"
            flux_detail_html += f"<div style='margin:5px 0;font-size:12px;font-weight:bold;'>{mask} 流量包明细：</div>"
            flux_detail_html += f"<div style='font-size:12px;line-height:1.6;padding-left:10px;'>{s['fluxDetail'].replace(chr(10), '<br>')}</div>"

    # 今日中奖记录
    today_win_rows = ""
    if TODAY_WINNING_RECORDS:
        for r in sorted(TODAY_WINNING_RECORDS, key=lambda x: x['time']):
            mask = f"{r['phone'][:3]}****{r['phone'][-4:]}"
            today_win_rows += f"<tr><td>{r['time']}</td><td>{mask}</td><td>{r['amount']}</td><td>{r['type']}</td></tr>"
    else:
        today_win_rows = '<tr><td colspan="4">今日暂无</td></tr>'

    # 本月中奖记录
    month_win_rows = ""
    if MONTH_WINNING_RECORDS:
        for r in sorted(MONTH_WINNING_RECORDS, key=lambda x: x['time']):
            mask = f"{r['phone'][:3]}****{r['phone'][-4:]}"
            month_win_rows += f"<tr><td>{r['time']}</td><td>{mask}</td><td>{r['amount']}</td><td>{r['type']}</td></tr>"
    else:
        month_win_rows = '<tr><td colspan="4">本月暂无</td></tr>'

    html = f"""
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<div style='text-align:center;font-size:16px;font-weight:bold;padding:8px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;margin-bottom:8px;border-radius:6px;'>
  {current_year}年{current_month}月电信监控报告
</div>

<div style='margin:5px;padding:8px;background:#f8f9fa;border-radius:6px;'>
  <div style='font-weight:bold;margin-bottom:4px;'>📱 套餐用量</div>
  <table border="1" width="100%" cellspacing="0" style="font-size:12px;text-align:center;border-collapse:collapse;">
    <tr style="background:#e9ecef;"><th>号码</th><th>余额</th><th>语音</th><th>总流量</th></tr>
    {usage_rows}
  </table>
  {flux_detail_html}
</div>

<div style='margin:5px;padding:8px;background:linear-gradient(135deg,#43e97b 0%,#38f9d7 100%);color:white;border-radius:6px;'>
  <div style='text-align:center;font-weight:bold;'>{today.month}月{today.day}日统计</div>
  <div style='display:flex;flex-wrap:wrap;justify-content:space-around;margin-top:5px;'>
    <div><span>金豆</span><br><b>{TODAY_AMOUNT_INFO['exchange']:.1f}</b></div>
    <div><span>抽奖</span><br><b>{TODAY_AMOUNT_INFO['prize']:.1f}</b></div>
    <div><span>权益</span><br><b>{TODAY_AMOUNT_INFO['rights']:.1f}</b></div>
    <div style="border-left:1px solid white;padding-left:8px;"><span>今日总计</span><br><b>{total_today:.1f}</b></div>
  </div>
</div>

<div style='margin:5px;'>
  <div style='font-weight:bold;margin-bottom:3px;'>🎁 今日中奖明细</div>
  <table border="1" width="100%" cellspacing="0" style="font-size:12px;text-align:center;border-collapse:collapse;">
    <tr style="background:#43e97b;color:white;"><th>时间</th><th>号码</th><th>金额</th><th>类型</th></tr>
    {today_win_rows}
  </table>
</div>

<div style='margin:5px;padding:8px;background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%);color:white;border-radius:6px;'>
  <div style='text-align:center;font-weight:bold;'>本月累计统计</div>
  <div style='display:flex;flex-wrap:wrap;justify-content:space-around;margin-top:5px;'>
    <div><span>金豆</span><br><b>{total_exchange:.1f}</b></div>
    <div><span>抽奖</span><br><b>{total_prize:.1f}</b></div>
    <div><span>权益</span><br><b>{total_rights:.1f}</b></div>
    <div style="border-left:1px solid white;padding-left:8px;"><span>本月总计</span><br><b>{total_month:.1f}</b></div>
  </div>
</div>

<div style='margin:5px;'>
  <div style='font-weight:bold;margin-bottom:3px;'>📅 本月中奖明细</div>
  <table border="1" width="100%" cellspacing="0" style="font-size:12px;text-align:center;border-collapse:collapse;">
    <tr style="background:#4facfe;color:white;"><th>时间</th><th>号码</th><th>金额</th><th>类型</th></tr>
    {month_win_rows}
  </table>
</div>
"""
    return html.replace('\n', '')

def generate_dingtalk_sign(secret: str) -> dict:
    """生成钉钉机器人签名"""
    timestamp = str(round(time.time() * 1000))
    secret_enc = secret.encode('utf-8')
    string_to_sign = f'{timestamp}\n{secret}'
    string_to_sign_enc = string_to_sign.encode('utf-8')
    hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote(base64.b64encode(hmac_code))
    return {
        'timestamp': timestamp,
        'sign': sign
    }

async def send_dingtalk(html_content: str):
    if not DINGTALK_WEBHOOK:
        print("⚠️ 未配置钉钉机器人 Webhook，跳过钉钉推送")
        return
    import urllib.parse
    import requests
    
    # 构建 URL
    url = DINGTALK_WEBHOOK
    params = {}
    
    # 如果配置了密钥，添加签名
    if DINGTALK_SECRET:
        sign_data = generate_dingtalk_sign(DINGTALK_SECRET)
        params['timestamp'] = sign_data['timestamp']
        params['sign'] = sign_data['sign']
    
    # 钉钉 Markdown 消息格式
    markdown_text = f"""### {current_year}年{current_month}月电信监控报告

#### 📱 套餐用量"""
    
    # 添加套餐用量数据
    total_exchange = sum(u["exchange"] for u in USER_AMOUNT_INFO.values())
    total_prize = sum(u["prize"] for u in USER_AMOUNT_INFO.values())
    total_rights = sum(u["rights"] for u in USER_AMOUNT_INFO.values())
    total_month = total_exchange + total_prize + total_rights
    total_today = TODAY_AMOUNT_INFO["exchange"] + TODAY_AMOUNT_INFO["prize"] + TODAY_AMOUNT_INFO["rights"]
    
    # 提取 HTML 中的表格数据转为 Markdown
    from html.parser import HTMLParser
    
    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_table = False
            self.in_row = False
            self.in_cell = False
            self.rows = []
            self.current_row = []
            self.current_cell = ""
            self.headers = []
            self.first_row = True
            
        def handle_starttag(self, tag, attrs):
            if tag == 'table':
                self.in_table = True
            elif tag == 'tr' and self.in_table:
                self.in_row = True
                self.current_row = []
            elif tag in ['td', 'th'] and self.in_row:
                self.in_cell = True
                self.current_cell = ""
                
        def handle_endtag(self, tag):
            if tag == 'table':
                self.in_table = False
            elif tag == 'tr' and self.in_table:
                self.in_row = False
                if self.first_row:
                    self.headers = self.current_row
                    self.first_row = False
                else:
                    self.rows.append(self.current_row)
            elif tag in ['td', 'th'] and self.in_row:
                self.in_cell = False
                self.current_row.append(self.current_cell.strip())
                
        def handle_data(self, data):
            if self.in_cell:
                self.current_cell += data
    
    # 解析 HTML 提取表格数据
    parser = TableParser()
    parser.feed(html_content)
    
    # 生成 Markdown 表格
    if parser.headers:
        markdown_text += "\n\n|" + "|".join(parser.headers) + "|\n"
        markdown_text += "|" + "|".join(["---"] * len(parser.headers)) + "|\n"
        for row in parser.rows[:10]:  # 限制显示前10行
            markdown_text += "|" + "|".join(row) + "|\n"
    
    # 添加福利统计
    markdown_text += f"""
#### 📊 今日统计
- 金豆兑换: {TODAY_AMOUNT_INFO['exchange']:.1f}元
- 各种抽奖: {TODAY_AMOUNT_INFO['prize']:.1f}元  
- 等级权益: {TODAY_AMOUNT_INFO['rights']:.1f}元
- **今日总计: {total_today:.1f}元**

#### 🎁 本月累计
- 金豆兑换: {total_exchange:.1f}元
- 各种抽奖: {total_prize:.1f}元
- 等级权益: {total_rights:.1f}元
- **本月总计: {total_month:.1f}元**

#### 📅 本月中奖明细
"""
    # 添加中奖记录
    if MONTH_WINNING_RECORDS:
        for r in sorted(MONTH_WINNING_RECORDS, key=lambda x: x['time'])[:15]:  # 限制显示前15条
            mask = f"{r['phone'][:3]}****{r['phone'][-4:]}"
            markdown_text += f"- {r['time']} | {mask} | {r['amount']} | {r['type']}\n"
    else:
        markdown_text += "本月暂无中奖记录\n"
    
    # 构建请求数据
    data = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"{current_year}年{current_month}月电信报告",
            "text": markdown_text
        }
    }
    
    try:
        if params:
            response = requests.post(url, params=params, json=data, headers={'Content-Type': 'application/json'})
        else:
            response = requests.post(url, json=data, headers={'Content-Type': 'application/json'})
        
        result = response.json()
        if result.get('errcode') == 0:
            print("✅ 钉钉推送成功")
        else:
            print(f"⚠️ 钉钉推送失败: {result.get('errmsg')}")
    except Exception as e:
        print(f"⚠️ 钉钉推送异常: {e}")

def qinglong_notify():
    if not all_accounts_msg:
        return
    try:
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from notify import send
        send("电信套餐+福利统计", "\n\n".join(all_accounts_msg))
        print("📢 青龙通知发送成功")
    except Exception as e:
        print(f"⚠️ 青龙通知发送失败: {e}")

# -------------------- 主流程 --------------------
async def process_user(session, phone, password):
    api = TelecomAPI(session, phone)
    # 登录
    if not await api.do_login(password):
        return None

    # 并发执行套餐查询与福利前置任务
    usage_task = asyncio.create_task(api.qry_important_data())
    ticket_task = asyncio.create_task(api.get_ticket())
    imp = await usage_task
    ticket_ok = await ticket_task

    usage_summary = None
    if imp and imp.get('responseData'):
        usage_summary = api.parse_usage_summary(imp['responseData']['data'])
    else:
        print(f"⚠️ 套餐查询失败，尝试重新登录...")
        if await api.do_login(password):
            imp = await api.qry_important_data()
            if imp and imp.get('responseData'):
                usage_summary = api.parse_usage_summary(imp['responseData']['data'])

    # 输出并收集流量包详情
    flux_detail_str = ""
    if usage_summary:
        s = usage_summary
        vp = s['voiceUsage']/s['voiceTotal']*100 if s['voiceTotal'] else 0
        tp = s['flowUse']/s['flowTotal']*100 if s['flowTotal'] else 0
        print(f"💰 余额: {s['balance']/100:.2f}元")
        print(f"📞 语音: {s['voiceUsage']}分/{s['voiceTotal']}分 ({vp:.0f}%)")
        print(f"📊 总流量: {s['flowUse']/1024:.1f}MB/{s['flowTotal']/1024:.1f}MB ({tp:.0f}%)")

        # 获取流量包详情
        try:
            pkg = await api.user_flux_package()
            if pkg and pkg.get('responseData'):
                packages = pkg['responseData']['data']['productOFFRatable']['ratableResourcePackages']
                lines = []
                for p in packages:
                    icon = "🇨🇳" if "国内" in p["title"] else "📺" if "专用" in p["title"] else "🌎"
                    line = f"{icon}{p['title']}: "
                    for prod in p["productInfos"]:
                        if prod.get("infiniteTitle"):
                            line += f"[{prod['title']}]{prod['infiniteTitle']}{prod['infiniteValue']}{prod['infiniteUnit']}/无限 "
                        else:
                            line += f"[{prod['title']}]{prod['leftTitle']}{prod['leftHighlight']}{prod['rightCommon']} "
                    print(line.strip())
                    lines.append(line.strip())
                flux_detail_str = "\n".join(lines)
                usage_summary['fluxDetail'] = flux_detail_str  # 存入供 HTML 使用
        except:
            pass
    else:
        print("套餐用量查询未返回有效数据")

    # 福利查询
    if ticket_ok and await api.login_for_bill():
        coin, rights, prize = await asyncio.gather(
            api.get_coin_records(),
            api.get_rights_records(),
            api.get_prize_records()
        )
        records_output = []
        for item in coin:
            if "话费" in item.get("title", ""):
                add_benefit_record(item.get("createdDate"), item.get("title"), phone, "金豆兑换")
                records_output.append((item.get("createdDate"), item.get("title"), "金豆兑换"))
        for item in rights:
            if "话费" in item.get("title", ""):
                add_benefit_record(item.get("createdDate"), item.get("title"), phone, "等级权益")
                records_output.append((item.get("createdDate"), item.get("title"), "等级权益"))
        for item in prize:
            if "话费" in item.get("winTitle", ""):
                add_benefit_record(item.get("createdDate"), item.get("winTitle"), phone, "各种抽奖")
                records_output.append((item.get("createdDate"), item.get("winTitle"), "各种抽奖"))

        if records_output:
            print("🎁 话费福利记录:")
            records_output.sort(key=lambda x: x[0])
            current_day = None
            for time_str, amount_str, rtype in records_output:
                try:
                    t = parser.parse(time_str)
                    day_label = f"{t.month}月{t.day}日"
                    if day_label != current_day:
                        current_day = day_label
                        print(f"  {day_label}")
                    time_part = t.strftime('%H:%M')
                    mask = f"{phone[:3]}****{phone[-4:]}"
                    print(f"  {time_part} | {mask} | {amount_str}")
                except:
                    pass
        else:
            print("🎁 本月话费福利：无记录")
    else:
        print("⚠️ 福利查询登录失败，跳过话费福利统计")

    # 构造发送给青龙通知的详细文本（包含套餐详情和福利汇总）
    if usage_summary:
        s = usage_summary
        vp = s['voiceUsage']/s['voiceTotal']*100 if s['voiceTotal'] else 0
        tp = s['flowUse']/s['flowTotal']*100 if s['flowTotal'] else 0
        msg = f"📱 {phone[:3]}****{phone[-4:]} 套餐\n"
        msg += f"💰余额: {s['balance']/100:.2f}元 | 语音: {s['voiceUsage']}/{s['voiceTotal']}分({vp:.0f}%) | 总流量: {s['flowUse']/1024:.1f}/{s['flowTotal']/1024:.1f}MB({tp:.0f}%)"
        if flux_detail_str:
            msg += f"\n流量包明细:\n{flux_detail_str}"
    else:
        msg = f"📱 {phone[:3]}****{phone[-4:]} 套餐查询失败"

    welfare_data = USER_AMOUNT_INFO[phone]
    total_w = welfare_data['exchange'] + welfare_data['prize'] + welfare_data['rights']
    msg += f"\n🎁 本月话费福利: {total_w:.2f}元 (金豆{welfare_data['exchange']:.2f} 抽奖{welfare_data['prize']:.2f} 权益{welfare_data['rights']:.2f})"
    all_accounts_msg.append(msg)

    return usage_summary

async def main():
    if not CHINATELECOM_ACCOUNT:
        print("未配置 CHINATELECOM_ACCOUNT 环境变量")
        return

    accounts = []
    for part in CHINATELECOM_ACCOUNT.replace('&', '\n').split('\n'):
        p = part.strip()
        if '#' in p:
            phone, pwd = p.split('#', 1)
            accounts.append((phone.strip(), pwd.strip()))
    if not accounts:
        print("无有效账号")
        return

    ssl_context = ssl.create_default_context()
    ssl_context.set_ciphers('DEFAULT@SECLEVEL=1')
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; 22081212C) AppleWebKit/537.36"
    }
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context),
                                     headers=headers) as session:
        if ENABLE_RUISHU:
            print("⏳ 初始化瑞数环境...")
            await fetch_ruishu(session)

        usage_summaries = []
        for idx, (phone, pwd) in enumerate(accounts):
            print(f"\n--- 处理第 {idx+1}/{len(accounts)} 个账号: {phone[:3]}****{phone[-4:]} ---")
            res = await process_user(session, phone, pwd)
            if res:
                usage_summaries.append(res)
            if idx < len(accounts) - 1:
                wait = random.uniform(1, 3)
                await asyncio.sleep(wait)

        print("\n📊 生成综合报告...")
        html = generate_html_report(usage_summaries)
        await send_dingtalk(html)
        qinglong_notify()

if __name__ == '__main__':
    asyncio.run(main())