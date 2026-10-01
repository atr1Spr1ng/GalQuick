"use strict";

const launchParams = new URLSearchParams(window.location.search);
const overlayToken = launchParams.get("overlay_token");
const launchedAsOverlay = launchParams.get("overlay") === "1";
if (overlayToken) document.title = `Scene Review · ${overlayToken}`;
if (launchedAsOverlay) document.documentElement.classList.add("overlay-client");

const state = {
  story: null,
  segments: new Map(),
  choices: new Map(),
  scenes: new Map(),
  outgoing: new Map(),
  session: { version: 1, actions: [] },
  storageKey: "",
  actionIndex: 0,
  blocked: null,
  dialogueCount: 0,
  renderedChoices: new Set(),
  renderedScenes: new Set(),
  runtime: null,
  overlayMode: launchedAsOverlay ? "full" : null,
  activeSceneId: null,
  pendingScene: null,
};

const colors = ["#79cdbd", "#ab91e8", "#e5ad69", "#75afe8"];
const novel = document.querySelector("#novel");
const toast = document.querySelector("#toast");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function domToken(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, character =>
    `_${character.codePointAt(0).toString(16)}_`
  );
}

function choiceDomId(id) {
  return `choice-${domToken(id)}`;
}

function sceneDomId(id) {
  return `scene-${domToken(id)}`;
}

function dialogueDomId(segmentId, offset) {
  return `line-${domToken(segmentId)}-${Number(offset)}`;
}

function storyPosition(order) {
  return `剧情 ${String(Number(order) + 1).padStart(3, "0")}`;
}

function publicSegmentLabel(label, order) {
  return /\.ws2$/i.test(label || "") ? storyPosition(order) : (label || storyPosition(order));
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 5200);
}

function scrollToId(id, behavior = "smooth") {
  const target = document.getElementById(id);
  if (!target) return false;
  target.scrollIntoView({ behavior, block: "start" });
  return true;
}

function loadSession() {
  state.storageKey = `scene-review-reader:${state.story.game.fingerprint}`;
  try {
    const value = JSON.parse(localStorage.getItem(state.storageKey) || "null");
    if (value?.version === 1 && Array.isArray(value.actions)) {
      state.session = {
        version: 1,
        actions: value.actions
          .filter(action => action && typeof action === "object")
          .slice(0, 512),
      };
    }
  } catch (_error) {
    state.session = { version: 1, actions: [] };
  }
}

function saveSession() {
  localStorage.setItem(state.storageKey, JSON.stringify(state.session));
}

function truncateSession(index) {
  if (state.session.actions.length === index) return;
  state.session.actions = state.session.actions.slice(0, index);
  saveSession();
}

function commitAction(index, action, anchorId) {
  state.session.actions = state.session.actions.slice(0, index);
  state.session.actions.push(action);
  saveSession();
  renderNovel(anchorId);
}

function recordedAction(type, id) {
  const action = state.session.actions[state.actionIndex];
  if (!action) return null;
  if (action.type === type && String(action.id) === String(id)) return action;
  truncateSession(state.actionIndex);
  return null;
}

function appendSegment(segment, startOffset, visits) {
  const count = visits.get(segment.id) || 0;
  visits.set(segment.id, count + 1);
  const section = element("section", "novel-section");
  section.dataset.segmentId = segment.id;

  const heading = element("header", "chapter-heading");
  const chapter = storyPosition(segment.order);
  heading.append(element("span", "chapter-number", count ? `${chapter} · 续` : chapter));
  if (!/\.ws2$/i.test(segment.label || "")) {
    heading.append(element("span", "chapter-label", segment.label));
  }
  if (Number.isFinite(startOffset)) {
    heading.title = `从脚本位置 ${startOffset} 继续`;
  }
  section.append(heading);
  novel.append(section);
  return section;
}

function renderDialogue(segment, event) {
  const paragraph = element("p", event.speaker ? "novel-line spoken" : "novel-line narration");
  paragraph.id = dialogueDomId(segment.id, event.offset);
  if (event.speaker) paragraph.append(element("strong", "novel-speaker", event.speaker));
  paragraph.append(element("span", "novel-text", event.text || ""));
  return paragraph;
}

