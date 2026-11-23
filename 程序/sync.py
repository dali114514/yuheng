# -*- coding: utf-8 -*-
"""
同步到主机 E 盘：Mac 上保留一份工作数据，定时完整复制到 5060 Ti 主机的 E:\\赚钱小助手（主机 E 盘上的文件夹名没改，和 Mac 上的「玉衡5.0」对应）

  data\\trading.db     交易数据库（信号、成交、环境卡、AI 意见、问答……）  每 15 分钟
  history\\history.db  历史 K 线库（比较大）                               每天收盘复盘后一次
  reports\\…           「报告」文件夹里新增或改过的文件                     每 15 分钟
  主机那边每天自动给数据库留一份备份（backup 文件夹，保留 30 天）。

  为什么不直接把数据库放在 E 盘上：盯盘不能依赖网络。主机关机或网络抖动时，Mac 照常记录，主机恢复后下一轮自动补上。
  两边各一份完整数据，Mac 或主机任何一块硬盘坏了都不会丢。

  设置.json → 存储：启用、主机地址 / 外网地址、端口、口令、同步间隔_分钟、
                    历史库外网也同步（默认 false：历史 K 线库 250 MB，只在局域网时同步；交易数据库和报告走外网也照常同步）
  主机端：host_setup 里的 install_storage.cmd
"""
import datetime as dt
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
DATA = HERE / "data"
REPORTS = BASE / "报告"
STATUS = DATA / ".sync_status.json"
MANIFEST = DATA / ".sync_manifest.json"
DEFAULT = {"启用": False, "主机地址": "", "外网地址": "", "端口": 8766, "口令": "", "同步间隔_分钟": 15, "历史库外网也同步": False}
SKIP = {".DS_Store"}


def settings(cfg):
    s = dict(DEFAULT)
    s.update(cfg.get("存储") or {})
    return s


def _host(s):
    import hosts
    return hosts.pick(s.get("主机地址"), s.get("外网地址"), int(s["端口"]))


def route(cfg):
    import hosts
    s = settings(cfg)
    return hosts.route(s.get("主机地址"), s.get("外网地址"), int(s["端口"]))


def _base(s):
    return f"http://{_host(s)[0]}:{int(s['端口'])}"


def _req(s, method, path, data=None, length=None, timeout=10):
    req = urllib.request.Request(_base(s) + "/" + urllib.parse.quote(path), data=data, method=method,
                                 headers={"X-Token": str(s.get("口令") or ""), "Content-Type": "application/octet-stream"})
    if length is not None:
        req.add_header("Content-Length", str(length))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def ping(cfg):
    s = settings(cfg)
    if not s["主机地址"] and not s.get("外网地址"):
        return False, "没填主机地址"
    if not s.get("口令"):
        return False, "没有口令（设置.json → 存储 → 口令）"
    try:
        r = _req(s, "GET", "ping", timeout=3)
        return True, f"E 盘剩余 {r.get('free_gb', '?')} GB"
    except urllib.error.HTTPError as e:
        return False, "口令不对" if e.code == 403 else f"主机返回 {e.code}"
    except Exception as e:
        return False, f"连不上主机存储服务（{type(e).__name__}）"


def put(s, rel, path, timeout=600, tries=3):
    """上传一个文件；网络偶尔抖一下（连不上、被重置）就等几秒重试，最多 3 次。"""
    size = os.path.getsize(path)
    for i in range(tries):
        try:
            with open(path, "rb") as f:
                return _req(s, "PUT", "files/" + rel, data=f, length=size, timeout=timeout)
        except urllib.error.HTTPError:
            raise                                  # 口令不对、路径不对：重试也没用
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def snapshot(db):
    """数据库正在被写也能安全复制：用 SQLite 自带的备份功能做一份一致的快照。"""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(str(db), timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return tmp


def _load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def status():
    return _load(STATUS, {})


def run(cfg, full=False, log=lambda *a: None):
    """同步一轮。full=True：连历史 K 线库一起（收盘复盘后调用）。返回说明文字。"""
    s = settings(cfg)
    st = status()
    if not s.get("启用"):
        return "E 盘同步没开启"
    ok, why = ping(cfg)
    if not ok:
        st.update(last_err=why, last_try=dt.datetime.now().isoformat(timespec="seconds"))
        STATUS.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        return f"✗ E 盘同步失败：{why}（下一轮再试，数据都还在 Mac 上）"
    sent, nbytes, errs, main_ok = 0, 0, [], False
    t0 = time.time()
    dbs = [("data/trading.db", DATA / "trading.db")]
    hist = DATA / "history.db"
    lan = _host(s)[1] == "局域网"
    if hist.exists() and (lan or s.get("历史库外网也同步")) and (full or not st.get("history_mtime") or
                          (hist.stat().st_mtime != st.get("history_mtime") and time.time() - st.get("history_at", 0) > 20 * 3600)):
        dbs.append(("history/history.db", hist))
    for rel, db in dbs:
        if not db.exists():
            continue
        tmp = None
        try:
            tmp = snapshot(db)
            put(s, rel, tmp)
            sent += 1
            main_ok = main_ok or rel == "data/trading.db"
            nbytes += os.path.getsize(tmp)
            if rel.startswith("history"):
                st.update(history_mtime=db.stat().st_mtime, history_at=time.time())
        except Exception as e:
            errs.append(f"{rel}：{type(e).__name__} {str(e)[:60]}")
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    man = _load(MANIFEST, {})
    if REPORTS.exists():
        for p in sorted(REPORTS.rglob("*")):
            if not p.is_file() or p.name in SKIP or p.name.startswith("~$"):
                continue
            rel = "reports/" + p.relative_to(REPORTS).as_posix()
            sig = [p.stat().st_size, int(p.stat().st_mtime)]
            if man.get(rel) == sig:
                continue
            try:
                put(s, rel, p)
                man[rel] = sig
                sent += 1
                nbytes += sig[0]
            except Exception as e:
                errs.append(f"{rel}：{type(e).__name__}")
                if len(errs) > 5:
                    break
    MANIFEST.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    now = dt.datetime.now().isoformat(timespec="seconds")
    st.update(last_try=now, files=sent, bytes=nbytes, secs=round(time.time() - t0, 1))
    if main_ok:
        st["last_ok"] = now                        # 交易数据库传成功就算这一轮成功；个别文件失败下一轮自动补
    st["last_err"] = "；".join(errs[:3]) if errs else None
    st["partial"] = bool(errs) and main_ok
    STATUS.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    st["route"] = _host(s)[1]
    msg = f"E 盘同步（{st['route']}）：{sent} 个文件，{nbytes / 1e6:.1f} MB，{st['secs']} 秒"
    if not lan and not s.get("历史库外网也同步") and hist.exists():
        msg += "；历史 K 线库等回到家里局域网再同步"
    return msg + (f"；{len(errs)} 个失败（{errs[0]}）" if errs else "")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    import macro_report as mr
    print(run(mr.load_config(), full="--full" in sys.argv, log=print))
