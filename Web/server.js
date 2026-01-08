// server.js (multiuser v3 - per-project, no passwords)
const net = require("net");
const http = require("http");
const https = require("https");
const express = require("express");
const WebSocket = require("ws");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");

const WEB_PORT = 3000;   // web + ws
const TCP_PORT = 7071;   // tcp from ReaScript

// ---- persistence ----
const DATA_PATH = path.join(__dirname, "rm_projects.json");

function loadProjects(){
  try{
    const raw = fs.readFileSync(DATA_PATH, "utf8");
    const obj = JSON.parse(raw);
    return (obj && typeof obj === "object") ? obj : {};
  }catch{
    return {};
  }
}
function saveProjects(){
  try{
    fs.writeFileSync(DATA_PATH, JSON.stringify(projects, null, 2), "utf8");
  }catch(e){
    console.error("saveProjects failed:", e && e.message ? e.message : e);
  }
}
function sha1(s){
  return crypto.createHash("sha1").update(String(s||"")).digest("hex");
}
function getProjectId(projectPath, projectName){
  const key = (projectPath && projectPath.length) ? projectPath : ("UNSAVED:" + (projectName||"Untitled"));
  return sha1(key);
}

let projects = loadProjects();

// current project
let currentProjectId = null;
let currentProjectName = "Untitled";
let currentProjectPath = "";

// ensure config exists for project
function ensureProjectCfg(pid){
  if (!projects[pid]){
    projects[pid] = {
      projectId: pid,
      projectName: currentProjectName || "Untitled",
      users: ["main","mon1","mon2"],
      admin: "main",
      assignments: { main: {all:true, guids:[]}, mon1: {all:false, guids:[]}, mon2: {all:false, guids:[]} },
      ui: { showColorFooter: true, footerIntensity: 0.35 }
    };
    saveProjects();
  }
  // backfill keys for older configs
  const cfg = projects[pid];
  if (!cfg.users) cfg.users = ["main","mon1","mon2"];
  if (!cfg.admin) cfg.admin = "main";
  if (!cfg.assignments) cfg.assignments = { main:{all:true, guids:[]}, mon1:{all:false, guids:[]}, mon2:{all:false, guids:[]} };
  if (!cfg.ui) cfg.ui = { showColorFooter: true, footerIntensity: 0.35 };
  if (typeof cfg.ui.showColorFooter !== "boolean") cfg.ui.showColorFooter = true;
  return cfg;
}

// ---- HTTP(S) static ----
// Use absolute path so running from a different CWD still serves the UI.
const app = express();
const PUBLIC_DIR = path.join(__dirname, "public");
app.use(express.static(PUBLIC_DIR));

// Explicitly serve index at `/` (guards against "Cannot GET /" if static
// resolution fails due to CWD issues or reverse proxies).
app.get("/", (req, res) => {
  res.sendFile(path.join(PUBLIC_DIR, "index.html"));
});

function startWebServer(){
  const forceHttp = process.env.RM_FORCE_HTTP === "1";
  const keyPath = path.join(__dirname, "ssl", "key.pem");
  const certPath = path.join(__dirname, "ssl", "cert.pem");

  if (!forceHttp && fs.existsSync(keyPath) && fs.existsSync(certPath)){
    try{
      const opts = {
        key: fs.readFileSync(keyPath),
        cert: fs.readFileSync(certPath)
      };
      const srv = https.createServer(opts, app).listen(WEB_PORT, "0.0.0.0", () => {
        console.log(`Web UI (HTTPS): https://<MAC_IP>:${WEB_PORT}`);
      });
      return srv;
    }catch(e){
      console.warn("HTTPS cert/key found but failed to start HTTPS, falling back to HTTP:", e && e.message ? e.message : e);
    }
  }

  const srv = http.createServer(app).listen(WEB_PORT, "0.0.0.0", () => {
    console.log(`Web UI: http://<MAC_IP>:${WEB_PORT}`);
    console.log("Tip: For Android 'Install app' prompt you need HTTPS (trusted cert) or Chrome flag 'treat insecure origin as secure'.");
  });
  return srv;
}

const httpServer = startWebServer();

// ---- WS ----
const wss = new WebSocket.Server({ server: httpServer, path: "/ws" });
const wsClients = new Set();

function sendTo(ws, obj){
  try{
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }catch{}
}

function broadcastProjectInfo(){
  if (!currentProjectId) return;
  const cfg = ensureProjectCfg(currentProjectId);
  const payload = {
    type: "projectInfo",
    projectId: currentProjectId,
    projectName: currentProjectName,
    users: cfg.users,
    admin: cfg.admin,
    ui: cfg.ui,
    // for admin UI convenience (just guids list)
    assignments: {
      mon1: cfg.assignments.mon1 ? cfg.assignments.mon1.guids : [],
      mon2: cfg.assignments.mon2 ? cfg.assignments.mon2.guids : []
    }
  };
  for (const ws of wsClients) sendTo(ws, payload);
}