function optionBranch(choice, optionId) {
  return choice?.branches?.find(
    branch => String(branch.option_id) === String(optionId)
  ) || null;
}

function renderChoiceGate(event, action, traceIndex) {
  const choice = state.choices.get(event.id);
  const gate = element("section", `route-gate${action ? " decided" : ""}`);
  gate.id = choiceDomId(event.id);
  gate.dataset.traceIndex = String(traceIndex);
  gate.append(element("div", "gate-kicker", action ? "ROUTE SELECTED" : "ROUTE DECISION"));
  gate.append(element("h2", "gate-title", action ? "已选择剧情路线" : "请选择接下来的剧情路线"));
  gate.append(element(
    "p",
    "gate-copy",
    action
      ? "已按这条路线继续展开小说；点击另一项可以从这里改走其他路线。"
      : "阅读在这里暂停。选择后，下一个分支的全部台词会继续铺在下方。"
  ));

  const options = element("div", "route-options");
  options.style.setProperty("--option-count", Math.min(event.options.length, 4));
  event.options.forEach((option, index) => {
    const selected = action && String(action.option_id) === String(option.id);
    const button = element("button", `route-option${selected ? " selected" : ""}`);
    button.style.setProperty("--route-color", colors[index % colors.length]);
    button.append(element("span", "option-index", `OPTION ${index + 1}`));
    button.append(element("strong", "option-text", option.text || "（无选项文本）"));

    const branch = optionBranch(choice, option.id);
    const path = element("span", "route-preview");
    if (branch?.nodes?.length) {
      for (const node of branch.nodes.slice(0, 8)) {
        const chip = element("span", "route-chip", publicSegmentLabel(node.label, node.order));
        chip.title = node.label;
        if (node.scene_ids?.length) {
          chip.append(element("em", "route-scenes", `Scene × ${node.scene_ids.length}`));
        }
        path.append(chip);
      }
      if (branch.nodes.length > 8) path.append(element("span", "route-chip", "…"));
    } else {
      path.append(element("span", "route-chip", choice?.merge ? "直接汇合" : "路线终点"));
    }
    button.append(path);
    button.addEventListener("click", () => {
      if (selected) return;
      commitAction(
        traceIndex,
        { type: "choice", id: event.id, option_id: option.id },
        choiceDomId(event.id),
      );
      showToast(`已选择：${option.text || "未命名选项"}`);
    });
    options.append(button);
  });
  gate.append(options);

  if (choice?.merge) {
    const mergeLabel = choice.merge_order === null || choice.merge_order === undefined
      ? "后续剧情"
      : storyPosition(choice.merge_order);
    gate.append(element("div", "merge-note", `分支将在 ${mergeLabel} 附近汇合`));
  }
  state.renderedChoices.add(event.id);
  return gate;
}

function renderSceneGate(event, scene, action, traceIndex) {
  const past = Boolean(action);
  const gate = element("section", `scene-gate${past ? " completed" : ""}`);
  gate.id = sceneDomId(event.id);
  gate.dataset.traceIndex = String(traceIndex);

  const number = element("div", "scene-number", String(scene.replay_key).padStart(2, "0"));
  const copy = element("div", "scene-copy");
  copy.append(element("div", "gate-kicker", past ? "SCENE COMPLETE" : "ORIGINAL GAME HANDOFF"));
  copy.append(element("h2", "gate-title", past ? `${scene.label} 已处理` : `到达 ${scene.label}`));
  if (past) {
    copy.append(element(
      "p",
      "gate-copy",
      action.status === "played"
        ? "这一段已交给原版游戏播放，小说从 Scene 结束位置继续。"
        : "这一段已跳过，小说从 Scene 结束位置继续。"
    ));
  } else {
    copy.append(element(
      "p",
      "gate-copy",
      "上方已经列出到这个 Scene 为止的普通剧情。现在可以切回原版游戏观看完整画面、声音和演出。"
    ));
  }

  const facts = element("div", "scene-facts");
  const speakers = scene.speakers?.length
    ? scene.speakers.slice(0, 8).join(" · ")
    : "角色未标注";
  facts.append(element("span", "", speakers));
  if (scene.metadata?.dialogue_count) {
    facts.append(element("span", "", `Scene 内 ${scene.metadata.dialogue_count} 条台词`));
  }
  copy.append(facts);

  const actions = element("div", "scene-actions");
  if (past) {
    const rewind = element("button", "secondary-button", "从这里重新处理");
    rewind.addEventListener("click", () => {
      state.session.actions = state.session.actions.slice(0, traceIndex);
      saveSession();
      renderNovel(sceneDomId(event.id));
    });
    actions.append(rewind);
  } else {
    const play = element("button", "play-button", "进入原版 Scene");
    if (!state.story.capabilities.scene_replay) {
      play.disabled = true;
      play.textContent = "当前引擎不支持原版回放";
    } else {
      play.addEventListener("click", () => playScene(event.id, play, traceIndex));
    }
    const skip = element("button", "secondary-button", "跳过 Scene，继续小说");
    skip.addEventListener("click", () => {
      commitAction(
        traceIndex,
        { type: "scene", id: event.id, status: "skipped" },
        sceneDomId(event.id),
      );
      showToast(`${scene.label} 已跳过，继续显示后续剧情。`);
    });
    actions.append(play, skip);
  }
  copy.append(actions);
  gate.append(number, copy);
  state.renderedScenes.add(event.id);
  return gate;
}

