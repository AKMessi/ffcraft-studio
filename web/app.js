(() => {
  const $ = (id) => document.getElementById(id);

  const state = {
    messages: [],        // [{ role: 'user'|'assistant', content: string }]
    attachedFile: null,  // ffprobe metadata object from backend
    runtime: null,
    isStreaming: false,
  };

  // DOM refs
  const chatScroll = $("chatScroll");
  const welcomeView = $("welcomeView");
  const messagesList = $("messagesList");
  const promptInput = $("promptInput");
  const sendBtn = $("sendBtn");
  const newChatBtn = $("newChatBtn");
  const attachFileBtn = $("attachFileBtn");
  const pastePathToggleBtn = $("pastePathToggleBtn");
  const pathBar = $("pathBar");
  const manualPathInput = $("manualPathInput");
  const probePathBtn = $("probePathBtn");
  const closePathBtn = $("closePathBtn");
  const attachedCard = $("attachedCard");
  const attachedName = $("attachedName");
  const attachedPills = $("attachedPills");
  const detachBtn = $("detachBtn");
  const modelPill = $("modelPill");
  const modelPillText = $("modelPillText");
  const ffmpegPill = $("ffmpegPill");
  const ffmpegPillText = $("ffmpegPillText");
  const setupBanner = $("setupBanner");
  const setupTitle = $("setupTitle");
  const setupSubtitle = $("setupSubtitle");
  const downloadModelBtn = $("downloadModelBtn");
  const retryStartBtn = $("retryStartBtn");
  const downloadBarWrap = $("downloadBarWrap");
  const downloadBarFill = $("downloadBarFill");
  const downloadStats = $("downloadStats");

  function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  function escapeHtml(str) {
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatInlineMarkdown(text) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return html;
  }

  function renderProseBlock(text) {
    const lines = text.trim().split("\n");
    if (!lines.length || (lines.length === 1 && !lines[0].trim())) return "";

    let html = "";
    let inOl = false;
    let inUl = false;

    const closeLists = () => {
      if (inOl) { html += "</ol>"; inOl = false; }
      if (inUl) { html += "</ul>"; inUl = false; }
    };

    for (const raw of lines) {
      const line = raw.trim();
      if (!line) {
        closeLists();
        continue;
      }
      const olMatch = line.match(/^\d+\.\s+(.*)$/);
      const ulMatch = line.match(/^[-*]\s+(.*)$/);
      if (olMatch) {
        if (inUl) { html += "</ul>"; inUl = false; }
        if (!inOl) { html += "<ol>"; inOl = true; }
        html += `<li>${formatInlineMarkdown(olMatch[1])}</li>`;
      } else if (ulMatch) {
        if (inOl) { html += "</ol>"; inOl = false; }
        if (!inUl) { html += "<ul>"; inUl = true; }
        html += `<li>${formatInlineMarkdown(ulMatch[1])}</li>`;
      } else {
        closeLists();
        html += `<p>${formatInlineMarkdown(line)}</p>`;
      }
    }
    closeLists();
    return html;
  }

  function parseAssistantContent(text) {
    // Split into prose and ```lang ... ``` blocks
    const re = /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g;
    const segments = [];
    let lastIdx = 0;
    let match;
    while ((match = re.exec(text)) !== null) {
      if (match.index > lastIdx) {
        segments.push({ type: "prose", content: text.slice(lastIdx, match.index) });
      }
      segments.push({
        type: "code",
        lang: match[1] || "bash",
        content: match[2].trim(),
      });
      lastIdx = re.lastIndex;
    }
    if (lastIdx < text.length) {
      segments.push({ type: "prose", content: text.slice(lastIdx) });
    }
    return segments;
  }

  function buildCommandCard(lang, command) {
    const card = document.createElement("div");
    card.className = "cmd-card";

    const isFfmpeg = /\bffmpeg\b/i.test(command);
    card.innerHTML = `
      <div class="cmd-header">
        <span class="cmd-lang">${escapeHtml(lang)}</span>
        <div class="cmd-actions">
          <button class="btn-secondary copy-btn">Copy</button>
          ${isFfmpeg ? '<button class="btn-primary run-btn">▶ Run with FFmpeg</button>' : ""}
        </div>
      </div>
      <pre class="cmd-code">${escapeHtml(command)}</pre>
    `;

    const copyBtn = card.querySelector(".copy-btn");
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(command);
        copyBtn.textContent = "Copied ✓";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 1600);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = command;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        copyBtn.textContent = "Copied ✓";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 1600);
      }
    });

    const runBtn = card.querySelector(".run-btn");
    if (runBtn) {
      runBtn.addEventListener("click", () => executeCommandInCard(card, runBtn, command));
    }
    return card;
  }

  async function executeCommandInCard(card, runBtn, command) {
    let workingDir = state.attachedFile ? state.attachedFile.dir : null;
    if (!workingDir) {
      runBtn.disabled = true;
      runBtn.textContent = "Pick folder…";
      try {
        const res = await fetch("/api/folder/pick", { method: "POST" });
        const data = await res.json();
        if (data.cancelled || !data.folder) {
          runBtn.disabled = false;
          runBtn.textContent = "▶ Run with FFmpeg";
          return;
        }
        workingDir = data.folder;
      } catch {
        runBtn.disabled = false;
        runBtn.textContent = "▶ Run with FFmpeg";
        return;
      }
    }

    // Remove previous run drawer if re-running
    const prev = card.querySelector(".run-drawer");
    if (prev) prev.remove();

    const drawer = document.createElement("div");
    drawer.className = "run-drawer";
    drawer.innerHTML = `
      <div class="run-header">
        <span class="run-label">Starting FFmpeg…</span>
        <button class="btn-danger cancel-run-btn">Cancel</button>
      </div>
      <div class="progress-wrap">
        <div class="progress-track"><div class="progress-fill" style="width: 4%"></div></div>
        <div class="progress-meta">Preparing…</div>
      </div>
    `;
    card.appendChild(drawer);
    scrollToBottom();

    const runLabel = drawer.querySelector(".run-label");
    const cancelBtn = drawer.querySelector(".cancel-run-btn");
    const fill = drawer.querySelector(".progress-fill");
    const meta = drawer.querySelector(".progress-meta");

    runBtn.disabled = true;
    runBtn.textContent = "Running…";

    let activeJobId = null;
    cancelBtn.addEventListener("click", async () => {
      if (!activeJobId) return;
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancelling…";
      await fetch("/api/run/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: activeJobId }),
      });
    });

    try {
      const resp = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command,
          working_dir: workingDir,
          duration_sec: state.attachedFile ? state.attachedFile.duration_sec : null,
          input_size_bytes: state.attachedFile ? state.attachedFile.size_bytes : null,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || "FFmpeg request failed");
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const chunk of parts) {
          const line = chunk.trim();
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5).trim());

          if (ev.type === "start") {
            activeJobId = ev.job_id;
            runLabel.textContent = ev.total_passes > 1
              ? `Running 2-pass encode in ${ev.cwd}…`
              : `Running FFmpeg in ${ev.cwd}…`;
          } else if (ev.type === "pass") {
            if (ev.total_passes > 1) {
              runLabel.textContent = `Pass ${ev.pass} of ${ev.total_passes}…`;
            }
          } else if (ev.type === "progress") {
            if (ev.percent != null) {
              fill.style.width = `${Math.max(4, Math.min(100, ev.percent))}%`;
            }
            const partsTxt = [];
            if (ev.total_passes > 1) partsTxt.push(`Pass ${ev.pass}/${ev.total_passes}`);
            if (ev.percent != null) partsTxt.push(`${ev.percent}%`);
            partsTxt.push(`time=${ev.current_sec}s`);
            if (ev.speed) partsTxt.push(`speed=${ev.speed}`);
            partsTxt.push(`elapsed=${ev.elapsed_sec}s`);
            meta.textContent = partsTxt.join(" · ");
          } else if (ev.type === "done") {
            fill.style.width = "100%";
            cancelBtn.remove();
            runLabel.className = "run-label run-status-ok";
            runLabel.textContent = `✓ Finished in ${ev.elapsed_sec}s`;

            let sizeSummary = ev.output_size_human ? `Output: ${ev.output_name} (${ev.output_size_human})` : "Command completed.";
            let savingsBadge = "";
            if (state.attachedFile && ev.output_size_human && ev.size_delta_pct != null) {
              const sign = ev.size_delta_pct > 0 ? "+" : "";
              sizeSummary = `${state.attachedFile.size_human} → ${ev.output_size_human}`;
              savingsBadge = `<span class="savings-pill">${sign}${ev.size_delta_pct}%</span>`;
            }

            const resultRow = document.createElement("div");
            resultRow.className = "result-row";
            resultRow.innerHTML = `
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span>${escapeHtml(sizeSummary)}</span>
                ${savingsBadge}
              </div>
              ${ev.output_path || ev.cwd ? '<button class="btn-secondary reveal-btn">Show in Folder</button>' : ""}
            `;
            drawer.appendChild(resultRow);

            const revealBtn = resultRow.querySelector(".reveal-btn");
            if (revealBtn) {
              revealBtn.addEventListener("click", () => {
                fetch("/api/reveal", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ path: ev.output_path || ev.cwd }),
                });
              });
            }
            scrollToBottom();
          } else if (ev.type === "cancelled") {
            drawer.classList.add("error");
            cancelBtn.remove();
            runLabel.className = "run-label run-status-err";
            runLabel.textContent = "Cancelled by user.";
          } else if (ev.type === "error") {
            drawer.classList.add("error");
            cancelBtn.remove();
            runLabel.className = "run-label run-status-err";
            runLabel.textContent = "FFmpeg failed";
            const pre = document.createElement("pre");
            pre.className = "error-pre";
            pre.textContent = ev.message;
            drawer.appendChild(pre);
          }
        }
      }
    } catch (err) {
      drawer.classList.add("error");
      cancelBtn.remove();
      runLabel.className = "run-label run-status-err";
      runLabel.textContent = `Error: ${err.message}`;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = "▶ Run Again";
    }
  }

  function renderAssistantBubble(bubbleEl, rawText, isFinal = false) {
    bubbleEl.innerHTML = "";
    const segments = parseAssistantContent(rawText);
    for (const seg of segments) {
      if (seg.type === "code") {
        bubbleEl.appendChild(buildCommandCard(seg.lang, seg.content));
      } else {
        const div = document.createElement("div");
        div.innerHTML = renderProseBlock(seg.content);
        if (div.innerHTML) bubbleEl.appendChild(div);
      }
    }
    if (!isFinal) {
      const cur = document.createElement("span");
      cur.className = "cursor-blink";
      bubbleEl.appendChild(cur);
    }
  }

  function appendUserMessage(text, attachedFile) {
    welcomeView.classList.add("hidden");
    const row = document.createElement("div");
    row.className = "msg-row user";

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble user";
    if (attachedFile) {
      const tag = document.createElement("div");
      tag.className = "msg-file-tag";
      tag.textContent = `📎 ${attachedFile.filename} (${attachedFile.size_human}${attachedFile.duration_clock ? " · " + attachedFile.duration_clock : ""})`;
      bubble.appendChild(tag);
    }
    const content = document.createElement("div");
    content.textContent = text;
    bubble.appendChild(content);

    row.appendChild(bubble);
    messagesList.appendChild(row);
    scrollToBottom();
  }

  async function sendMessage(overrideText = null) {
    const text = (overrideText ?? promptInput.value).trim();
    if (!text || state.isStreaming) return;
    if (!state.runtime || state.runtime.state !== "ready") {
      alert("FFCraft-1.5B is not ready yet. Please wait for the status pill to turn green (or download the model above).");
      return;
    }

    promptInput.value = "";
    autoResizeTextarea();

    const showFileTag = state.messages.length === 0 ? state.attachedFile : null;
    appendUserMessage(text, showFileTag);
    state.messages.push({ role: "user", content: text });

    // Create streaming assistant row
    const row = document.createElement("div");
    row.className = "msg-row assistant";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble assistant";
    bubble.innerHTML = '<span class="cursor-blink"></span>';
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = "FFCraft-1.5B · thinking…";

    row.appendChild(bubble);
    row.appendChild(meta);
    messagesList.appendChild(row);
    scrollToBottom();

    state.isStreaming = true;
    sendBtn.disabled = true;

    let accText = "";
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: state.messages,
          attached_file: state.attachedFile,
          use_system_prompt: true,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || "Chat request failed");
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const chunk of parts) {
          const line = chunk.trim();
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "delta") {
            accText += ev.content;
            renderAssistantBubble(bubble, accText, false);
            scrollToBottom();
          } else if (ev.type === "done") {
            accText = ev.full_text;
            renderAssistantBubble(bubble, accText, true);
            meta.textContent = `FFCraft-1.5B · ${ev.tokens} tokens · ${ev.elapsed_sec}s · ${ev.tokens_per_sec} t/s`;
            state.messages.push({ role: "assistant", content: accText });
            scrollToBottom();
          } else if (ev.type === "error") {
            throw new Error(ev.message);
          }
        }
      }
    } catch (err) {
      bubble.innerHTML = `<p style="color:#fda4af;">Error: ${escapeHtml(err.message)}</p>`;
      meta.textContent = "FFCraft-1.5B · error";
      state.messages.pop();
    } finally {
      state.isStreaming = false;
      sendBtn.disabled = false;
      promptInput.focus();
    }
  }

  function renderAttachedFile(file) {
    state.attachedFile = file;
    if (!file) {
      attachedCard.classList.add("hidden");
      return;
    }
    attachedName.textContent = file.filename;
    attachedName.title = file.path;
    const pills = [file.size_human];
    if (file.duration_clock) pills.push(file.duration_clock);
    if (file.width && file.height) pills.push(`${file.width}×${file.height}`);
    if (file.fps) pills.push(`${file.fps} fps`);
    if (file.vcodec) pills.push(file.vcodec);
    if (file.acodec) pills.push(file.acodec);
    if (file.subtitle_streams > 0) pills.push(`${file.subtitle_streams} sub`);

    attachedPills.innerHTML = pills
      .map((p) => `<span class="meta-pill">${escapeHtml(String(p))}</span>`)
      .join("");
    attachedCard.classList.remove("hidden");
  }

  function resetChat() {
    if (state.isStreaming) return;
    state.messages = [];
    messagesList.innerHTML = "";
    welcomeView.classList.remove("hidden");
    promptInput.focus();
  }

  function autoResizeTextarea() {
    promptInput.style.height = "auto";
    promptInput.style.height = Math.min(160, promptInput.scrollHeight) + "px";
  }

  function updateRuntimeUI(st) {
    state.runtime = st;

    // FFmpeg pill
    ffmpegPill.className = `status-pill ${st.ffmpeg_found ? "ready" : "error"}`;
    ffmpegPillText.textContent = st.ffmpeg_found ? "ffmpeg ✓" : "ffmpeg missing";

    // Model pill & setup banner
    if (st.state === "ready") {
      modelPill.className = "status-pill ready";
      modelPillText.textContent = `${st.model_alias} · Ready`;
      setupBanner.classList.add("hidden");
    } else if (st.state === "starting" || st.state === "idle") {
      modelPill.className = "status-pill starting";
      modelPillText.textContent = "Loading FFCraft-1.5B…";
      setupBanner.classList.add("hidden");
    } else if (st.state === "missing_model") {
      modelPill.className = "status-pill starting";
      modelPillText.textContent = "Model needed";
      setupBanner.classList.remove("hidden");
      setupTitle.textContent = "Download FFCraft-1.5B (1.6 GB) to get started";
      setupSubtitle.innerHTML = `Pulls <code>${escapeHtml(st.default_gguf)}</code> directly from Hugging Face (<code>${escapeHtml(st.hf_repo)}</code>).`;
      downloadModelBtn.classList.remove("hidden");
      downloadModelBtn.disabled = false;
      retryStartBtn.classList.add("hidden");
      downloadBarWrap.classList.add("hidden");
    } else if (st.state === "downloading") {
      modelPill.className = "status-pill starting";
      modelPillText.textContent = `Downloading ${st.download.percent}%`;
      setupBanner.classList.remove("hidden");
      setupTitle.textContent = "Downloading FFCraft-1.5B v2 from Hugging Face…";
      downloadModelBtn.classList.add("hidden");
      retryStartBtn.classList.add("hidden");
      downloadBarWrap.classList.remove("hidden");
      downloadBarFill.style.width = `${st.download.percent}%`;
      const dlMb = (st.download.downloaded_bytes / (1024 * 1024)).toFixed(0);
      const totMb = st.download.total_bytes ? (st.download.total_bytes / (1024 * 1024)).toFixed(0) : "1646";
      downloadStats.textContent = `${st.download.percent}% · ${dlMb} / ${totMb} MB · ${st.download.speed_mb_s} MB/s`;
    } else if (st.state === "missing_llama") {
      modelPill.className = "status-pill error";
      modelPillText.textContent = "llama-server missing";
      setupBanner.classList.remove("hidden");
      setupTitle.textContent = "llama.cpp (llama-server) is required";
      setupSubtitle.innerHTML = `Install once via <code>winget install ggml.llamacpp</code> (Windows) or <code>brew install llama.cpp</code> (macOS), then click Retry.`;
      downloadModelBtn.classList.add("hidden");
      retryStartBtn.classList.remove("hidden");
    } else {
      modelPill.className = "status-pill error";
      modelPillText.textContent = "Model error";
      setupBanner.classList.remove("hidden");
      setupTitle.textContent = "Could not start FFCraft-1.5B";
      setupSubtitle.textContent = st.error || "Unknown error.";
      downloadModelBtn.classList.add("hidden");
      retryStartBtn.classList.remove("hidden");
    }
  }

  async function pollStatus() {
    try {
      const res = await fetch("/api/status");
      const st = await res.json();
      updateRuntimeUI(st);
      const delay = st.state === "ready" ? 5000 : 900;
      setTimeout(pollStatus, delay);
    } catch {
      setTimeout(pollStatus, 2500);
    }
  }

  // Event bindings
  promptInput.addEventListener("input", autoResizeTextarea);
  promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  sendBtn.addEventListener("click", () => sendMessage());
  newChatBtn.addEventListener("click", resetChat);

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      resetChat();
    }
  });

  document.querySelectorAll(".starter-chip").forEach((btn) => {
    btn.addEventListener("click", () => sendMessage(btn.dataset.prompt));
  });

  attachFileBtn.addEventListener("click", async () => {
    attachFileBtn.disabled = true;
    try {
      const res = await fetch("/api/file/pick", { method: "POST" });
      const data = await res.json();
      if (!data.cancelled) {
        if (data.error) alert(data.error);
        else renderAttachedFile(data.file);
      }
    } finally {
      attachFileBtn.disabled = false;
      promptInput.focus();
    }
  });

  pastePathToggleBtn.addEventListener("click", () => {
    pathBar.classList.toggle("hidden");
    if (!pathBar.classList.contains("hidden")) manualPathInput.focus();
  });
  closePathBtn.addEventListener("click", () => pathBar.classList.add("hidden"));

  async function probeManualPath() {
    const val = manualPathInput.value.trim();
    if (!val) return;
    probePathBtn.disabled = true;
    try {
      const res = await fetch("/api/file/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: val }),
      });
      const data = await res.json();
      if (!res.ok) alert(data.error || "Could not inspect file");
      else {
        renderAttachedFile(data.file);
        manualPathInput.value = "";
        pathBar.classList.add("hidden");
      }
    } finally {
      probePathBtn.disabled = false;
    }
  }
  probePathBtn.addEventListener("click", probeManualPath);
  manualPathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); probeManualPath(); }
  });

  detachBtn.addEventListener("click", () => renderAttachedFile(null));

  downloadModelBtn.addEventListener("click", async () => {
    downloadModelBtn.disabled = true;
    await fetch("/api/model/download", { method: "POST" });
    pollStatus();
  });

  retryStartBtn.addEventListener("click", async () => {
    await fetch("/api/model/start", { method: "POST" });
    pollStatus();
  });

  // Boot
  pollStatus();
  promptInput.focus();
})();
