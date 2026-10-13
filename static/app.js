const $ = (id) => document.getElementById(id);

const state = {
  ready: false,
  mode: "image",
  hasImage: false,
  name: "",
  width: 0,
  height: 0,
  fileCount: 0,
  folderReady: false,
  model: "realesrgan-x4plus",
  sizeMode: "scale",
  scale: 4,
  paper: "A3",
  customCm: 50,
  dpi: 300,
  format: "png",
  quality: 95,
  tile: 0,
  gpu: "",
  tta: false,
  outputDir: "",
  customModels: "",
  serviceUrl: "",
  running: false,
  allows: false,
  email: "",
  route: "checking",
  placeGpu: "",
  booted: false,
};

function payload() {
  return {
    mode: state.mode,
    model: state.model,
    sizeMode: state.sizeMode,
    scale: state.scale,
    paper: state.paper,
    customCm: Number(state.customCm) || 50,
    dpi: Number(state.dpi) || 300,
    format: state.format,
    quality: Number(state.quality) || 95,
    tile: Number(state.tile) || 0,
    gpu: state.gpu.trim(),
    tta: state.tta,
    outputDir: state.outputDir.trim(),
    customModels: state.customModels.trim(),
    serviceUrl: state.serviceUrl.trim(),
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Não consegui concluir isso.");
  return data;
}

function notice(text, tone) {
  const node = $("notice");
  node.hidden = !text;
  node.textContent = text || "";
  node.classList.toggle("calm", tone === "calm");
}

function formatDate(iso) {
  if (!iso || !iso.includes("-")) return "";
  const [year, month, day] = iso.split("-");
  return `${day}/${month}/${year}`;
}

function planLabel(sub) {
  if (!sub) return "";
  const date = formatDate(sub.until);
  if (sub.status === "trial") return date ? `Avaliação até ${date}` : "Avaliação";
  if (sub.status === "active") return date ? `Plano ativo até ${date}` : "Plano ativo";
  return "Mensalidade encerrada";
}

function placeCopy(route, gpu) {
  if (route === "service") {
    return {
      engine: "Real-ESRGAN no serviço. A imagem sai deste computador só para o aumento.",
      sub: "A imagem sai deste computador só para o aumento e o arquivo volta para a sua pasta.",
    };
  }
  if (route === "local") {
    return {
      engine: gpu ? `Real-ESRGAN neste computador, ${gpu}.` : "Real-ESRGAN neste computador.",
      sub: "O aumento acontece neste computador.",
    };
  }
  if (route === "checking") {
    return {
      engine: "Verificando este computador…",
      sub: "Verificando este computador…",
    };
  }
  return {
    engine: "Este computador não amplia sozinho. O serviço ainda não está ligado.",
    sub: "Este computador não amplia sozinho. O serviço ainda não está ligado.",
  };
}

function applyPlace() {
  const copy = placeCopy(state.route, state.placeGpu);
  $("engine-line").textContent = copy.engine;
  if (state.running) return;
  if (!$("compare").hidden) return;
  $("subcrumb").textContent = copy.sub;
}

function applyAccount(me) {
  state.email = me.user?.email || "";
  state.allows = Boolean(me.subscription?.allows);
  state.subscription = me.subscription || null;
  if (me.route) state.route = me.route;
  if (me.gpu) state.placeGpu = me.gpu;
  $("account").hidden = !me.user;
  $("account-email").textContent = state.email;
  $("account-plan").textContent = planLabel(me.subscription);
  applyPlace();
  updateGo();
  if (me.user && !state.allows) {
    notice("A mensalidade desta conta terminou. Renove para continuar ampliando.");
  }
}

function setGate(open, mode) {
  $("gate").hidden = !open;
  document.querySelector("aside").inert = open;
  document.querySelector("main").inert = open;
  if (!open) return;
  const register = mode === "register";
  $("gate-title").textContent = register ? "Criar conta" : "Entrar";
  $("gate-submit").textContent = register ? "Criar conta" : "Entrar";
  $("gate-switch").textContent = register ? "Já tenho conta" : "Criar conta";
  $("gate-password").autocomplete = register ? "new-password" : "current-password";
  $("gate").dataset.mode = register ? "register" : "login";
}

async function showBilling() {
  const data = await api("/api/billing/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  notice(data.message, "calm");
}

function renderModels(models) {
  const box = $("models");
  box.innerHTML = "";
  models.forEach((model) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "model" + (model.id === state.model ? " on" : "");
    button.innerHTML = `<strong></strong><span></span>`;
    button.querySelector("strong").textContent = model.name;
    button.querySelector("span").textContent = model.detail;
    button.addEventListener("click", () => {
      state.model = model.id;
      renderModels(models);
      refreshPlan();
    });
    box.appendChild(button);
  });
  state.modelButtons = models;
}

function setPressed(selector, attr, value) {
  document.querySelectorAll(selector).forEach((button) => {
    button.classList.toggle("on", button.dataset[attr] === String(value));
  });
}

function showImage() {
  $("empty").hidden = true;
  $("compare").hidden = true;
  $("single").hidden = false;
  $("single-img").src = "/media/original.jpg?t=" + Date.now();
  $("crumb").textContent = state.name;
  const count = state.mode === "folder" ? ` · ${state.fileCount} imagens` : "";
  $("subcrumb").textContent = `${state.width} × ${state.height} px${count}`;
}

function showCompare() {
  const stamp = Date.now();
  $("empty").hidden = true;
  $("single").hidden = true;
  $("compare").hidden = false;
  $("before").src = "/media/original.jpg?t=" + stamp;
  $("after").src = "/media/result.jpg?t=" + stamp;
  $("open-file").hidden = false;
  $("open-folder").hidden = false;
  requestAnimationFrame(layoutWipe);
}

function layoutWipe() {
  const box = $("compare");
  const before = $("before");
  if (box.hidden) return;
  before.style.width = box.clientWidth + "px";
  before.style.height = box.clientHeight + "px";
  setWipe($("wipe").value);
}

function setWipe(value) {
  const percent = Number(value) / 10;
  $("clip").style.width = percent + "%";
  $("handle").style.left = percent + "%";
}

function updateGo() {
  const button = $("go");
  if (state.email && !state.allows && !state.running) {
    button.disabled = false;
    button.classList.remove("stop");
    button.textContent = "Renovar";
    return;
  }
  const sourceReady = state.mode === "image" ? state.hasImage : state.folderReady;
  const engineOk = state.ready || state.route === "service";
  button.disabled = state.running ? false : !(engineOk && sourceReady);
  button.classList.toggle("stop", state.running);
  button.textContent = state.running ? "Parar" : state.sizeMode === "print" ? "Preparar impressão" : "Ampliar";
}

function setProgress(visible, value, label) {
  $("meter").hidden = !visible;
  $("overlay").hidden = !visible;
  $("meter-bar").style.width = value + "%";
  $("overlay-bar").style.width = value + "%";
  $("meter-label").textContent = label || "";
  $("overlay-label").textContent = label || "";
}

let planTimer = 0;
function refreshPlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(async () => {
    if (!state.hasImage) {
      $("forecast").hidden = true;
      return;
    }
    try {
      const plan = await api("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload()),
      });
      $("forecast").hidden = false;
      $("forecast").classList.toggle("warn", plan.warn);
      $("forecast-px").textContent = `${plan.outWidth} × ${plan.outHeight} px`;
      $("forecast-cm").textContent = `${plan.cmWidth.toLocaleString("pt-BR")} × ${plan.cmHeight.toLocaleString("pt-BR")} cm no papel · ${plan.embedDpi} DPI`;
      $("forecast-note").textContent = plan.note;
      $("file-line").textContent = plan.passCount > 1 ? `${plan.passCount} passagens do modelo` : "";
    } catch (error) {
      $("forecast").hidden = true;
      notice(error.message);
    }
  }, 180);
}

