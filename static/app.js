const STORAGE_KEY = "claude-web-session";

const state = {
  sessionId: null,
  isStreaming: false,
  isStopping: false,
  currentAssistantTurn: null,
  currentAbortController: null,
};

const chatLog = document.getElementById("chat-log");
const composer = document.getElementById("composer");
const messageInput = document.getElementById("message-input");
const sendButton = document.getElementById("send-btn");
const newChatButton = document.getElementById("new-chat-btn");
const sessionIdEl = document.getElementById("session-id");
const modelNameEl = document.getElementById("model-name");
const permissionModeEl = document.getElementById("permission-mode");
const skillsListEl = document.getElementById("skills-list");

function syncPrimaryAction() {
  if (state.isStreaming) {
    sendButton.textContent = state.isStopping ? "停止中..." : "停止";
    sendButton.classList.remove("primary-button");
    sendButton.classList.add("danger-button");
    sendButton.disabled = state.isStopping;
    sendButton.type = "button";
  } else {
    sendButton.textContent = "发送";
    sendButton.classList.remove("danger-button");
    sendButton.classList.add("primary-button");
    sendButton.disabled = false;
    sendButton.type = "submit";
  }
}

function setBusy(isBusy) {
  state.isStreaming = isBusy;
  if (!isBusy) {
    state.isStopping = false;
    state.currentAbortController = null;
  }
  messageInput.disabled = isBusy;
  syncPrimaryAction();
}

function removeEmptyState() {
  const empty = chatLog.querySelector(".empty-state");
  if (empty) {
    empty.remove();
  }
}

function clearChatLog() {
  chatLog.innerHTML = `
    <div class="empty-state">
      <h3>开始一个新对话</h3>
      <p>消息会以流式方式实时出现，工具调用和任务进度也会显示在消息下方。</p>
    </div>
  `;
}

function renderMarkdown(text) {
  if (!window.marked || !window.DOMPurify) {
    const escaped = document.createElement("div");
    escaped.textContent = text;
    return escaped.innerHTML;
  }

  marked.setOptions({
    breaks: true,
    gfm: true,
  });

  const rawHtml = marked.parse(text || "");
  return DOMPurify.sanitize(rawHtml);
}

function highlightCode(root) {
  if (!window.hljs) {
    return;
  }
  root.querySelectorAll("pre code").forEach((block) => {
    window.hljs.highlightElement(block);
  });
}

function setBubbleContent(messageObj, text) {
  messageObj.rawText = text;
  messageObj.bubble.innerHTML = renderMarkdown(text);
  highlightCode(messageObj.bubble);
}

function createMessage(role, text = "", metaEntries = []) {
  removeEmptyState();

  const wrapper = document.createElement("article");
  wrapper.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const meta = document.createElement("div");
  meta.className = "message-meta";

  wrapper.appendChild(bubble);
  wrapper.appendChild(meta);
  chatLog.appendChild(wrapper);

  const messageObj = {
    wrapper,
    bubble,
    meta,
    role,
    rawText: "",
    metaEntries: [],
  };

  setBubbleContent(messageObj, text);
  metaEntries.forEach((entry) => addMetaPill(messageObj, entry.text, entry.variant || ""));

  chatLog.scrollTop = chatLog.scrollHeight;
  return messageObj;
}

function addMetaPill(messageObj, text, variant = "") {
  const pill = document.createElement("span");
  pill.className = `meta-pill ${variant}`.trim();
  pill.textContent = text;
  messageObj.meta.appendChild(pill);
  messageObj.metaEntries.push({ text, variant });
  chatLog.scrollTop = chatLog.scrollHeight;
}

function ensureAssistantTurn() {
  if (!state.currentAssistantTurn) {
    state.currentAssistantTurn = createMessage("assistant", "");
  }
  return state.currentAssistantTurn;
}

function renderSessionMeta(data) {
  sessionIdEl.textContent = data.session_id;
  modelNameEl.textContent = data.model || "-";
  permissionModeEl.textContent = data.permission_mode || "-";

  skillsListEl.innerHTML = "";
  if (!data.skills || data.skills.length === 0) {
    const chip = document.createElement("span");
    chip.className = "chip muted";
    chip.textContent = "未发现额外 skills";
    skillsListEl.appendChild(chip);
    return;
  }

  for (const skill of data.skills) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = skill;
    skillsListEl.appendChild(chip);
  }
}

function saveSessionId(sessionId) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionId }));
}

function readSavedSessionId() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw);
    return parsed.sessionId || null;
  } catch {
    return null;
  }
}

function clearSavedSession() {
  localStorage.removeItem(STORAGE_KEY);
}

function renderHistory(history) {
  clearChatLog();
  if (!history || history.length === 0) {
    return;
  }

  chatLog.innerHTML = "";
  for (const item of history) {
    createMessage(item.role, item.content, item.meta || []);
  }
}

async function fetchSessionState(sessionId) {
  const response = await fetch(`/api/sessions/${sessionId}`);
  if (!response.ok) {
    throw new Error("会话不存在或已失效");
  }
  return response.json();
}

async function createSession() {
  const response = await fetch("/api/sessions", { method: "POST" });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`创建会话失败：${detail}`);
  }

  const data = await response.json();
  state.sessionId = data.session_id;
  renderSessionMeta(data);
  renderHistory([]);
  saveSessionId(data.session_id);
  return data;
}

