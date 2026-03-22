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

// --- Run ---

app.post('/api/run', (req, res) => {
  const { input, output, dryRun, force, noSanitize, noConvertWebp, reface, maxFiles } = req.body;
  if (!input) return res.status(400).json({ error: 'input directory required' });

  const args = ['process', input, '--json'];
  if (output) args.push('-o', output);
  if (dryRun) args.push('--dry-run');
  if (force) args.push('--force');
  if (noSanitize) args.push('--no-sanitize');
  if (noConvertWebp) args.push('--no-convert-webp');
  if (reface) args.push('--reface');
  if (maxFiles > 0) args.push('--max-files', String(maxFiles));

  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });

  const proc = spawn('python', [AFS_PY, '--json', ...args.slice(1)], { cwd: ROOT });

  proc.stdout.on('data', d => {
    d.toString().split('\n').filter(Boolean).forEach(line => {
      res.write(`data: ${line}\n\n`);
    });
  });
  proc.stderr.on('data', d => res.write(`data: ${JSON.stringify({ event: 'log', message: d.toString() })}\n\n`));
  proc.on('close', code => {
    res.write(`data: ${JSON.stringify({ event: 'exit', code })}\n\n`);
    res.end();
  });
  req.on('close', () => proc.kill());
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

app.listen(PORT, () => console.log(`AFS Dashboard: http://localhost:${PORT}`));