function applyImage(data) {
  state.hasImage = true;
  state.mode = "image";
  state.name = data.name;
  state.width = data.width;
  state.height = data.height;
  state.fileCount = 1;
  setPressed("[data-mode]", "mode", "image");
  $("pick-image-row").hidden = false;
  $("pick-folder-row").hidden = true;
  $("image-name").textContent = data.name;
  $("open-file").hidden = true;
  showImage();
  notice("");
  updateGo();
  refreshPlan();
}

async function uploadFile(file) {
  notice("");
  $("subcrumb").textContent = "Lendo a imagem…";
  const data = await api("/api/upload", {
    method: "POST",
    headers: { "X-Filename": encodeURIComponent(file.name) },
    body: file,
  });
  applyImage(data);
}

let pollTimer = 0;
function startPoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const status = await api("/api/status");
    setProgress(status.status === "running", status.progress, status.message);
    if (status.logs?.length) $("log").textContent = status.logs.join("\n");
    if (status.route) state.route = status.route;
    if (status.gpu) state.placeGpu = status.gpu;
    if (status.ready) state.ready = true;
    $("engine-line").textContent = placeCopy(state.route, state.placeGpu).engine;
    if (status.status === "running") return;
    clearInterval(pollTimer);
    state.running = false;
    updateGo();
    setProgress(false, 0, "");
    if (status.status === "done") {
      if (status.resultReady) showCompare();
      $("subcrumb").textContent = "Prévia na tela. O arquivo salvo está na resolução cheia.";
      $("file-line").textContent = status.outputs?.[0] ? status.outputs[status.outputs.length - 1] : "";
      notice("");
    } else if (status.status === "error") {
      notice(status.error || "O aumento falhou.");
    } else if (status.status === "stopped") {
      notice("Interrompido. A imagem parcial não foi mantida.");
    }
  }, 400);
}

