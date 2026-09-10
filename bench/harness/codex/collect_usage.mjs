// Export only numeric usage from this run's sessions, never auth or session text.
// Rollout totals are cumulative: retain the LAST snapshot for each thread.
import fs from 'node:fs';
import path from 'node:path';
const art = process.env.ARTIFACTS_DIR || '/artifacts';
const home = process.env.CODEX_HOME || '/codex-auth';
function events(file) {
  return fs.readFileSync(file, 'utf8').split('\n').flatMap(line => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
}
function files(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(e =>
    e.isDirectory() ? files(path.join(dir,e.name)) : [path.join(dir,e.name)]);
}
const ids = new Set(fs.readdirSync(art).filter(n => /^codex-\d+\.jsonl$/.test(n))
  .flatMap(n => events(path.join(art,n))).filter(e => e.type === 'thread.started').map(e => e.thread_id));
const sessions = new Map();
for (const file of files(path.join(home,'sessions')).filter(n => n.endsWith('.jsonl'))) {
  // Filter filenames before reading potentially unrelated cached sessions.
  if (![...ids].some(id => path.basename(file).includes(id))) continue;
  let id;
  for (const event of events(file)) {
    if (event.type === 'session_meta') id = event.payload?.id;
    const usage = event.payload?.type === 'token_count' && event.payload?.info?.total_token_usage;
    if (!ids.has(id) || !usage || !Number.isFinite(usage.input_tokens)) continue;
    const numeric = Object.fromEntries(Object.entries(usage).filter(([k,v]) =>
      ['input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens','reasoning_output_tokens','total_tokens'].includes(k)
      && Number.isFinite(v) && v >= 0));
    sessions.set(id, numeric);
  }
}
const out = path.join(art,'codex-session-usage.json');
fs.writeFileSync(out+'.tmp', JSON.stringify({source:'session_token_count',sessions:[...sessions].map(([thread_id,usage]) => ({thread_id,usage}))},null,2));
fs.renameSync(out+'.tmp',out);