function appendEnding(message, error = false) {
  const ending = element("section", `story-ending${error ? " error" : ""}`);
  ending.append(element("div", "gate-kicker", error ? "ROUTE STOPPED" : "END OF ROUTE"));
  ending.append(element("h2", "gate-title", error ? "无法继续自动展开" : "当前路线已读完"));
  ending.append(element("p", "gate-copy", message));
  const restart = element("button", "secondary-button", "从头阅读");
  restart.addEventListener("click", restartReading);
  ending.append(restart);
  novel.append(ending);
  state.blocked = { type: error ? "error" : "ending", id: ending.id };
}

function nextSegment(segment, startOffset) {
  const edges = (state.outgoing.get(segment.id) || [])
    .filter(edge => edge.kind !== "choice" && state.segments.has(edge.target));
  if (!edges.length) return null;

  const afterCursor = edges.filter(edge => {
    const offset = Number(edge.evidence?.offset);
    return !Number.isFinite(startOffset) || !Number.isFinite(offset) || offset >= startOffset;
  });
  const candidates = afterCursor.length ? afterCursor : edges;
  candidates.sort((left, right) => {
    if (Boolean(left.preferred) !== Boolean(right.preferred)) return left.preferred ? -1 : 1;
    const leftSegment = state.segments.get(left.target);
    const rightSegment = state.segments.get(right.target);
    const leftForward = leftSegment.order > segment.order ? 0 : 1;
    const rightForward = rightSegment.order > segment.order ? 0 : 1;
    if (leftForward !== rightForward) return leftForward - rightForward;
    const leftDistance = Math.abs(leftSegment.order - segment.order);
    const rightDistance = Math.abs(rightSegment.order - segment.order);
    if (leftDistance !== rightDistance) return leftDistance - rightDistance;
    return left.target.localeCompare(right.target);
  });
  return candidates[0].target;
}

