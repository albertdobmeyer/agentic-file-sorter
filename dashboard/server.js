const express = require('express');
const fs = require('fs');
const path = require('path');
const { spawn, execSync } = require('child_process');
const http = require('http');

const app = express();
const PORT = parseInt(process.argv.find((a, i) => process.argv[i - 1] === '--port') || '7860');
const ROOT = path.resolve(__dirname, '..');
const CONFIG_PATH = path.join(ROOT, 'afs-config.json');
const FACES_DIR = path.join(ROOT, 'faces');
const AFS_PY = path.join(ROOT, 'afs.py');

app.use(express.static(path.join(__dirname, 'public')));
app.use(express.json({ limit: '50mb' }));

// --- Config ---

const DEFAULTS = {
  models: {
    ollama_url: "http://localhost:11434",
    vision_model: "llava:latest",
    text_model: "qwen3:8b",
    vision_timeout: 180,
    text_timeout: 120,
    vision_ctx: 4096,
    text_ctx: 8192,
    keep_alive: "30m"
  },
  processing: {
    sanitize_images: true,
    convert_webp: true,
    chunk_size: 30,
    confidence_threshold: 0.5,
    photo_threshold_mp: 4.0,
    skip_cdr_photos: true,
    faces_dir: "",
    identify_faces: true
  },
  sorting: {
    max_topics: 25,
    max_topic_words: 2,
    cleanup_empty_folders: true,
    photo_sorting: "flat",
    custom_folders: {},
    folder_aliases: {}
  }
};

app.get('/api/config', (req, res) => {
  try {
    const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
    // Deep merge with defaults
    const merged = JSON.parse(JSON.stringify(DEFAULTS));
    for (const section of Object.keys(merged)) {
      if (cfg[section]) Object.assign(merged[section], cfg[section]);
    }
    res.json(merged);
  } catch {
    res.json(DEFAULTS);
  }
});