function bindModes() {
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      state.mode = button.dataset.mode;
      setPressed("[data-mode]", "mode", state.mode);
      $("pick-image-row").hidden = state.mode !== "image";
      $("pick-folder-row").hidden = state.mode !== "folder";
      updateGo();
      refreshPlan();
    });
  });
  document.querySelectorAll("[data-size]").forEach((button) => {
    button.addEventListener("click", () => {
      state.sizeMode = button.dataset.size;
      setPressed("[data-size]", "size", state.sizeMode);
      $("scale-row").hidden = state.sizeMode !== "scale";
      $("print-row").hidden = state.sizeMode !== "print";
      updateGo();
      refreshPlan();
    });
  });
  document.querySelectorAll("[data-scale]").forEach((button) => {
    button.addEventListener("click", () => {
      state.scale = Number(button.dataset.scale);
      setPressed("[data-scale]", "scale", state.scale);
      refreshPlan();
    });
  });
  document.querySelectorAll("[data-paper]").forEach((button) => {
    button.addEventListener("click", () => {
      state.paper = button.dataset.paper;
      setPressed("[data-paper]", "paper", state.paper);
      $("custom-wrap").hidden = state.paper !== "custom";
      refreshPlan();
    });
  });
  document.querySelectorAll("[data-format]").forEach((button) => {
    button.addEventListener("click", () => {
      state.format = button.dataset.format;
      setPressed("[data-format]", "format", state.format);
      $("quality-wrap").hidden = state.format === "png";
      $("format-hint").textContent =
        state.format === "png"
          ? "PNG guarda o detalhe. É o arquivo certo para imprimir."
          : "Esse formato comprime o arquivo. Para gráfica, prefira PNG.";
      refreshPlan();
    });
  });
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("on", item === button));
      $("panel-work").hidden = button.dataset.tab !== "work";
      $("panel-settings").hidden = button.dataset.tab !== "settings";
    });
  });
}

async function watchRoute() {
  for (let attempt = 0; attempt < 80 && state.route === "checking"; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    const status = await api("/api/status");
    if (status.route) state.route = status.route;
    if (status.gpu) state.placeGpu = status.gpu;
    if (status.ready) state.ready = true;
    applyPlace();
    updateGo();
  }
}

