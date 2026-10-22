"""实现无关的并发判据探针：走真实 Flask（随机端口）+ 桩 agent，用 HTTP/SSE 观测行为。

不假设具体方法名，只测「行为是否满足不变量」。每条判据都以反例形式给出：
若实现有缺陷，下面某条必然打印【缺陷】。

不变量：
  I1 同一会话任意时刻最多 1 个 agent 线程在跑（并发峰值 == 1）
  I2 SSE 断开 ≠ 任务被取消：断开后线程继续跑完（stub.runs_finished 递增）
  I3 「运行中」的对外表述与线程真实状态一致（断开前/断开后/结束后三种时刻各查一次）
  I4 cancel 之后不永久 409：线程结束后该会话能再次接受新消息
  I5 「正在跑」可被后加入的客户端观测到（断开再连能看到该轮证据）

用法：/home/chenyuxi/anaconda3/envs/Agent/bin/python -I /tmp/rename/probe_after.py
"""
import json
import sys
import threading
import time
import socket
import urllib.request
import urllib.error

ROOT = "/home/chenyuxi/skill/agent"
sys.path.insert(0, ROOT)

# ---------------- 桩 agent：慢、可观测、不碰网络 ----------------
CHUNKS = 100
CHUNK_S = 0.1   # 10s 运行期


class StubAgent:
    def __init__(self):
        self.history = []
        self.active_skills = []
        self.usage_bar = {"output_tokens": 0, "latency_ms": 0, "steps": 0, "model": "stub"}
        self._last_stop = None
        self.store = None
        self.session_id = None
        self._running = 0
        self.concurrent_peak = 0
        self.runs_started = 0
        self.runs_finished = 0
        self.stopped_early = 0
        self.ignore_cancel = False
        self._lock = threading.Lock()

    def answer(self, text, on_delta=None, on_thinking=None):
        with self._lock:
            self._running += 1
            self.runs_started += 1
            self.concurrent_peak = max(self.concurrent_peak, self._running)
        cancelled = False
        try:
            for i in range(CHUNKS):
                time.sleep(CHUNK_S)
                # 桩也尊重 cancel_event（真实 agent 在轮次开头检查）；
                # ignore_cancel=True 模拟「正在执行 Bash，python 杀不掉线程」
                if getattr(self, "ignore_cancel", False):
                    continue
                ce = getattr(self, "cancel_event", None)
                if ce is not None and ce.is_set():
                    cancelled = True
                    break
                if on_delta:
                    on_delta(f"chunk{i} ")
            return "stub-cancelled" if cancelled else "stub-done"
        finally:
            with self._lock:
                self._running -= 1
                self.runs_finished += 1
                if cancelled:
                    self.stopped_early += 1

    def clear(self):
        self.history = []


class StubCfg:
    model = "stub-model"
    permission = "auto"


class StubTools:
    permission = "auto"
    idempotency = None


def patch_session_module():
    """只替换**桩 agent**：后端状态机（Run/busy/订阅）必须是被测的真代码。

    改造后 WebSession.run 已由服务端实现，探针不得覆盖它 —— 否则测的是探针自己。
    """
    return None


def make_app():
    from webapp import server as S

    server_mod = S
    # 绕开 build_session：用桩会话
    stub_holder = {}

    def fake_get_or_create(sid):
        with server_mod.CFG_REF["lock"]:
            s = server_mod._sessions().get(sid)
            if s is None:
                from webapp.session import WebSession, Hooks
                s = WebSession(sid, StubCfg(), None, StubTools(), None)
                s.hooks = Hooks(s)
                s.agent = StubAgent()
                # 桩也要能收到取消事件 / 走真状态机
                s.agent.session = s
                server_mod._sessions()[sid] = s
            stub_holder[sid] = s.agent
            return s

    server_mod._get_or_create = fake_get_or_create

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, server_mod.app, threaded=True)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port, server_mod, stub_holder


# ---------------- HTTP 小工具 ----------------
def post(port, path, body, timeout=15):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(port, path, timeout=15):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class SSEReader:
    """裸 socket 读 SSE：close() 立即返回，不阻塞主线程（urllib 的 buffered reader 会卡住）。"""

    def __init__(self, port, path, body=None):
        import socket
        self.text = ""
        self.done = False
        self.status = None
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        if body is None:
            req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
        else:
            payload = json.dumps(body).encode()
            req = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                   f"Accept: text/event-stream\r\n\r\n")
            self._sock.sendall(req.encode() + payload)
            req = None
        if req:
            self._sock.sendall(req.encode())
        self._sock.settimeout(0.2)
        self._raw = b""
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        try:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self._raw += chunk
                if self.status is None and b"\r\n" in self._raw:
                    head = self._raw.split(b"\r\n", 1)[0].decode("latin1")
                    parts = head.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        self.status = int(parts[1])
                self.text = self._raw.decode("utf-8", "replace")
        except Exception:
            pass
        finally:
            self.done = True

    def close(self):
        """模拟客户端断开：关 socket，立即返回。"""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