async function restoreOrCreateSession() {
  const savedSessionId = readSavedSessionId();
  if (savedSessionId) {
    try {
      const data = await fetchSessionState(savedSessionId);
      state.sessionId = data.session_id;
      renderSessionMeta(data);
      renderHistory(data.history);
      return;
    } catch {
      clearSavedSession();
    }
  }

  await createSession();
}

async function ensureSession() {
  if (!state.sessionId) {
    await restoreOrCreateSession();
  }
}

async function resetSession() {
  if (state.isStreaming) {
    return;
  }

  if (state.sessionId) {
    await fetch(`/api/sessions/${state.sessionId}`, { method: "DELETE" }).catch(() => {});
  }

  state.sessionId = null;
  state.currentAssistantTurn = null;
  clearSavedSession();
  clearChatLog();
  sessionIdEl.textContent = "未创建";
  modelNameEl.textContent = "-";
  permissionModeEl.textContent = "-";
  skillsListEl.innerHTML = '<span class="chip muted">等待初始化</span>';
  await createSession();
}

async function interruptCurrentTurn() {
  if (!state.sessionId || !state.isStreaming || state.isStopping) {
    return;
  }

  state.isStopping = true;
  syncPrimaryAction();

  if (state.currentAbortController) {
    state.currentAbortController.abort();
  }

  await fetch(`/api/sessions/${state.sessionId}/interrupt`, { method: "POST" }).catch(() => {});
}

async function streamChat(message) {
  await ensureSession();

  createMessage("user", message);
  state.currentAssistantTurn = createMessage("assistant", "");
  state.currentAbortController = new AbortController();

  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: state.sessionId,
      message,
    }),
    signal: state.currentAbortController.signal,
  });

  if (!response.ok || !response.body) {
    const detail = await response.text();
    throw new Error(detail || "流式请求失败");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.trim()) {
        continue;
      }
      handleEvent(JSON.parse(line));
    }
  }

  if (buffer.trim()) {
    handleEvent(JSON.parse(buffer));
  }
}

function handleEvent(event) {
  const turn = ensureAssistantTurn();

  switch (event.type) {
    case "status":
      addMetaPill(turn, event.message);
      break;
    case "text_delta":
      setBubbleContent(turn, `${turn.rawText}${event.text}`);
      break;
    case "text":
      setBubbleContent(turn, `${turn.rawText}${event.text}`);
      break;
    case "tool_use":
      addMetaPill(turn, `工具 ${event.name}: ${event.preview}`);
      break;
    case "tool_result":
      addMetaPill(
        turn,
        `${event.is_error ? "工具报错" : "工具结果"}: ${event.preview}`,
        event.is_error ? "error" : ""
      );
      break;
    case "task_started":
      addMetaPill(turn, `任务开始: ${event.description}`);
      break;
    case "task_progress":
      addMetaPill(
        turn,
        `进行中: ${event.description}${event.last_tool_name ? ` · ${event.last_tool_name}` : ""}`
      );
      break;
    case "task_done":
      addMetaPill(
        turn,
        `任务${event.status === "completed" ? "完成" : event.status}: ${event.summary}`,
        event.status === "completed" ? "success" : ""
      );
      break;
    case "rate_limit":
      addMetaPill(turn, `限流状态: ${event.status}`, "error");
      break;
    case "result":
      if (event.is_error) {
        addMetaPill(turn, `执行失败: ${event.result || "未知错误"}`, "error");
      } else {
        addMetaPill(
          turn,
          `完成 · ${event.duration_ms} ms${event.cost_usd ? ` · $${event.cost_usd}` : ""}`,
          "success"
        );
      }
      break;
    case "error":
      addMetaPill(turn, event.message, "error");
      if (!turn.rawText) {
        setBubbleContent(turn, "请求失败，请查看错误信息。");
      }
      break;
    default:
      addMetaPill(turn, `事件: ${event.type}`);
      break;
  }

  chatLog.scrollTop = chatLog.scrollHeight;
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();

  if (state.isStreaming) {
    await interruptCurrentTurn();
    return;
  }

  const message = messageInput.value.trim();
  if (!message) {
    return;
  }

  messageInput.value = "";
  setBusy(true);

  try {
    await streamChat(message);
  } catch (error) {
    if (error.name === "AbortError") {
      const turn = ensureAssistantTurn();
      addMetaPill(turn, "已手动停止", "error");
      if (!turn.rawText) {
        setBubbleContent(turn, "已手动停止当前响应。");
      }
    } else {
      const turn = ensureAssistantTurn();
      addMetaPill(turn, error.message || "请求失败", "error");
      if (!turn.rawText) {
        setBubbleContent(turn, "请求失败，请稍后重试。");
      }
    }
  } finally {
    setBusy(false);
    state.currentAssistantTurn = null;
    saveSessionId(state.sessionId);
    messageInput.focus();
  }
});

messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    if (state.isStreaming) {
      interruptCurrentTurn();
    } else {
      composer.requestSubmit();
    }
  }
});

sendButton.addEventListener("click", async () => {
  if (state.isStreaming) {
    await interruptCurrentTurn();
  }
});

newChatButton.addEventListener("click", async () => {
  await resetSession();
});

document.querySelectorAll(".prompt-chip").forEach((button) => {
  button.addEventListener("click", () => {
    messageInput.value = button.dataset.prompt || "";
    messageInput.focus();
  });
});

window.addEventListener("load", async () => {
  syncPrimaryAction();
  try {
    await restoreOrCreateSession();
  } catch (error) {
    const turn = createMessage("assistant", "初始化会话失败，请检查 Claude Code 登录态或本地配置。");
    addMetaPill(turn, error.message || "初始化失败", "error");
  }
});