function renderNovel(anchorId = null) {
  novel.replaceChildren();
  state.actionIndex = 0;
  state.blocked = null;
  state.dialogueCount = 0;
  state.renderedChoices = new Set();
  state.renderedScenes = new Set();

  let segmentId = state.story.entry_segment;
  let startOffset = Number.NEGATIVE_INFINITY;
  const visits = new Map();
  const cursors = new Set();
  let steps = 0;

  routeLoop:
  while (steps++ < 4096) {
    const cursorKey = `${segmentId}@${startOffset}@${state.actionIndex}`;
    if (cursors.has(cursorKey)) {
      appendEnding("剧情图在当前位置形成循环，阅读器已停止以避免重复铺开文本。", true);
      break;
    }
    cursors.add(cursorKey);

    const segment = state.segments.get(segmentId);
    if (!segment) {
      appendEnding(`找不到剧情段：${segmentId}`, true);
      break;
    }
    const section = appendSegment(segment, startOffset, visits);
    const events = [...segment.events]
      .filter(event => Number(event.offset || 0) >= startOffset)
      .sort((left, right) => Number(left.offset || 0) - Number(right.offset || 0));

    for (const event of events) {
      if (event.kind === "dialogue") {
        section.append(renderDialogue(segment, event));
        state.dialogueCount += 1;
        continue;
      }

      if (event.kind === "choice") {
        let action = recordedAction("choice", event.id);
        let selectedOption = action
          ? event.options.find(option => String(option.id) === String(action.option_id))
          : null;
        if (action && !selectedOption) {
          truncateSession(state.actionIndex);
          action = null;
        }
        const traceIndex = state.actionIndex;
        section.append(renderChoiceGate(event, action, traceIndex));
        if (!action) {
          state.blocked = { type: "choice", id: event.id, domId: choiceDomId(event.id) };
          break routeLoop;
        }
        state.actionIndex += 1;
        segmentId = selectedOption.target;
        startOffset = Number.NEGATIVE_INFINITY;
        continue routeLoop;
      }

      if (event.kind === "scene") {
        const scene = state.scenes.get(event.id);
        if (!scene) continue;
        let action = recordedAction("scene", event.id);
        if (action && !["played", "skipped"].includes(action.status)) {
          truncateSession(state.actionIndex);
          action = null;
        }
        const traceIndex = state.actionIndex;
        section.append(renderSceneGate(event, scene, action, traceIndex));
        if (!action) {
          state.blocked = { type: "scene", id: event.id, domId: sceneDomId(event.id) };
          break routeLoop;
        }
        state.actionIndex += 1;
        if (!scene.resume_cursor) {
          appendEnding("这个 Scene 之后没有可继续的剧情位置。", false);
          break routeLoop;
        }
        segmentId = scene.resume_cursor.segment_id;
        startOffset = Number(scene.resume_cursor.offset);
        continue routeLoop;
      }
    }

    const target = nextSegment(segment, startOffset);
    if (!target) {
      appendEnding("没有更多可达的剧情段。", false);
      break;
    }
    segmentId = target;
    startOffset = Number.NEGATIVE_INFINITY;
  }

  if (steps >= 4096) {
    appendEnding("剧情路径过长，阅读器已在安全上限处停止。", true);
  }
  if (state.session.actions.length > state.actionIndex) truncateSession(state.actionIndex);
  updateReaderStatus();

  if (anchorId) {
    requestAnimationFrame(() => scrollToId(anchorId, "auto"));
  }
}

function updateReaderStatus() {
  const choices = state.session.actions.filter(action => action.type === "choice").length;
  const scenes = state.session.actions.filter(action => action.type === "scene").length;
  let waiting = "当前路线结束";
  if (state.blocked?.type === "choice") waiting = "等待选择路线";
  else if (state.blocked?.type === "scene") waiting = `等待进入 ${state.scenes.get(state.blocked.id)?.label || "Scene"}`;
  else if (state.blocked?.type === "error") waiting = "剧情路径需要检查";
  document.querySelector("#reader-status").textContent =
    `${state.dialogueCount} 条剧情已展开 · ${choices} 次选择 · ${scenes} 个 Scene 已处理 · ${waiting}`;
}

