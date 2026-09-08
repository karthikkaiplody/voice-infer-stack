import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
const { findChrome } = await import(`${process.env.HOME}/.claude/skills/archify/bin/visual-check.mjs`);

const chrome = findChrome();
if (!chrome) { console.error('no chrome'); process.exit(1); }

const dir = path.dirname(new URL(import.meta.url).pathname);
const names = ['cascade-pipeline', 'turn-timeline', 'false-endpoint'];
const W = 1600, H = 1000, SCALE = 2;

const userDataDir = fs.mkdtempSync('/tmp/archify-shot-');
const child = spawn(chrome, [
  '--headless=new', '--remote-debugging-pipe', '--disable-gpu', '--hide-scrollbars',
  `--user-data-dir=${userDataDir}`, '--no-first-run', '--no-default-browser-check',
  `--window-size=${W},${H}`, 'about:blank',
], { stdio: ['ignore', 'ignore', 'inherit', 'pipe', 'pipe'] });

const write = child.stdio[3], read = child.stdio[4];
read.setEncoding('utf8');
let buf = '', id = 0;
const pending = new Map();
read.on('data', (c) => {
  buf += c;
  let i;
  while ((i = buf.indexOf('\0')) >= 0) {
    const raw = buf.slice(0, i); buf = buf.slice(i + 1);
    if (!raw) continue;
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    }
  }
});
const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const msg = { id: ++id, method, params };
  if (sessionId) msg.sessionId = sessionId;
  pending.set(msg.id, { resolve, reject });
  write.write(JSON.stringify(msg) + '\0');
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(800);

for (const name of names) {
  for (const [mode, suffix, h, beyond] of [['present', '', 1000, false], ['embed', 'clean.', 760, true]])
  for (const theme of ['dark', 'light']) {
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Emulation.setDeviceMetricsOverride',
      { width: W, height: h, deviceScaleFactor: SCALE, mobile: false }, sessionId);
    const url = `file://${dir}/${name}.html?${mode}=1&theme=${theme}`;
    await send('Page.navigate', { url }, sessionId);
    await sleep(3500);
    const { data } = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: beyond }, sessionId);
    const out = path.join(dir, 'slides', `${name}.${suffix}${theme}.png`);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, Buffer.from(data, 'base64'));
    console.log('wrote', out);
    await send('Target.closeTarget', { targetId });
  }
}
child.kill();
process.exit(0);
