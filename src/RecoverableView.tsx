import { Component, type ReactNode } from "react";

export async function loadView<T>(loader: () => Promise<T>): Promise<T> {
  // A late rejection may belong to a view the user already left. Recovery
  // stays local to React's mounted boundary; refresh is an explicit action.
  return await loader();
}

export class RecoverableView extends Component<{ label: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <section className="notice error" role="alert">
      <div>
        <h2>{this.props.label}暂时没能打开</h2>
        <p>请刷新页面后继续，已保存的资料和草稿会保留。</p>
        <button className="button primary" onClick={() => window.location.reload()}>刷新页面</button>
      </div>
    </section>;
  }
}