async function playScene(sceneId, button, traceIndex = null) {
  const scene = state.scenes.get(sceneId);
  if (!scene) return;
  const persistent = state.runtime?.playback?.kind === "persistent_scene_session";
  if (!persistent && !confirm(`将临时切换游戏脚本并用原版引擎播放 ${scene.label}。退出游戏后自动恢复并继续小说。继续吗？`)) return;

  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = persistent ? "正在切换到原版…" : "原版引擎运行中…";
  showToast(
    persistent
      ? `正在把已经运行的原版游戏切换到 ${scene.label}。`
      : "已启动原版游戏。播放结束后退出游戏，小说会从 Scene 后继续。",
    false,
  );
  try {
    const response = await fetch("/api/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "scene_replay", scene_id: sceneId }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "回放失败");
    state.activeSceneId = sceneId;
    if (persistent) {
      state.pendingScene = { id: sceneId, traceIndex };
      applyOverlayMode("compact");
      showToast(`${scene.label} 已交给原版游戏；看完后点击“返回阅读”。`, false);
    } else if (traceIndex !== null) {
      commitAction(
        traceIndex,
        { type: "scene", id: sceneId, status: "played" },
        sceneDomId(sceneId),
      );
    }
    if (!persistent) {
      showToast("原版 Scene 已结束，游戏文件已恢复，继续显示后续小说。", false);
    }
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function applyOverlayMode(mode) {
  if (!launchedAsOverlay || !["full", "compact", "hidden"].includes(mode)) return;
  state.overlayMode = mode;
  document.body.classList.toggle("overlay-full", mode === "full");
  document.body.classList.toggle("overlay-compact", mode === "compact");
  document.body.classList.toggle("overlay-hidden", mode === "hidden");
  const scene = state.activeSceneId ? state.scenes.get(state.activeSceneId) : null;
  document.querySelector("#compact-title").textContent = scene
    ? `${scene.label} 正在原版引擎中播放`
    : "原版游戏模式";
}

async function setOverlayMode(mode) {
  if (!launchedAsOverlay) return;
  try {
    const response = await fetch("/api/overlay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "set", mode }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "悬浮层切换失败");
    applyOverlayMode(payload.result.mode);
    if (payload.result.mode === "full" && state.pendingScene) {
      const pending = state.pendingScene;
      state.pendingScene = null;
      if (pending.traceIndex !== null) {
        commitAction(
          pending.traceIndex,
          { type: "scene", id: pending.id, status: "played" },
          sceneDomId(pending.id),
        );
      }
    }
  } catch (error) {
    showToast(error.message || String(error), true);
  }
}

async function refreshRuntime() {
  try {
    const response = await fetch("/api/runtime", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) return;
    state.runtime = payload.runtime;
    if (payload.runtime?.overlay?.mode) {
      applyOverlayMode(payload.runtime.overlay.mode);
    }
    const replayKey = payload.runtime?.playback?.last_scene;
    if (replayKey !== null && replayKey !== undefined) {
      const scene = state.story?.scenes?.find(
        item => String(item.replay_key) === String(replayKey)
      );
      if (scene) state.activeSceneId = scene.id;
    }
  } catch (_error) {
    // A transient poll failure during shutdown should not replace the story UI.
  }
}

function restartReading() {
  if (state.session.actions.length && !confirm("清除当前路线选择和 Scene 进度，从剧情开头重新阅读吗？")) return;
  state.session = { version: 1, actions: [] };
  saveSession();
  renderNovel();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openNavigator(type) {
  const panel = document.querySelector("#navigator");
  const list = document.querySelector("#navigator-list");
  const title = document.querySelector("#navigator-title");
  list.replaceChildren();

  if (type === "choices") {
    title.textContent = "路线选择节点";
    for (const choice of state.story.graph.choices) {
      const item = element("button", "nav-item");
      item.append(element("strong", "", choice.branches.map(branch => branch.text).join(" / ")));
      const source = state.segments.get(choice.source);
      item.append(element("span", "", source ? publicSegmentLabel(source.label, source.order) : choice.source));
      item.addEventListener("click", () => {
        closeNavigator();
        if (!scrollToId(choiceDomId(choice.id))) showToast("当前所选路线尚未经过这个选择节点。", true);
      });
      list.append(item);
    }
  } else {
    title.textContent = "Scene 节点";
    for (const scene of state.story.scenes) {
      const row = element("div", "nav-scene-row");
      const jump = element("button", "nav-item");
      jump.append(element("strong", "", scene.label));
      jump.append(element("span", "", scene.speakers.slice(0, 8).join(" · ") || "角色未标注"));
      jump.addEventListener("click", () => {
        closeNavigator();
        if (!scrollToId(sceneDomId(scene.id))) showToast("当前所选路线还没有到达这个 Scene。", true);
      });
      const play = element("button", "nav-play", "直接播放");
      play.disabled = !state.story.capabilities.scene_replay;
      play.addEventListener("click", () => playScene(scene.id, play, null));
      row.append(jump, play);
      list.append(row);
    }
  }
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
}

function closeNavigator() {
  const panel = document.querySelector("#navigator");
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
}

function searchStory(query) {
  const box = document.querySelector("#search-results");
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }

  const hits = [];
  for (const segment of state.story.segments) {
    for (const event of segment.events) {
      if (event.kind !== "dialogue") continue;
      const haystack = `${event.speaker || ""}\n${event.text || ""}`.toLocaleLowerCase();
      if (haystack.includes(normalized)) hits.push({ segment, event });
      if (hits.length >= 60) break;
    }
    if (hits.length >= 60) break;
  }

  box.replaceChildren();
  const heading = element("div", "search-heading");
  heading.append(element("h2", "", `搜索结果 · ${hits.length}${hits.length === 60 ? "+" : ""}`));
  const close = element("button", "icon-button", "×");
  close.addEventListener("click", () => {
    document.querySelector("#search").value = "";
    box.hidden = true;
  });
  heading.append(close);
  box.append(heading);

  for (const hit of hits) {
    const item = element("button", "search-result");
    item.append(element("small", "", `${hit.event.speaker || "旁白"} · ${publicSegmentLabel(hit.segment.label, hit.segment.order)}`));
    item.append(element("span", "", hit.event.text));
    item.addEventListener("click", () => {
      if (!scrollToId(dialogueDomId(hit.segment.id, hit.event.offset))) {
        showToast("这条台词不在当前已经选择并展开的路线中。", true);
      }
    });
    box.append(item);
  }
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function boot() {
  try {
    await refreshRuntime();
    const response = await fetch("/api/story", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.story = await response.json();
    state.segments = new Map(state.story.segments.map(segment => [segment.id, segment]));
    state.choices = new Map(state.story.graph.choices.map(choice => [choice.id, choice]));
    state.scenes = new Map(state.story.scenes.map(scene => [scene.id, scene]));
    for (const edge of state.story.graph.edges) {
      if (!state.outgoing.has(edge.source)) state.outgoing.set(edge.source, []);
      state.outgoing.get(edge.source).push(edge);
    }

    if (!overlayToken) document.title = `${state.story.game.title} · 剧情快速阅读`;
    document.querySelector("#game-title").textContent = state.story.game.title;
    document.querySelector("#choice-count").textContent = state.story.summary.choice_count;
    document.querySelector("#scene-count").textContent = state.story.summary.scene_count;
    const summary = document.querySelector("#summary");
    for (const [number, label] of [
      [state.story.summary.dialogue_count, "条剧情文字"],
      [state.story.summary.choice_count, "个路线选择"],
      [state.story.summary.scene_count, "个原版 Scene"],
    ]) {
      const item = element("span", "summary-item");
      item.append(element("strong", "", number));
      item.append(document.createTextNode(label));
      summary.append(item);
    }

    loadSession();
    const lastAction = state.session.actions.at(-1);
    const anchorId = lastAction
      ? (lastAction.type === "choice" ? choiceDomId(lastAction.id) : sceneDomId(lastAction.id))
      : null;
    renderNovel(anchorId);
    if (launchedAsOverlay) {
      document.body.classList.add("overlay-mode");
      applyOverlayMode(state.runtime?.overlay?.mode || "full");
      window.setInterval(refreshRuntime, 700);
    }
  } catch (error) {
    document.querySelector("#game-title").textContent = "剧情数据读取失败";
    document.querySelector("#reader-status").textContent = error.message || String(error);
    showToast(error.message || String(error), true);
  }
}

document.querySelector("#choices-button").addEventListener("click", () => openNavigator("choices"));
document.querySelector("#scenes-button").addEventListener("click", () => openNavigator("scenes"));
document.querySelector("#restart-button").addEventListener("click", restartReading);
document.querySelector("#navigator-close").addEventListener("click", closeNavigator);
document.querySelector("#navigator").addEventListener("click", event => {
  if (event.target.id === "navigator") closeNavigator();
});
document.querySelector("#search").addEventListener("input", event => searchStory(event.target.value));
document.querySelector("#game-toggle").addEventListener("click", () => setOverlayMode("compact"));
document.querySelector("#compact-return").addEventListener("click", () => setOverlayMode("full"));
window.addEventListener("scroll", () => {
  const max = document.documentElement.scrollHeight - innerHeight;
  document.querySelector("#progress").style.width = `${max > 0 ? scrollY / max * 100 : 0}%`;
}, { passive: true });

boot();
