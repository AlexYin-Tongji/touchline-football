(() => {
  const notice = document.createElement('div');
  notice.style.cssText = 'position:sticky;top:0;z-index:40;display:flex;gap:20px;align-items:center;justify-content:center;padding:16px 20px;background:#18222d;color:white;font:15px/1.6 system-ui,sans-serif;flex-wrap:wrap;';
  const text = document.createElement('strong');
  text.textContent = '这是静态设计原型，不会保存配置或调用模型。';
  const link = document.createElement('a');
  link.href = 'http://127.0.0.1:8765/';
  link.textContent = '进入正式应用 →';
  link.style.cssText = 'color:white;text-decoration:none;border:1px solid #627487;padding:8px 14px;border-radius:6px;';
  notice.append(text, link);
  document.body.prepend(notice);
  const settings = document.getElementById('settings');
  if (settings) {
    settings.replaceChildren();
    const heading = document.createElement('h1');
    heading.textContent = '模型配置请在正式应用中完成';
    const explanation = document.createElement('p');
    explanation.textContent = '这里仅供查看设计。正式应用会明确显示保存结果，并实际验证模型与工具调用。';
    const appLink = link.cloneNode(true);
    appLink.style.background = '#18222d';
    settings.append(heading, explanation, appLink);
  }
})();