function allowedGuidsFor(user, cfg){
  if (!user) return new Set();
  if (user === cfg.admin || user === "main" || (cfg.assignments[user] && cfg.assignments[user].all)) return null; // null = all
  const g = (cfg.assignments[user] && Array.isArray(cfg.assignments[user].guids)) ? cfg.assignments[user].guids : [];
  return new Set(g);
}

// include parent folders for allowed tracks
function expandWithParents(tracks, allowedSet){
  if (!allowedSet) return null; // all
  const parentsByDepth = [];
  const include = new Set(allowedSet);
  for (const t of tracks){
    const d = Number(t.indent||0);
    parentsByDepth.length = d;
    // current top-of-stack is parent for this depth
    // If this track is included and has parents, include parents
    if (include.has(t.guid)){
      for (const p of parentsByDepth) include.add(p.guid);
    }
    // if this is folderStart, push as potential parent for next depth
    if (Number(t.folderDepth||0) > 0){
      parentsByDepth[d] = { guid: t.guid };
    }
  }
  return include;
}

function filterStateFor(ws, state){
  if (!currentProjectId) return state;
  const cfg = ensureProjectCfg(currentProjectId);
  const user = ws.user;
  if (!user){
    // not selected yet: send nothing track-wise
    return { type:"state", master:null, tracks:[], projectName: state.projectName, projectPath: state.projectPath, transport: state.transport, ts: state.ts, version: state.version };
  }
  if (user === cfg.admin || user === "main" || (cfg.assignments[user] && cfg.assignments[user].all)){
    return state;
  }
  const allowed = allowedGuidsFor(user, cfg);
  const expanded = expandWithParents(state.tracks||[], allowed);
  const tracks = (state.tracks||[]).filter(t=> expanded && expanded.has(t.guid));
  return { ...state, master: null, tracks };
}

function filterMeterFor(ws, meter){
  if (!currentProjectId) return meter;
  const cfg = ensureProjectCfg(currentProjectId);
  const user = ws.user;
  if (!user) return { ...meter, frames: [] };
  if (user === cfg.admin || user === "main" || (cfg.assignments[user] && cfg.assignments[user].all)) return meter;
  const allowed = allowedGuidsFor(user, cfg);
  const frames = (meter.frames||[]).filter(f => allowed.has(f.guid));
  return { ...meter, frames };
}

function canControl(ws, guid){
  if (!currentProjectId) return false;
  const cfg = ensureProjectCfg(currentProjectId);
  const user = ws.user;
  if (!user) return false;
  if (user === cfg.admin || user === "main" || (cfg.assignments[user] && cfg.assignments[user].all)) return true;
  const allowed = allowedGuidsFor(user, cfg);
  return allowed.has(guid);
}

let lastState = null;
let lastMeter = null;