async function bootApp() {
  if (state.booted) {
    applyPlace();
    updateGo();
    return;
  }
  const data = await api("/api/bootstrap");
  state.ready = data.ready;
  state.outputDir = data.settings.outputDir || "";
  state.customModels = data.settings.customModels || "";
  state.serviceUrl = data.settings.serviceUrl || "";
  state.model = data.settings.model || state.model;
  state.tile = data.settings.tile ?? 0;
  state.gpu = data.settings.gpu || "";
  state.dpi = data.settings.dpi || 300;
  state.format = data.settings.format || "png";
  state.quality = data.settings.quality || 95;
  state.tta = Boolean(data.settings.tta);
  state.scale = data.settings.scale || 4;
  state.paper = data.settings.paper || "A3";
  state.customCm = data.settings.customCm || 50;
  state.sizeMode = data.settings.sizeMode || "scale";
  $("output-dir").value = state.outputDir;
  $("custom-models").value = state.customModels;
  $("service-url").value = state.serviceUrl;
  $("gpu").value = state.gpu;
  $("tile").value = String(state.tile);
  $("dpi").value = String(state.dpi);
  $("quality").value = String(state.quality);
  $("quality-value").textContent = String(state.quality);
  $("tta").checked = state.tta;
  $("custom-cm").value = String(state.customCm);
  setPressed("[data-scale]", "scale", state.scale);
  setPressed("[data-paper]", "paper", state.paper);
  setPressed("[data-size]", "size", state.sizeMode);
  setPressed("[data-format]", "format", state.format);
  $("scale-row").hidden = state.sizeMode !== "scale";
  $("print-row").hidden = state.sizeMode !== "print";
  $("custom-wrap").hidden = state.paper !== "custom";
  $("quality-wrap").hidden = state.format === "png";
  if (data.route) state.route = data.route;
  if (data.gpu) state.placeGpu = data.gpu;
  renderModels(data.models || []);
  applyPlace();
  if (!data.ready && data.engineError && state.route !== "service") {
    notice(data.engineError);
  }
  if (data.image && data.image.name) applyImage(data.image);
  state.booted = true;
  updateGo();
  watchRoute().catch(() => {});
}

async function boot() {
  const me = await api("/api/me");
  applyAccount(me);
  if (!me.user) {
    setGate(true, "login");
    return;
  }
  setGate(false);
  await bootApp();
}

bindModes();
$("pick-image").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", () => {
  const file = $("file-input").files?.[0];
  if (file) uploadFile(file).catch((error) => notice(error.message));
});
$("pick-folder").addEventListener("click", async () => {
  try {
    const data = await api("/api/pick-folder", { method: "POST" });
    if (data.cancelled) return;
    state.hasImage = true;
    state.mode = "folder";
    state.name = data.name;
    state.width = data.width;
    state.height = data.height;
    state.fileCount = data.fileCount;
    state.folderReady = true;
    $("folder-name").textContent = `${data.fileCount} imagens · ${data.folder}`;
    showImage();
    updateGo();
    refreshPlan();
  } catch (error) {
    notice(error.message);
  }
});
$("pick-output").addEventListener("click", async () => {
  try {
    const data = await api("/api/pick-output", { method: "POST" });
    if (!data.cancelled && data.outputDir) {
      state.outputDir = data.outputDir;
      $("output-dir").value = data.outputDir;
    }
  } catch (error) {
    notice(error.message);
  }
});
$("pick-models").addEventListener("click", async () => {
  try {
    const data = await api("/api/pick-models", { method: "POST" });
    if (data.cancelled) return;
    state.customModels = data.customModels;
    $("custom-models").value = data.customModels;
    renderModels(data.models || []);
  } catch (error) {
    notice(error.message);
  }
});
$("output-dir").addEventListener("change", () => {
  state.outputDir = $("output-dir").value;
});
$("service-url").addEventListener("change", async () => {
  state.serviceUrl = $("service-url").value.trim();
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ serviceUrl: state.serviceUrl }),
    });
    applyAccount(await api("/api/me"));
  } catch (error) {
    notice(error.message);
  }
});
$("custom-models").addEventListener("change", async () => {
  state.customModels = $("custom-models").value;
  const data = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customModels: state.customModels }),
  });
  renderModels(data.models || []);
});
$("dpi").addEventListener("change", () => {
  state.dpi = Number($("dpi").value);
  refreshPlan();
});
$("custom-cm").addEventListener("input", () => {
  state.customCm = Number($("custom-cm").value);
  refreshPlan();
});
$("tile").addEventListener("change", () => {
  state.tile = Number($("tile").value);
  refreshPlan();
});
$("gpu").addEventListener("change", () => {
  state.gpu = $("gpu").value;
});
$("tta").addEventListener("change", () => {
  state.tta = $("tta").checked;
});
$("quality").addEventListener("input", () => {
  state.quality = Number($("quality").value);
  $("quality-value").textContent = String(state.quality);
});
$("wipe").addEventListener("input", () => setWipe($("wipe").value));
window.addEventListener("resize", layoutWipe);
$("before").addEventListener("load", layoutWipe);
$("after").addEventListener("load", layoutWipe);

