"""后端状态机缺陷的确定性复现（改造前版，保留作对照）。

⚠️ 这个脚本**只能在改造前的代码上运行**：它直接驱动旧实现内部的白盒符号
    `server._sse_stream(session)` —— 那个函数在修复中被删除了，因为它的存在本身
    就是缺陷（SSE 响应的生命周期与运行态绑在一起）。留着它是为了让「改造前复现过
    什么」这件事可查，而不是当作一个还能跑的测试。

现在要验证同样的不变量，请用**实现无关**的黑盒探针：
    tools/concurrency/probe_after.py          # 后端 6 条判据（改造前 2/6）
    tools/concurrency/ui_concurrency_probe.py # 浏览器 17 条判据（含负控）

对照关系（旧白盒脚本 → 新黑盒判据）：
    R1  busy 被 SSE 断开错误复位        → I1（断开后仍应 409）
    R2  cancel 对正在执行的 Bash 无效   → I4（协作式取消的诚实语义）
    R3  409 依据的 busy 与实际在跑对不上 → I3（对外表述 == 线程真实状态）

原始重现内容（改造前实测）：
  R1  断开 SSE 后 busy 被 finally 置 False，而 agent 线程仍在跑
      → 此时再发消息不会被 409 拦住 → 同一会话并发两个 agent 线程（峰值实测 = 2）
  R2  /api/cancel 只置 cancel_event，不复位 busy；对阻塞在 subprocess.run 的 Bash 无效
  R3  409 的判定依据是 busy，而 busy 会被 SSE 断开错误清除
      → 用户看到「正在运行中」与实际在跑两件事对不上
"""
import sys

sys.path.insert(0, "/home/chenyuxi/skill/agent")

MSG = """\
此脚本依赖改造前才有的 server._sse_stream（已于修复中删除 —— 它的存在本身即缺陷：
SSE 响应的生命周期与运行态被绑在一起，断开就顺手 cancel + 复位 busy）。

请改用实现无关的黑盒探针：
  /home/chenyuxi/anaconda3/envs/Agent/bin/python -I tools/concurrency/probe_after.py
  /home/chenyuxi/anaconda3/bin/python tools/concurrency/ui_concurrency_probe.py
详见 docs/session-concurrency-fix.md 第 3、5、9 节。
"""


def main() -> int:
    try:
        from webapp import server as S  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"导入失败：{type(e).__name__}: {e}")
        return 1
    missing = [n for n in ("_sse_stream",) if not hasattr(S, n)]
    if missing:
        print(f"[不适用] 当前代码已无 {', '.join(missing)} —— 说明缺陷已按架构修复。\n")
        print(MSG)
        return 0
    print("[意外] 当前代码仍存在 _sse_stream：本脚本的白盒复现路径应已作废，请复查实现。\n")
    print(MSG)
    return 2


if __name__ == "__main__":
    sys.exit(main())
