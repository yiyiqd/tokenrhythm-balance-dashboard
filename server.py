#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基元律动 TokenRhythm 余额看板 - 轻量后端
纯标准库实现,无第三方依赖。数据存本地文件,前端只拿余额等展示字段,不接触凭据明文。
"""
import json
import os
import re
import time
import hashlib
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.txt")   # 每行: 备注名 sess_token [tr_ref_device] (# 开头为注释)
DATA_FILE = os.path.join(BASE_DIR, "data.json")          # 查询结果缓存
KEYS_FILE = os.path.join(BASE_DIR, "keys_full.json")     # 本地保存的完整 Key: {key_id: full_key}
PORT = 9155
API_BASE = "https://tokenrhythm.studio"
REFRESH_INTERVAL = 1800       # 后台自动刷新间隔(秒)
MAX_WORKERS = 10              # 并发查询上限
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

_lock = threading.Lock()
_refreshing = {"running": False, "started_at": None, "finished_at": None, "msg": ""}


# ---------- 账号文件 ----------

def load_accounts():
    """解析 accounts.txt。兼容格式:
      备注名 sess_xxx ref_yyy | sess_xxx | sess_xxx,ref_yyy | tr_session=xxx; tr_ref_device=yyy(整行cookie)"""
    accounts = []
    if not os.path.exists(ACCOUNTS_FILE):
        return accounts
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            a = parse_account_line(line)
            if a:
                accounts.append(a)
    return accounts


def parse_account_line(line):
    note, token, ref = "", "", ""
    # 整行 Cookie 形态: tr_session=xxx; tr_ref_device=yyy
    m = re.search(r"tr_session=([A-Za-z0-9_\-]+)", line)
    if m:
        token = m.group(1)
        m2 = re.search(r"tr_ref_device=([A-Za-z0-9_\-]+)", line)
        if m2:
            ref = m2.group(1)
        parts = [p.strip() for p in re.split(r"[;\s]+", line) if p.strip()]
        for p in parts:
            if p.startswith("sess_") and not token:
                token = p
            elif p.startswith("tr_ref_device="):
                ref = p.split("=", 1)[1]
    else:
        parts = re.split(r"[\s,]+", line)
        if parts and parts[0].startswith("sess_"):
            token = parts[0]
            ref = parts[1] if len(parts) > 1 and not parts[1].startswith("sess_") else ""
        elif len(parts) >= 2 and parts[1].startswith("sess_"):
            note = parts[0]
            token = parts[1]
            ref = parts[2] if len(parts) > 2 else ""
    if not token.startswith("sess_"):
        return None
    if note and (note.startswith("tr_") or "=" in note):
        note = ""
    return {"note": note, "token": token, "ref": ref}


def parse_import_text(text):
    """解析批量导入文本。支持:
      1) Netscape cookie 文件格式(tab 分隔, F12 导出): 同组 session/ref 按出现顺序配对
      2) 整行 Cookie: tr_session=xxx; tr_ref_device=yyy
      3) 简单格式: 备注名 sess_xxx ref_yyy / 仅 sess_xxx
    返回 [{note, token, ref}]"""
    sessions, refs = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("\t")]
        # Netscape 格式行: 域名 \t 标志 \t 路径 \t secure \t 过期 \t 键 \t 值
        if len(fields) >= 7 and fields[5] in ("tr_session", "tr_ref_device"):
            key, val = fields[5], fields[6]
            if key == "tr_session" and val.startswith("sess_"):
                sessions.append({"note": "", "token": val})
            elif key == "tr_ref_device" and val:
                refs.append(val)
            continue
        a = parse_account_line(line)
        if a:
            if a["token"]:
                sessions.append({"note": a["note"], "token": a["token"]})
            if a["ref"]:
                refs.append(a["ref"])
    accounts = []
    for i, s in enumerate(sessions):
        ref = refs[i] if i < len(refs) else ""
        accounts.append({"note": s["note"], "token": s["token"], "ref": ref})
    # 无法识别的行数 = 非空/非注释行中, 既不是 Netscape 关键行也解析不出账号的行
    bad = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("\t")]
        if len(fields) >= 7 and fields[5] in ("tr_session", "tr_ref_device", "tr_csrf", "_c_WBKFRo"):
            continue  # 属于 cookie 文件里的无关行(CSRF/随机饼干等), 不算错误
        if not parse_account_line(line):
            bad += 1
    return accounts, bad


def save_accounts(accounts):
    lines = ["# 基元律动账号凭据: 每行 = 备注名 sess_token [tr_ref_device]"]
    for a in accounts:
        parts = [a["note"], a["token"], a["ref"]]
        lines.append(" ".join(p for p in parts if p))
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ACCOUNTS_FILE)
    os.chmod(ACCOUNTS_FILE, 0o600)


# ---------- 上游 API ----------

def _num(v):
    """上游金额字段可能是字符串或数字, 统一转 float; 空值保持 None"""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def api_get(path, token, ref):
    return api_call(path, token, ref)


def api_call(path, token, ref, body=None, method=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Cookie": "tr_ref_device=%s; tr_session=%s" % (ref, token),
        "User-Agent": UA,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, data=data, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def find_account(aid):
    for a in load_accounts():
        if hashlib.sha256(a["token"].encode()).hexdigest()[:12] == aid:
            return a
    return None


def load_full_keys():
    if not os.path.exists(KEYS_FILE):
        return {}
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_full_keys(d):
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEYS_FILE)
    os.chmod(KEYS_FILE, 0o600)


def fetch_keys(account):
    """列出账号的 API Keys, 若本地保存过完整 Key 则附上 fullKey"""
    try:
        keys = api_get("/api/api-keys", account["token"], account["ref"])
        if keys.get("code") != 0:
            return {"ok": False, "error": str(keys.get("code"))}
        full = load_full_keys()
        out = []
        for k in keys.get("data") or []:
            k = dict(k)
            if k.get("id") in full:
                k["fullKey"] = full[k["id"]]
            out.append(k)
        return {"ok": True, "keys": out}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def create_key(account, name):
    """创建 API Key, 完整 Key 只返回一次; 自动存本地便于以后显示"""
    try:
        r = api_call("/api/api-keys", account["token"], account["ref"], {"name": name})
        if r.get("code") != 0:
            return {"ok": False, "error": str(r.get("code"))}
        d = r["data"]
        full = load_full_keys()
        if d.get("id") and d.get("key"):
            full[d["id"]] = d["key"]
            save_full_keys(full)
        return {"ok": True, "key": d.get("key"), "id": d.get("id"),
                "name": d.get("name"), "maskedKey": d.get("maskedKey")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def delete_key(account, key_id):
    """删除 API Key: POST .../<id>/delete, 必须带 {} body 和 Content-Type"""
    try:
        r = api_call("/api/api-keys/%s/delete" % key_id, account["token"], account["ref"], {})
        if r.get("code") != 0:
            return {"ok": False, "error": str(r.get("code"))}
        return {"ok": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %d" % e.code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def fetch_one(account):
    """查询单个账号: 余额 + 账号名。失败时 ok=False 并记录原因。"""
    token, ref = account["token"], account["ref"]
    res = {
        "id": hashlib.sha256(token.encode()).hexdigest()[:12],
        "note": account["note"],
        "name": "",
        "phone": "",
        "joined_at": "",
        "ok": False,
        "error": "",
        "checked_at": None,
        "balance": None, "cost": None,
        "calls": None, "success_calls": None,
        "input_tokens": None, "output_tokens": None,
        "expiry": None,
    }
    try:
        usage = api_get("/api/usage-summary", token, ref)
        if usage.get("code") != 0:
            raise Exception("接口错误: %s" % usage.get("code"))
        d = usage["data"]
        res["balance"] = _num(d.get("availableBalanceCny"))
        res["cost"] = _num(d.get("costCny"))
        res["calls"] = d.get("calls")
        res["success_calls"] = d.get("successCalls")
        res["input_tokens"] = d.get("inputTokens")
        res["output_tokens"] = d.get("outputTokens")
        res["expiry"] = d.get("nextExpiryAt")
        try:
            me = api_get("/api/me", token, ref)
            if me.get("code") == 0 and isinstance(me.get("data"), dict):
                res["name"] = me["data"].get("name") or ""
                res["phone"] = me["data"].get("phoneMasked") or ""
                res["joined_at"] = me["data"].get("joinedAt") or ""
        except Exception:
            pass
        res["ok"] = True
    except urllib.error.HTTPError as e:
        res["error"] = "HTTP %d" % e.code
        if e.code == 401:
            res["error"] = "凭据失效(401), 需重新获取"
    except Exception as e:
        res["error"] = str(e)[:80]
    res["checked_at"] = int(time.time())
    return res


def fetch_models(account):
    """查询单个账号的按模型消耗分布: 列出所有 API Key, 逐个取 usage-panel 的 byModel 聚合。
    返回 [{model, totalTokens, costCny, calls}]"""
    token, ref = account["token"], account["ref"]
    try:
        keys = api_get("/api/api-keys", token, ref)
        if keys.get("code") != 0:
            return []
        key_list = keys.get("data") or []
        models = {}
        for k in key_list:
            kid = k.get("id")
            if not kid:
                continue
            up = api_get("/api/api-keys/%s/usage-panel?range=all&pageSize=1" % kid, token, ref)
            if up.get("code") != 0:
                continue
            for m in (up.get("data") or {}).get("byModel") or []:
                mid = m.get("modelId") or m.get("model") or "未知"
                if mid not in models:
                    models[mid] = {"model": m.get("model") or mid, "totalTokens": 0, "costCny": 0.0, "calls": 0}
                models[mid]["totalTokens"] += int(m.get("totalTokens") or 0)
                models[mid]["costCny"] += _num(m.get("costCny")) or 0.0
                models[mid]["calls"] += int(m.get("calls") or 0)
        return list(models.values())
    except Exception:
        return []


def refresh_all():
    with _lock:
        if _refreshing["running"]:
            return {"error": "刷新进行中"}
        _refreshing["running"] = True
        _refreshing["started_at"] = int(time.time())
        _refreshing["msg"] = ""
    try:
        accounts = load_accounts()
        results = []
        n = min(MAX_WORKERS, max(1, len(accounts)))
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(fetch_one, a) for a in accounts]
            for fut in as_completed(futs):
                results.append(fut.result())
        results.sort(key=lambda r: (r["note"] or "", r["name"] or ""))
        # 跨账号聚合按模型消耗分布(模型排行)
        model_agg = {}
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(fetch_models, a) for a in accounts]
            for fut in as_completed(futs):
                for m in fut.result():
                    mid = m["model"]
                    if mid not in model_agg:
                        model_agg[mid] = {"model": mid, "totalTokens": 0, "costCny": 0.0, "calls": 0}
                    model_agg[mid]["totalTokens"] += m["totalTokens"]
                    model_agg[mid]["costCny"] += m["costCny"]
                    model_agg[mid]["calls"] += m["calls"]
        models = sorted(model_agg.values(), key=lambda m: m["totalTokens"], reverse=True)
        data = {"updated_at": int(time.time()), "accounts": results, "models": models}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(DATA_FILE, 0o600)
        ok_n = sum(1 for r in results if r["ok"])
        _refreshing["msg"] = "刷新完成: 成功 %d / 共 %d" % (ok_n, len(results))
        return {"ok": True, "msg": _refreshing["msg"]}
    except Exception as e:
        _refreshing["msg"] = "刷新异常: %s" % e
        return {"error": str(e)}
    finally:
        _refreshing["finished_at"] = int(time.time())
        _refreshing["running"] = False


def bg_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            if os.path.exists(ACCOUNTS_FILE):
                refresh_all()
        except Exception:
            pass


# ---------- HTTP ----------

def read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def send_json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _serve_index(self):
        path = os.path.join(BASE_DIR, "index.html")
        if not os.path.exists(path):
            send_json(self, {"error": "index.html 不存在"}, 500)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            self._serve_index()
        elif self.path == "/api/data":
            data = {"updated_at": None, "accounts": [], "models": []}
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass
            send_json(self, data)
        elif self.path.startswith("/api/keys"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            aid = (qs.get("id") or [""])[0]
            account = find_account(aid)
            if not account:
                send_json(self, {"error": "账号不存在"}, 404)
                return
            send_json(self, fetch_keys(account))
        elif self.path == "/api/status":
            with _lock:
                st = dict(_refreshing)
            st["account_count"] = len(load_accounts())
            send_json(self, st)
        else:
            send_json(self, {"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/refresh":
            threading.Thread(target=refresh_all, daemon=True).start()
            send_json(self, {"ok": True, "msg": "已开始刷新"})
        elif self.path == "/api/keys":
            body = read_body(self)
            account = find_account(body.get("id", ""))
            if not account:
                send_json(self, {"error": "账号不存在"}, 404)
                return
            action = body.get("action")
            if action == "create":
                send_json(self, create_key(account, body.get("name", "新 Key")))
            elif action == "delete":
                send_json(self, delete_key(account, body.get("key_id", "")))
            elif action == "bind":
                # 手动绑定完整 Key: 用 keyPrefix 匹配上游列表里的 Key
                full_key = (body.get("full_key") or "").strip()
                if not full_key.startswith("sk_tr_"):
                    send_json(self, {"error": "完整 Key 应以 sk_tr_ 开头"}, 400)
                    return
                res = fetch_keys(account)
                if not res.get("ok"):
                    send_json(self, {"error": "获取 Key 列表失败: " + res.get("error", "")}, 500)
                    return
                matched = [k for k in res["keys"] if k.get("keyPrefix") and full_key.startswith(k["keyPrefix"])]
                if not matched:
                    send_json(self, {"error": "未匹配到该账号下的 Key, 请确认完整 Key 属于此账号"}, 400)
                    return
                if len(matched) > 1:
                    send_json(self, {"error": "前缀匹配到多个 Key, 无法确定绑定目标"}, 400)
                    return
                kid = matched[0]["id"]
                full = load_full_keys()
                full[kid] = full_key
                save_full_keys(full)
                send_json(self, {"ok": True, "msg": "已绑定", "key_id": kid})
            else:
                send_json(self, {"error": "unknown action"}, 400)
        elif self.path == "/api/accounts":
            body = read_body(self)
            action = body.get("action")
            with _lock:
                accounts = load_accounts()
                if action == "add":
                    a = parse_account_line(body.get("line", "").strip())
                    if not a:
                        send_json(self, {"error": "格式无法识别, 请提供 sess_ 开头的 Token"}, 400)
                        return
                    if any(x["token"] == a["token"] for x in accounts):
                        send_json(self, {"error": "该 Token 已存在"}, 400)
                        return
                    accounts.append(a)
                    save_accounts(accounts)
                    send_json(self, {"ok": True, "msg": "已添加: %s" % (a["note"] or a["token"][:12])})
                elif action == "remove":
                    aid = body.get("id", "")
                    def _match(a):
                        return hashlib.sha256(a["token"].encode()).hexdigest()[:12] == aid
                    accounts = [x for x in accounts if not _match(x)]
                    save_accounts(accounts)
                    send_json(self, {"ok": True, "msg": "已删除"})
                elif action in ("import", "replace_all"):
                    # 批量导入: 默认追加(append), 勾选清空则替换(replace)
                    mode = body.get("mode", "append")
                    new_list, bad = parse_import_text(body.get("text", ""))
                    if mode == "replace":
                        # 覆盖前自动备份旧列表, 防误操作
                        if os.path.exists(ACCOUNTS_FILE):
                            import shutil
                            shutil.copy(ACCOUNTS_FILE, ACCOUNTS_FILE + ".bak")
                            os.chmod(ACCOUNTS_FILE + ".bak", 0o600)
                        save_accounts(new_list)
                        send_json(self, {"ok": True, "msg": "已清空并导入: %d 条, 无法识别 %d (旧列表已备份 accounts.txt.bak)" % (len(new_list), bad)})
                    else:
                        cur = load_accounts()
                        added = 0
                        dup = 0
                        for a in new_list:
                            if any(x["token"] == a["token"] for x in cur):
                                dup += 1
                                continue
                            cur.append(a)
                            added += 1
                        save_accounts(cur)
                        send_json(self, {"ok": True, "msg": "追加导入: 新增 %d 条, 已存在跳过 %d, 无法识别 %d" % (added, dup, bad)})
                else:
                    send_json(self, {"error": "unknown action"}, 400)
        else:
            send_json(self, {"error": "not found"}, 404)


if __name__ == "__main__":
    if not os.path.exists(ACCOUNTS_FILE):
        save_accounts([])
    threading.Thread(target=bg_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("TokenRhythm 余额看板已启动: http://0.0.0.0:%d  (自动刷新间隔 %d 秒)" % (PORT, REFRESH_INTERVAL))
    srv.serve_forever()