app.put('/api/config', (req, res) => {
  try {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(req.body, null, 2) + '\n', 'utf-8');
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/config/defaults', (req, res) => res.json(DEFAULTS));

// --- Status ---

app.get('/api/status', (req, res) => {
  try {
    const out = execSync(`python "${AFS_PY}" --json status`, { cwd: ROOT, timeout: 15000 });
    res.json(JSON.parse(out.toString().trim()));
  } catch (e) {
    res.status(500).json({ error: 'Failed to get status', detail: e.message });
  }
});

// --- Ollama models ---

app.get('/api/models', async (req, res) => {
  try {
    const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
    const url = (cfg.models && cfg.models.ollama_url) || DEFAULTS.models.ollama_url;
    const resp = await fetch(`${url}/api/tags`);
    const data = await resp.json();
    res.json(data.models || []);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// --- Face samples ---

app.get('/api/faces', (req, res) => {
  const result = {};
  if (!fs.existsSync(FACES_DIR)) return res.json(result);

  for (const entry of fs.readdirSync(FACES_DIR, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;

    if (entry.isDirectory()) {
      const personDir = path.join(FACES_DIR, entry.name);
      const files = fs.readdirSync(personDir).filter(f => /\.(jpg|jpeg|png|webp|bmp|gif|tiff?)$/i.test(f));
      if (files.length) result[entry.name] = files;
    } else if (/\.(jpg|jpeg|png|webp|bmp|gif|tiff?)$/i.test(entry.name)) {
      const name = path.parse(entry.name).name.toLowerCase().replace(/[_ ]/g, '-');
      result[name] = result[name] || [];
      result[name].push(entry.name);
    }
  }
  res.json(result);
});

app.post('/api/faces/:name', express.raw({ type: '*/*', limit: '10mb' }), (req, res) => {
  const personDir = path.join(FACES_DIR, req.params.name);
  fs.mkdirSync(personDir, { recursive: true });
  const filename = req.headers['x-filename'] || `sample-${Date.now()}.jpg`;
  fs.writeFileSync(path.join(personDir, filename), req.body);
  res.json({ ok: true });
});

app.delete('/api/faces/:name/:file', (req, res) => {
  const filePath = path.join(FACES_DIR, req.params.name, req.params.file);
  try {
    fs.unlinkSync(filePath);
    // Remove dir if empty
    const dir = path.join(FACES_DIR, req.params.name);
    if (fs.existsSync(dir) && fs.readdirSync(dir).length === 0) fs.rmdirSync(dir);
    res.json({ ok: true });
  } catch (e) {
    res.status(404).json({ error: e.message });
  }
});

app.put('/api/faces/:name', (req, res) => {
  const oldDir = path.join(FACES_DIR, req.params.name);
  const newDir = path.join(FACES_DIR, req.body.newName);
  try {
    fs.renameSync(oldDir, newDir);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Serve face sample images
app.get('/api/faces/:name/:file/image', (req, res) => {
  const filePath = path.join(FACES_DIR, req.params.name, req.params.file);
  if (fs.existsSync(filePath)) res.sendFile(filePath);
  else res.status(404).end();
});

// --- Run (SSE via GET after POST start) ---

let activeRun = null;

app.post('/api/run', (req, res) => {
  const { input, output, dryRun, force, noSanitize, noConvertWebp, reface, maxFiles } = req.body;
  if (!input) return res.status(400).json({ error: 'input directory required' });
  if (activeRun) return res.status(409).json({ error: 'A process is already running' });

  const args = [AFS_PY, '--json', 'process', input];
  if (output) args.push('-o', output);
  if (dryRun) args.push('--dry-run');
  if (force) args.push('--force');
  if (noSanitize) args.push('--no-sanitize');
  if (noConvertWebp) args.push('--no-convert-webp');
  if (reface) args.push('--reface');
  if (maxFiles > 0) args.push('--max-files', String(maxFiles));

  console.log('Run:', 'python', args.join(' '));
  const proc = spawn('python', args, { cwd: ROOT, shell: true });
  activeRun = { proc, events: [], done: false, clients: [] };

  proc.stdout.on('data', d => {
    d.toString().split('\n').filter(Boolean).forEach(line => {
      activeRun.events.push(line);
      activeRun.clients.forEach(c => { try { c.write(`data: ${line}\n\n`); } catch {} });
    });
  });
  proc.stderr.on('data', d => {
    const msg = JSON.stringify({ event: 'log', message: d.toString().trim() });
    activeRun.events.push(msg);
    activeRun.clients.forEach(c => { try { c.write(`data: ${msg}\n\n`); } catch {} });
  });
  proc.on('error', e => {
    console.log('Spawn error:', e.message);
    const msg = JSON.stringify({ event: 'error', error: e.message });
    activeRun.events.push(msg);
    activeRun.done = true;
  });
  proc.on('close', code => {
    console.log('Process exited:', code);
    const msg = JSON.stringify({ event: 'exit', code });
    activeRun.events.push(msg);
    activeRun.clients.forEach(c => { try { c.write(`data: ${msg}\n\n`); c.end(); } catch {} });
    activeRun.done = true;
    activeRun.clients = [];
    setTimeout(() => { activeRun = null; }, 5000);
  });

  res.json({ ok: true, started: true });
});

app.get('/api/run/stream', (req, res) => {
  if (!activeRun) return res.status(404).json({ error: 'No active run' });

  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });

  // Send all buffered events first
  for (const line of activeRun.events) {
    res.write(`data: ${line}\n\n`);
  }

  if (activeRun.done) {
    res.end();
    return;
  }

  // Subscribe to future events
  activeRun.clients.push(res);
  req.on('close', () => {
    if (activeRun) activeRun.clients = activeRun.clients.filter(c => c !== res);
  });
});

app.post('/api/flatten', (req, res) => {
  const { input, dryRun } = req.body;
  if (!input) return res.status(400).json({ error: 'input directory required' });

  const args = [AFS_PY, 'flatten', input];
  if (dryRun) args.push('--dry-run');

  try {
    const out = execSync(`python ${args.map(a => `"${a}"`).join(' ')}`, { cwd: ROOT, timeout: 60000 });
    res.json({ ok: true, output: out.toString() });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// --- Manifest ---

app.get('/api/manifest', (req, res) => {
  const dir = req.query.dir || ROOT;
  const manifestPath = path.join(dir, '.afs-manifest.json');
  try {
    if (fs.existsSync(manifestPath)) res.json(JSON.parse(fs.readFileSync(manifestPath, 'utf-8')));
    else res.json(null);
  } catch { res.json(null); }
});

// --- Directory browser (for file picker) ---

app.get('/api/browse', (req, res) => {
  const dir = req.query.dir || (process.platform === 'win32' ? 'C:\\' : '/');
  try {
    const resolved = path.resolve(dir);
    const entries = fs.readdirSync(resolved, { withFileTypes: true });
    const dirs = [];
    const fileCount = { total: 0, images: 0 };

    for (const entry of entries) {
      if (entry.name.startsWith('.')) continue;
      if (entry.isDirectory()) {
        dirs.push(entry.name);
      } else if (entry.isFile()) {
        fileCount.total++;
        if (/\.(jpg|jpeg|png|gif|webp|bmp|tiff?|webm|mp4|mov|avi|mkv)$/i.test(entry.name)) {
          fileCount.images++;
        }
      }
    }

    // Get parent directory
    const parent = path.dirname(resolved);

    // Get drive letters on Windows
    let drives = [];
    if (process.platform === 'win32') {
      try {
        const out = require('child_process').execSync('wmic logicaldisk get name', { encoding: 'utf-8' });
        drives = out.split('\n').map(l => l.trim()).filter(l => /^[A-Z]:$/.test(l)).map(d => d + '\\');
      } catch {
        drives = ['C:\\'];
      }
    }

    res.json({
      current: resolved,
      parent: parent !== resolved ? parent : null,
      drives,
      directories: dirs.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' })),
      fileCount,
    });
  } catch (e) {
    res.status(400).json({ error: e.message, current: dir });
  }
});

app.listen(PORT, () => console.log(`AFS Dashboard: http://localhost:${PORT}`));