wss.on("connection", (ws) => {
  ws.user = null;
  wsClients.add(ws);

  if (currentProjectId) sendTo(ws, {
    type:"projectInfo",
    projectId: currentProjectId,
    projectName: currentProjectName,
    users: ensureProjectCfg(currentProjectId).users,
    admin: ensureProjectCfg(currentProjectId).admin,
    ui: ensureProjectCfg(currentProjectId).ui,
    assignments: {
      mon1: ensureProjectCfg(currentProjectId).assignments.mon1.guids,
      mon2: ensureProjectCfg(currentProjectId).assignments.mon2.guids
    }
  });

  ws.on("message", (buf) => {
    let msg = null;
    try{ msg = JSON.parse(String(buf)); }catch{ return; }
    if (!msg || !msg.type) return;

    if (msg.type === "reqProjectInfo"){
      broadcastProjectInfo();
      return;
    }
    if (msg.type === "setUser"){
      ws.user = String(msg.user||"");
      sendTo(ws, {type:"user", user: ws.user});
      // send latest state/meter filtered
      if (lastState) sendTo(ws, filterStateFor(ws, lastState));
      if (lastMeter) sendTo(ws, filterMeterFor(ws, lastMeter));
      return;
    }
    if (msg.type === "reqRegions"){
      if (lastState && lastState.transport){
        const t = lastState.transport;
        sendTo(ws, {
          type: "regions",
          regions: Array.isArray(t.regions) ? t.regions : [],
          markers: Array.isArray(t.markers) ? t.markers : [],
          regionName: t.regionName || "",
          regionIndex: Number.isFinite(t.regionIndex) ? t.regionIndex : null
        });
      }
      if (reaperSock){
        try{ reaperSock.write(JSON.stringify(msg) + "\n"); }catch{}
      }
      return;
    }
    if (msg.type === "reqState"){
      if (lastState) sendTo(ws, filterStateFor(ws, lastState));
      return;
    }
    if (msg.type === "setUi"){
      if (!currentProjectId) return;
      const cfg = ensureProjectCfg(currentProjectId);
      // only admin can change project-wide ui settings
      if (ws.user !== cfg.admin) return;
      if (msg.ui && typeof msg.ui === "object"){
      if (typeof msg.ui.showColorFooter === "boolean"){
        cfg.ui.showColorFooter = msg.ui.showColorFooter;
      }
      if (typeof msg.ui.footerIntensity === "number"){
        const v = msg.ui.footerIntensity;
        // allow only the supported steps
        cfg.ui.footerIntensity = (v===0.25||v===0.35||v===0.45) ? v : 0.35;
      }
      saveProjects();
      broadcastProjectInfo();
    }
      return;
    }
    if (msg.type === "adminSetAssignments"){
      if (!currentProjectId) return;
      const cfg = ensureProjectCfg(currentProjectId);
      if (ws.user !== cfg.admin) return;
      const target = String(msg.target||"");
      const guids = Array.isArray(msg.guids) ? msg.guids.map(String) : [];
      if (!cfg.assignments[target]) cfg.assignments[target] = {all:false, guids:[]};
      cfg.assignments[target].all = false;
      cfg.assignments[target].guids = guids;
      saveProjects();
      broadcastProjectInfo();
      // notify
      for (const c of wsClients) sendTo(c, {type:"assignments"});
      // push updated filtered state
      if (lastState){
        for (const c of wsClients) sendTo(c, filterStateFor(c, lastState));
      }
      return;
    }

    // pass-through control commands to REAPER over TCP, but validate permissions
    const needsGuid = new Set([
      "setVol","setPan","setMute","setSolo","setRec","setRecInput",
      "setFxEnabled","setFxAllEnabled","deleteFx","setFxParam","addFx","moveFx","showFxChain",
      "reqFxList","reqFxParams",
      "setSendVol","setSendMute","setSendMode","addSend",
      "setRecvVol","setRecvMute","addReturn",
      "renameTrack"
    ]);
    if (needsGuid.has(msg.type)){
      const guid = String(msg.guid||"");
      if (!guid) return;
      // master only controllable by admin/main
      if (guid === "MASTER" || guid === "{MASTER}"){
        const cfg = ensureProjectCfg(currentProjectId);
        if (ws.user !== cfg.admin) return;
      }else{
        if (!canControl(ws, guid)) return;
      }
    }
    // forward to REAPER
    if (reaperSock){
      try{ reaperSock.write(JSON.stringify(msg) + "\n"); }catch{}
    }
  });

  ws.on("close", ()=>{ wsClients.delete(ws); });
});

// ---- TCP server (REAPER -> Node) ----
let reaperSock = null;

const tcpServer = net.createServer((sock) => {
  console.log("REAPER connected via TCP");
  reaperSock = sock;
  sock.setEncoding("utf8");
  let buf = "";

  sock.on("data", (chunk) => {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx);
      buf = buf.slice(idx + 1);
      if (!line.trim()) continue;
      let msg = null;
      try { msg = JSON.parse(line); } catch (e) { continue; }
      if (!msg || !msg.type) continue;

      if (msg.type === "state"){
        // update project
        const pName = msg.projectName || "Untitled";
        const pPath = msg.projectPath || "";
        const pid = getProjectId(pPath, pName);
        const changed = (pid !== currentProjectId);
        currentProjectId = pid;
        currentProjectName = pName;
        currentProjectPath = pPath;
        const cfg = ensureProjectCfg(currentProjectId);
        cfg.projectName = currentProjectName;
        projects[currentProjectId] = cfg;
        if (changed) broadcastProjectInfo();

        lastState = msg;
        // broadcast filtered
        for (const ws of wsClients){
          sendTo(ws, filterStateFor(ws, msg));
        }
        continue;
      }

      if (msg.type === "meter"){
        lastMeter = msg;
        for (const ws of wsClients){
          sendTo(ws, filterMeterFor(ws, msg));
        }
        continue;
      }

      if (msg.type === "fxList"){
        // only send to users who can see this track
        for (const ws of wsClients){
          if (ws.user && canControl(ws, msg.guid)){
            sendTo(ws, msg);
          } else if (ws.user === ensureProjectCfg(currentProjectId).admin && ws.user){
            sendTo(ws, msg);
          }
        }
        continue;
      }

      // default broadcast
      for (const ws of wsClients) sendTo(ws, msg);
    }
  });

  sock.on("end", () => {
    console.log("REAPER TCP end");
  });

  sock.on("close", () => {
    console.log("REAPER TCP disconnected");
    if (reaperSock === sock) reaperSock = null;
  });

  sock.on("error", (e) => {
    console.log("REAPER TCP error:", e.message || e);
  });
});

tcpServer.listen(TCP_PORT, "127.0.0.1", () => {
  console.log(`TCP (ReaScript) listening on 127.0.0.1:${TCP_PORT}`);
});