$("go").addEventListener("click", async () => {
  notice("");
  if (state.running) {
    await api("/api/stop", { method: "POST" });
    return;
  }
  if (state.email && !state.allows) {
    try {
      await showBilling();
    } catch (error) {
      notice(error.message);
    }
    return;
  }
  try {
    state.running = true;
    updateGo();
    setProgress(true, 2, "Começando");
    await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    startPoll();
  } catch (error) {
    state.running = false;
    updateGo();
    setProgress(false, 0, "");
    notice(error.message);
  }
});

$("open-file").addEventListener("click", () => api("/api/reveal", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ target: "file" }),
}));
$("open-folder").addEventListener("click", () => api("/api/reveal", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ target: "folder" }),
}));

$("gate-switch").addEventListener("click", () => {
  const next = $("gate").dataset.mode === "register" ? "login" : "register";
  $("gate-error").hidden = true;
  setGate(true, next);
});
$("gate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("gate-error").hidden = true;
  $("gate-submit").disabled = true;
  try {
    const path = $("gate").dataset.mode === "register" ? "/api/register" : "/api/login";
    const me = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("gate-email").value.trim(),
        password: $("gate-password").value,
      }),
    });
    $("gate-password").value = "";
    notice("");
    applyAccount(me);
    setGate(false);
    await bootApp();
  } catch (error) {
    $("gate-error").hidden = false;
    $("gate-error").textContent = error.message;
  } finally {
    $("gate-submit").disabled = false;
  }
});
$("logout").addEventListener("click", async () => {
  try {
    if (state.running) await api("/api/stop", { method: "POST" });
    await api("/api/logout", { method: "POST" });
  } catch (error) {
    notice(error.message);
  }
  state.running = false;
  state.allows = false;
  state.email = "";
  $("account").hidden = true;
  $("account-email").textContent = "";
  $("account-plan").textContent = "";
  setProgress(false, 0, "");
  setGate(true, "login");
  updateGo();
});
$("account-pay").addEventListener("click", () => {
  showBilling().catch((error) => notice(error.message));
});

["dragenter", "dragover"].forEach((name) => {
  document.addEventListener(name, (event) => {
    event.preventDefault();
    $("stage").classList.add("hot");
  });
});
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) $("stage").classList.remove("hot");
});
document.addEventListener("drop", (event) => {
  event.preventDefault();
  $("stage").classList.remove("hot");
  const file = event.dataTransfer?.files?.[0];
  if (file) uploadFile(file).catch((error) => notice(error.message));
});

boot().catch((error) => {
  setGate(true, "login");
  $("gate-error").hidden = false;
  $("gate-error").textContent = error.message;
});
