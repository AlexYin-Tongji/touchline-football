import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { ArrowRight, Goal, KeyRound, LogIn } from "lucide-react";
import { api, errorText, post } from "./api";
import { ErrorNotice, Spinner } from "./ui";

interface AuthState { required: boolean; authenticated: boolean; server_mode: boolean }
const AuthContext = createContext<{ status: AuthState | null; logout: () => Promise<void> }>({ status: null, logout: async () => undefined });
export const useAuth = () => useContext(AuthContext);

export function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthState | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const readStatus = async () => {
    setError("");
    try { setStatus(await api<AuthState>("/auth/status")); }
    catch (err) { setError(errorText(err)); }
  };
  useEffect(() => {
    void readStatus();
    const expired = () => { setStatus(previous => ({ required: true, server_mode: previous?.server_mode ?? true, authenticated: false })); setError("登录已过期，请重新进入你的看台。"); };
    window.addEventListener("touchline-auth-required", expired);
    return () => window.removeEventListener("touchline-auth-required", expired);
  }, []);
  const logout = async () => { await post("/auth/logout"); setStatus(previous => previous ? { ...previous, authenticated: false } : previous); setPassword(""); };
  if (status && (!status.required || status.authenticated)) return <AuthContext.Provider value={{ status, logout }}>{children}</AuthContext.Provider>;
  return <main className="login-screen">
    <section className="login-intro"><span className="v4-brand"><Goal size={31} />看台</span><span className="overline">你的私人足球主场</span><h1>每一份热爱，<br />都有值得细读的故事。</h1><p>跟上球队的变化，读懂消息的来龙去脉。<br />带着你的问题，与足球助手一起看球。</p><div className="login-pitch" aria-hidden="true"><span /></div></section>
    <section className="login-panel">
      {!status ? <><Spinner text="正在连接你的看台" /><ErrorNotice>{error}</ErrorNotice>{error && <button className="button" onClick={readStatus}>重新连接</button>}</> : <form onSubmit={async e => {
        e.preventDefault(); if (busy) return; setBusy(true); setError("");
        try { await post("/auth/login", { password }); setPassword(""); await readStatus(); }
        catch (err) { setError(errorText(err)); }
        finally { setBusy(false); }
      }}>
        <span className="login-icon"><KeyRound size={24} /></span><h2>欢迎回到看台</h2><p>输入访问密码，进入你的私人网站。</p>
        <ErrorNotice>{error}</ErrorNotice>
        <label htmlFor="site-password">访问密码</label><input id="site-password" type="password" autoComplete="current-password" required autoFocus value={password} onChange={e => setPassword(e.target.value)} placeholder="网站访问密码" />
        <button className="button dark-button" disabled={busy || !password}>{busy ? <Spinner text="正在登录" /> : <><LogIn size={17} />进入看台<ArrowRight size={17} /></>}</button>
        <small>资料和跟队偏好保存在服务器，换一台设备也能继续阅读。</small>
      </form>}
    </section>
  </main>;
}
