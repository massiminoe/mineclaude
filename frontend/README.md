# MineTrials monitor

Read-only React/TypeScript monitor for the shared MineTrials runtime: live video,
action queue, event history, player vitals, and inventory details.

Use Node.js 22.12+ and start the Python runtime from the repository root with
`make run` (or `make run-mock` for development without Minecraft).

```bash
npm ci
npm run dev
```

Open http://localhost:5173. Vite forwards `/api` and `/video` to the runtime on
port 5555 and `/bridge` to the native bridge on port 8081. Configure these
development proxies in `vite.config.ts` if your services use different ports.

`npm run build` checks TypeScript and creates `dist/`. The Python runtime serves
that build on port 5555. `npm run lint` runs the frontend lint rules.

The agent drives the bot through MCP; the monitor has no command console.

Item icons are generated locally from `minecraft-assets` before `npm run dev`
and `npm run build`. The generated `public/itemIcons.json` is ignored by Git;
Minecraft textures retain their upstream terms (see [THIRD_PARTY.md](../THIRD_PARTY.md)).
