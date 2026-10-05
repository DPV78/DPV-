// Formata o texto do assistente (markdown simples) com escape de HTML.
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "", list = null;
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { close(); html += `<h4>${inline(m[2])}</h4>`; }
    else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
    else if (!line.trim()) { close(); }
    else { close(); html += `<p>${inline(line)}</p>`; }
  }
  close();
  return html;
}
document.querySelectorAll(".body.md").forEach(el => { el.innerHTML = renderMarkdown(el.textContent); el.classList.add("rendered"); });

// Botões que disparam chamadas demoradas ao assistente
document.querySelectorAll("form[data-busy]").forEach(f => f.addEventListener("submit", () => {
  const b = f.querySelector("button"); b.disabled = true; b.textContent = f.dataset.busy;
}));

// Discussão: envia a mensagem (se houver) e transmite a resposta do assistente
const askBtn = document.getElementById("ask-ai");
if (askBtn) {
  askBtn.addEventListener("click", async () => {
    const form = document.getElementById("msg-form");
    const content = document.getElementById("msg-content").value.trim();
    askBtn.disabled = true;
    const original = askBtn.textContent;
    askBtn.textContent = "Assistente analisando…";
    try {
      if (content) {
        const r = await fetch(form.action, {method: "POST", body: new FormData(form), headers: {Accept: "application/json"}});
        if (!r.ok) throw new Error("Falha ao enviar a mensagem");
        const who = document.createElement("div");
        who.className = "msg user";
        who.innerHTML = `<div class="who">Você</div><div class="body">${escapeHtml(content)}</div>`;
        document.getElementById("thread").appendChild(who);
        document.getElementById("msg-content").value = "";
      }
      document.getElementById("empty-thread")?.remove();
      const box = document.createElement("div");
      box.className = "msg assistant";
      box.innerHTML = '<div class="who">Assistente estratégico</div><div class="body"></div>';
      document.getElementById("thread").appendChild(box);
      const body = box.querySelector(".body");
      const resp = await fetch(askBtn.dataset.url, {method: "POST", headers: {"X-CSRF-Token": window.CSRF}});
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({error: resp.statusText}));
        body.textContent = "Erro: " + err.error;
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let full = "";
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        full += decoder.decode(value, {stream: true});
        body.textContent = full;
        box.scrollIntoView({block: "end"});
      }
      body.innerHTML = renderMarkdown(full);
      body.classList.add("rendered");
    } catch (e) {
      alert(e.message);
    } finally {
      askBtn.disabled = false;
      askBtn.textContent = original;
    }
  });
}

// Copiar número do processo (para colar no PJe aberto com token/Whom)
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(b.dataset.copy); const t = b.textContent; b.textContent = "Copiado"; setTimeout(() => b.textContent = t, 1500); }
  catch { prompt("Copie o número:", b.dataset.copy); }
}));