T0 = time.time()
def el():
    return f"t={time.time()-T0:.2f}s"

def wait_status(reader, timeout=5.0):
    """等 HTTP 响应头到达（否则 status 为 None）。"""
    t0 = time.time()
    while reader.status is None and time.time() - t0 < timeout:
        time.sleep(0.05)
    return reader.status

def main():
    patch_session_module()
    srv, port, S, holder = make_app()
    results = []

    def report(name, ok, detail):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else '缺陷'}] {name}")
        print(f"         {detail}")

    def status_of(sid):
        """尽力从任一可用入口读该会话的运行态。"""
        for path in (f"/api/session/{sid}/run", f"/api/session/status?session={sid}",
                     f"/api/status?session={sid}", "/api/sessions"):
            st, body = get(port, path)
            if st == 200:
                try:
                    d = json.loads(body)
                except Exception:
                    continue
                if isinstance(d, dict) and "running" in d:
                    return d
                if isinstance(d, list):
                    hit = [x for x in d if x.get("id") == sid or x.get("session") == sid]
                    if hit and any(k in hit[0] for k in ("running", "busy", "state")):
                        return hit[0]
        return None

    print("=" * 74)
    print("I1  同一会话最多 1 个 agent 线程（并发峰值 == 1）")
    print("=" * 74)
    sid1 = "probe_i1"
    r1 = SSEReader(port, "/api/chat", {"session": sid1, "message": "hello"})
    time.sleep(0.6)
    st_a, _ = post(port, "/api/chat", {"session": sid1, "message": "while-visible"})
    r1.close()                                   # ← 关键：先断开（模拟关页/切走）
    time.sleep(0.4)
    r1b = SSEReader(port, "/api/chat", {"session": sid1, "message": "after-detach"})
    st_b = wait_status(r1b)
    time.sleep(0.4)
    ag = holder.get(sid1)
    peak = ag.concurrent_peak if ag else -1
    # 判据三条并列：①未断开时被拒 ②断开后（线程仍在跑）也被拒 ③总共只启动过 1 轮
    ok1 = (st_a == 409) and (st_b == 409) and (ag.runs_started == 1)
    report("I1 断开后不得并发双跑", ok1,
           f"[{el()}] SSE 未断开时 POST={st_a}（期望 409）｜断开后 POST={st_b}（期望 409）"
           f"｜总启动数={ag.runs_started}（期望 1）｜并发峰值={peak}")
    time.sleep(CHUNKS * CHUNK_S + 0.5)

    print()
    print("=" * 74)
    print("I2  SSE 断开 ≠ 任务被取消：断开后线程仍跑完")
    print("=" * 74)
    sid2 = "probe_i2"
    r2 = SSEReader(port, "/api/chat", {"session": sid2, "message": "long"})
    time.sleep(0.6)
    ag2 = holder[sid2]
    got_before = len(r2.text)
    r2.close()                                  # ← 模拟关页面/切走
    time.sleep(0.4)
    mid = ag2.runs_finished                    # 断开瞬间：应仍为 0（还没跑完）
    time.sleep(CHUNKS * CHUNK_S + 0.5)
    fin = ag2.runs_finished
    report("I2 断开后任务继续并跑完", (mid == 0) and (fin == 1) and ag2.stopped_early == 0,
           f"[{el()}] 断开前已收正文 {got_before} 字节；断开时 finished={mid}（期望 0=仍在跑）；之后 finished={fin}；"
           f"被取消次数={ag2.stopped_early}（期望 0）")

    print()
    print("=" * 74)
    print("I3  「运行中」的对外表述 == 线程真实状态")
    print("=" * 74)
    sid3 = "probe_i3"
    r3 = SSEReader(port, "/api/chat", {"session": sid3, "message": "x"})
    time.sleep(0.6)
    ag3 = holder[sid3]
    s_running = status_of(sid3)
    truth_running = ag3._running > 0
    r3.close()
    time.sleep(CHUNKS * CHUNK_S + 0.5)
    s_after = status_of(sid3)
    truth_after = ag3._running > 0
    if s_running is None or s_after is None:
        report("I3 有可查询的运行态入口", False,
               "找不到带 running/busy/state 字段的状态入口 —— 用户无从得知「是否真在跑」")
    else:
        def running_flag(d):
            for k in ("running", "busy"):
                if k in d:
                    return bool(d[k])
            return str(d.get("state", "")).lower() in ("running", "busy")
        ok3 = running_flag(s_running) == truth_running and running_flag(s_after) == truth_after
        report("I3 运行态与线程一致", ok3,
               f"[{el()}] 运行中: 接口={running_flag(s_running)} 实际={truth_running} | "
               f"结束后: 接口={running_flag(s_after)} 实际={truth_after}")

    print()
    print("=" * 74)
    print("I4  [回归护栏] 多会话可并行；单会话轮次结束后可复用")
    print("=" * 74)
    sa, sb = "probe_i4a", "probe_i4b"
    ra = SSEReader(port, "/api/chat", {"session": sa, "message": "a"})
    st_a = wait_status(ra)
    time.sleep(0.5)
    rb = SSEReader(port, "/api/chat", {"session": sb, "message": "b"})
    st_b = wait_status(rb)
    time.sleep(0.8)
    aga, agb = holder.get(sa), holder.get(sb)
    both = (st_a == 200) and (st_b == 200) and aga and agb and \
           (aga._running > 0) and (agb._running > 0)
    report("I4a 两个会话可同时各跑一轮", both,
           f"[{el()}] A 会话 POST={st_a} 在跑={aga._running if aga else '?'}｜"
           f"B 会话 POST={st_b} 在跑={agb._running if agb else '?'}")
    ra.close(); rb.close()
    time.sleep(CHUNKS * CHUNK_S + 0.5)
    # 轮次真正结束后（线程已退出）该会话必须能再次接受消息
    st_reuse = None
    t0 = time.time()
    while time.time() - t0 < 12:
        r = SSEReader(port, "/api/chat", {"session": sa, "message": "again"})
        st_reuse = wait_status(r)
        if st_reuse == 200:
            r.close()
            break
        r.close()
        time.sleep(0.5)
    report("I4b 轮次结束后会话可复用（无残留锁）", st_reuse == 200,
           f"[{el()}] 线程已退出后再次 POST={st_reuse}（期望 200），耗时 {time.time()-t0:.1f}s")
    time.sleep(CHUNKS * CHUNK_S + 0.5)

    print()
    print("=" * 74)
    print("I5  「正在跑」可被后加入的客户端看到（任一入口皆可）")
    print("=" * 74)
    sid5 = "probe_i5"
    r5 = SSEReader(port, "/api/chat", {"session": sid5, "message": "z"})
    wait_status(r5)
    time.sleep(1.5)                              # 让它先产出一些正文
    produced = len(r5.text)
    r5.close()                                   # 模拟切走
    time.sleep(0.6)
    # 穷举所有合理入口，任一能给出「该轮在跑 / 已产出内容」的证据即通过
    probes = []
    for path in (f"/api/session/{sid5}/run", f"/api/session/status?session={sid5}",
                 f"/api/status?session={sid5}", f"/api/session/{sid5}", "/api/sessions"):
        st, body = get(port, path)
        probes.append((path, st, len(body)))
        if st != 200:
            continue
        try:
            d = json.loads(body)
        except Exception:
            d = body
        blob = json.dumps(d, ensure_ascii=False) if not isinstance(d, str) else d
        # 判定证据：出现 running/busy 标记，或出现该轮已产出的正文片段
        if ("chunk" in blob) or ('"running": true' in blob) or ('"busy": true' in blob) \
           or ('"running":true' in blob) or ('"busy":true' in blob) \
           or ('"state": "running"' in blob) or ('"state":"running"' in blob):
            report("I5 后加入可见该轮证据", True,
                   f"[{el()}] 断开前已产出 {produced} 字节；命中入口 {path} 状态={st}")
            time.sleep(CHUNKS * CHUNK_S + 0.5)
            srv.shutdown()
            return _finish(results)
    report("I5 后加入可见该轮证据", False,
           f"[{el()}] 断开前已产出 {produced} 字节；试遍入口均无证据："
           + "；".join(f"{p}→{st}({n}B)" for p, st, n in probes))
    time.sleep(CHUNKS * CHUNK_S + 0.5)

    srv.shutdown()
    return _finish(results)


def _finish(results):
    print()
    print("=" * 74)
    bad = [n for n, ok, _ in results if not ok]
    print(f"结论：{len(results) - len(bad)}/{len(results)} 通过")
    for n in bad:
        print(f"  未通过：{n}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
