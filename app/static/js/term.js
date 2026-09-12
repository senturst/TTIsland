/** 终端输出：打字机效果 + append-only 渲染。
 *
 * 主文本区**只追加不重建**——文字游戏的核心资产就是屏幕上的历史，
 * 重建列表会丢掉滚动位置和阅读上下文。
 */

const MAX_LINES = 300;
let queue = [];
let typing = false;
let skip = false;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;   // 一律 textContent，杜绝 XSS
  return node;
}

/** 判断一行属于哪类语义，决定配色。 */
function classify(text) {
  if (!text) return "sys";
  if (text.startsWith("【") && text.includes("】")) return "level";
  if (text.startsWith("—") && text.endsWith("—")) return "divider";
  if (text.includes("** 你死了") || text.startsWith("** 你死了")) return "death";
  if (text.includes("撤离成功")) return "good";
  if (text.includes("暴击")) return "crit";
  if (text.includes("造成伤害") || text.includes("命中")) return "combat";
  if (/击中你|受到 \d+ 点伤害|感染 \+|HP −/.test(text)) return "hurt";
  if (/尸潮|引来了它们|噪音到了临界点/.test(text)) return "bad";
  if (/掉出了|你找到了|你搜出了/.test(text)) return "loot";
  if (/^HP \+|撤离者的补给|获得 /.test(text)) return "good";
  if (text.startsWith("> ") || text.startsWith("【")) return "sys";
  return "";
}

export function initTerm(container) {
  const lines = document.createElement("div");
  lines.className = "term-lines";
  container.innerHTML = "";
  container.appendChild(lines);
  return { container, lines };
}

export function appendLine(host, text, cls) {
  if (!text) return null;
  const node = el("div", "line " + (cls || classify(text)), text);
  host.lines.appendChild(node);

  while (host.lines.childElementCount > MAX_LINES) {
    host.lines.removeChild(host.lines.firstChild);
  }
  host.container.scrollTop = host.container.scrollHeight;
  return node;
}

/** 打字机：18ms/字。同帧多条时加速，避免拖沓。 */
export function typeLine(host, text, cls) {
  return new Promise((resolve) => {
    if (!text) return resolve(null);
    const node = el("div", "line typing " + (cls || classify(text)), "");
    host.lines.appendChild(node);
    node.classList.add("typing");

    const speed = Math.max(6, Math.min(18, Math.round(700 / Math.max(1, text.length))));
    let i = 0;
    const timer = setInterval(() => {
      if (skip) {
        node.textContent = text;
        finish();
        return;
      }
      node.textContent = text.slice(0, ++i);
      host.container.scrollTop = host.container.scrollHeight;
      if (i >= text.length) finish();
    }, speed);

    function finish() {
      clearInterval(timer);
      node.classList.remove("typing");
      node.textContent = text;
      host.container.scrollTop = host.container.scrollHeight;
      while (host.lines.childElementCount > MAX_LINES) {
        host.lines.removeChild(host.lines.firstChild);
      }
      resolve(node);
    }
  });
}

/** 顺序播放一串文本。点击/按键可跳过当前这条。 */
export async function playLines(host, texts) {
  skip = false;
  for (const t of texts) {
    if (!t) continue;
    // 空串当分隔符
    if (t.trim() === "") {
      appendLine(host, "", "divider");
      continue;
    }
    await typeLine(host, t);
    if (skip) skip = false;
  }
}

export function skipTyping() {
  skip = true;
}

/** AI 生成的风味文本到达后，原地替换掉之前那条模板文本。 */
export function patchLine(host, target, text) {
  const nodes = host.lines.querySelectorAll(".line[data-target]");
  for (const n of nodes) {
    if (n.dataset.target === target) {
      n.textContent = text;
      n.classList.add("flavor");
      return true;
    }
  }
  return false;
}

export function markTarget(node, target) {
  if (node) node.dataset.target = target;
}
